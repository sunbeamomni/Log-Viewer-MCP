"""server/config.py 的单元测试（任务 1.4、14.2；需求 6.6、10.1、10.2）。

启动检查不检查属主、权限位与是否以 root 运行；凭据文件只须存在、是普通文件、可读且非空。
"""

import copy
import json
import os
import re
from datetime import timedelta

import pytest

from log_viewer_mcp.server import config as cfg
from log_viewer_mcp.server.config import ConfigError, load_config, load_config_or_exit

posix_only = pytest.mark.skipif(not hasattr(os, "geteuid"), reason="只在 POSIX 系统上运行")

SHA = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
TOKENS = [
    {"token_id": "a1b2c3d4", "user": "zhangsan", "sha256": SHA, "created_at": "2026-09-17T10:00:00-02:00", "revoked": False},
    {"token_id": "e5f6", "user": "lisi", "sha256": SHA, "created_at": "2026-09-17T11:00:00-02:00", "revoked": True},
]

_DELETE = object()


# ---------------------------------------------------------------------------
# 夹具：在临时目录中写配置与凭据
# ---------------------------------------------------------------------------


def _toml_value(value):
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) in (int, float):
        return repr(value)
    if type(value) is str:
        return json.dumps(value)  # JSON 字符串转义是 TOML 基本字符串的子集
    if type(value) is list:
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise TypeError(value)


def _to_toml(data, prefix=()):
    lines = []
    for key, value in data.items():
        if type(value) is not dict:
            lines.append(f"{json.dumps(key)} = {_toml_value(value)}")
    for key, value in data.items():
        if type(value) is dict:
            path = prefix + (key,)
            lines.append("")
            lines.append("[" + ".".join(json.dumps(k) for k in path) + "]")
            lines.extend(_to_toml(value, path))
    return lines


class Env:
    """临时目录中的一套配置：base 为配置字典，write() 应用修改后写出 config.toml。"""

    def __init__(self, root):
        self.root = root
        cred = root / "cred"
        cred.mkdir()
        self.files = {
            "password": cred / "db_password",
            "key": cred / "id_ed25519",
            "known_hosts": cred / "known_hosts",
            "tokens": cred / "tokens.json",
        }
        self.write_file("password", b"s3cret;'\"pw\n")
        self.write_file("key", b"-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----\n")
        self.write_file("known_hosts", b"10.0.1.11 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIE\n")
        self.write_file("tokens", json.dumps(TOKENS).encode())
        p = {k: v.as_posix() for k, v in self.files.items()}
        self.base = {
            "server_timezone": "-02:00",
            "tokens_file": p["tokens"],
            "zip_dir": "/usr/local/ds/SFS2X/ziplogs",
            "listen": {"address": "0.0.0.0", "port": 8080},
            "admin_db": {
                "host": "10.0.0.2",
                "port": 3306,
                "user": "logview",
                "database": "admin",
                "password_file": p["password"],
            },
            "ssh": {
                "user": "root",
                "port": 22,
                "key_file": p["key"],
                "known_hosts": p["known_hosts"],
                "connect_timeout_s": 10,
            },
            "limits": {"max_servers": 10, "query_timeout_s": 40},
            "results": {"dir": (root / "results").as_posix(), "ttl_min": 60, "cleanup_interval_min": 5},
            "audit": {"dir": (root / "audit").as_posix(), "retention_days": 90},
            "log_types": {
                "normal": {"dir": "/usr/local/ds/SFS2X/logs", "suffix": ""},
                "login": {"dir": "/usr/local/ds/SFS2X/logpushinit", "suffix": ".logpushinit"},
                "analysis": {"dir": "/usr/local/ds/SFS2X/loganalys", "suffix": ".loganalys"},
                "pulsar": {"dir": "/usr/local/ds/SFS2X/polsarlog", "suffix": ".polsarlog"},
            },
        }

    def write_file(self, name, content, mode=0o600):
        path = self.files[name]
        path.write_bytes(content)
        os.chmod(path, mode)

    def write(self, *changes):
        """changes 为 (路径元组, 值)；值为 _DELETE 时删除该项。返回 config.toml 的路径。"""
        data = copy.deepcopy(self.base)
        for path, value in changes:
            table = data
            for key in path[:-1]:
                table = table[key]
            if value is _DELETE:
                del table[path[-1]]
            else:
                table[path[-1]] = value
        out = self.root / "config.toml"
        out.write_text("\n".join(_to_toml(data)) + "\n", encoding="utf-8")
        return str(out)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def _raises(path, fragment):
    with pytest.raises(ConfigError, match=re.escape(fragment)) as ei:
        load_config(path)
    return ei.value


# ---------------------------------------------------------------------------
# 合法配置
# ---------------------------------------------------------------------------


def test_valid_config_loads(env):
    c = load_config(env.write())
    assert c.listen == cfg.ListenConfig("0.0.0.0", 8080)
    assert c.tz.utcoffset(None) == timedelta(hours=-2)
    assert c.timezone_label == "GMT-2"
    assert list(c.log_types) == ["normal", "login", "analysis", "pulsar"]
    assert c.log_types["login"].suffix == ".logpushinit"
    assert c.zip_dir == "/usr/local/ds/SFS2X/ziplogs"
    assert c.admin_db.password == "s3cret;'\"pw"  # 只去掉末尾换行
    assert c.ssh.private_key.startswith(b"-----BEGIN")
    assert [t.token_id for t in c.tokens] == ["a1b2c3d4", "e5f6"]
    assert [t.token_id for t in c.active_tokens] == ["a1b2c3d4"]
    # 未写出的 limits 项取默认值
    assert c.limits.max_concurrent == 8
    assert c.limits.max_scan_bytes == 8 * 1024**3


def test_secrets_not_in_repr(env):
    c = load_config(env.write())
    assert "s3cret" not in repr(c)
    assert "OPENSSH" not in repr(c)


def test_optional_tables_use_defaults(env):
    c = load_config(env.write((("limits",), _DELETE), (("results",), _DELETE), (("server_timezone",), _DELETE)))
    assert c.limits == cfg.Limits()
    assert c.results == cfg.ResultsConfig()
    assert c.server_timezone == "-02:00"


# ---------------------------------------------------------------------------
# 缺项、类型错误、未知项
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        ("listen",),
        ("listen", "port"),
        ("listen", "address"),
        ("admin_db",),
        ("admin_db", "host"),
        ("log_types",),
        ("log_types", "normal", "dir"),
    ],
)
def test_missing_required_item(env, path):
    _raises(env.write((path, _DELETE)), "缺少配置项 " + ".".join(path))


@pytest.mark.parametrize(
    "path, value",
    [
        (("listen", "port"), "8080"),
        (("listen", "port"), True),
        (("listen", "port"), 8080.0),
        (("listen", "port"), 0),
        (("listen", "port"), 65536),
        (("listen", "address"), 8080),
        (("listen", "address"), "bad host!"),
        (("admin_db", "host"), 3306),
        (("admin_db", "user"), ""),
        (("admin_db", "database"), "admin\n"),
        (("ssh", "connect_timeout_s"), 0),
        (("ssh", "port"), "22"),
        (("limits", "max_servers"), 1.5),
        (("limits", "max_servers"), True),
        (("limits", "max_context_lines"), -1),
        (("results", "ttl_min"), "60"),
        (("audit", "retention_days"), 0),
        (("tokens_file",), "relative/tokens.json"),
        (("admin_db", "password_file"), 123),
        (("admin_db", "password_file"), "relative/db_password"),
        (("ssh", "known_hosts"), "relative/known_hosts"),
        (("ssh", "key_file"), ""),
        (("audit", "dir"), "relative/audit"),
        (("zip_dir",), 1),
        (("listen",), "0.0.0.0:8080"),
        (("log_types", "normal"), "/usr/local/ds/SFS2X/logs"),
    ],
)
def test_wrong_type_or_range(env, path, value):
    with pytest.raises(ConfigError, match=re.escape(".".join(path))):
        load_config(env.write((path, value)))


@pytest.mark.parametrize("value", ["Etc/GMT-2", "GMT-2", "-2", "-0200", "-02:00\n", "-24:00", "+02:60", "", -2])
def test_invalid_server_timezone(env, value):
    _raises(env.write((("server_timezone",), value)), "server_timezone")


@pytest.mark.parametrize("path", [("unknown",), ("listen", "tls"), ("limits", "max_server"), ("log_types", "normal", "zip")])
def test_unknown_item(env, path):
    _raises(env.write((path, 1)), "未知的配置项 " + ".".join(path))


def test_limits_cross_checks(env):
    _raises(env.write((("limits", "default_lookback_h"), 25)), "default_lookback_h")
    _raises(env.write((("limits", "max_time_span_h"), 1), (("limits", "default_lookback_h"), 1), (("limits", "point_window_min"), 31)), "point_window_min")


def test_invalid_toml(env):
    path = env.root / "config.toml"
    path.write_text("listen = = 1\n", encoding="utf-8")
    _raises(str(path), "不是合法的 TOML")


def test_missing_config_file(env):
    _raises(str(env.root / "nope.toml"), "不可读")


# ---------------------------------------------------------------------------
# 目录格式与 [log_types]
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "/usr/local/ds:/tmp",
        "/usr/local/ds/SFS2X/logs:",
        "usr/local/ds/SFS2X/logs",
        "",
        "/usr/local/ds/../../etc",
        "/usr/local/ds SFS2X",
        "/usr/local/ds;rm -rf /",
        "/usr/local/$(id)",
        "/usr/local/ds'",
        "/usr/local/ds\n",
        "/usr/local/日志",
        "C:/logs",
    ],
)
@pytest.mark.parametrize("path", [
    ("zip_dir",), ("log_types", "normal", "dir"), ("log_types", "pulsar", "dir"), ("log_types", "pulsar", "zip_dir"),
])
def test_invalid_remote_dir(env, path, value):
    _raises(env.write((path, value)), ".".join(path))


def test_empty_log_types(env):
    _raises(env.write((("log_types",), {})), "至少要定义一种日志类型")


@pytest.mark.parametrize("name", ["Normal", "2x", "_x", "nor-mal", "a" * 33, "日志"])
def test_invalid_log_type_name(env, name):
    _raises(env.write((("log_types", name), {"dir": "/x", "suffix": ".x"})), "[log_types] 的键")


@pytest.mark.parametrize("suffix", [".2026", "logpushinit", ".log:x", ". x", "." + "a" * 33, 1])
def test_invalid_suffix(env, suffix):
    _raises(env.write((("log_types", "login", "suffix"), suffix)), "log_types.login.suffix")


def test_duplicate_suffix(env):
    _raises(env.write((("log_types", "login", "suffix"), ".loganalys")), "suffix 却相同")


def test_duplicate_suffix_in_same_per_type_zip_dir(env):
    # 类型自己的 zip_dir 与顶层相同（末尾多一个 /）时仍是同一目录
    _raises(env.write((("log_types", "login", "suffix"), ""),
                      (("log_types", "login", "zip_dir"), "/usr/local/ds/SFS2X/ziplogs/")), "suffix 却相同")


def test_zip_dir_is_optional(env):
    c = load_config(env.write((("zip_dir",), _DELETE)))
    assert c.zip_dir is None
    assert all(c.zip_dir_for(t) is None for t in c.log_types.values())


def test_zip_dir_omitted_allows_any_suffixes(env):
    # 没有压缩目录时 suffix 不参与区分，可以相同
    c = load_config(env.write((("zip_dir",), _DELETE), (("log_types", "login", "suffix"), ".loganalys")))
    assert c.log_types["login"].suffix == c.log_types["analysis"].suffix == ".loganalys"


def test_per_type_zip_dir_overrides_top_level(env):
    c = load_config(env.write((("log_types", "pulsar", "zip_dir"), "/data/pulsar_zip")))
    assert c.log_types["pulsar"].zip_dir == "/data/pulsar_zip"
    assert c.zip_dir_for(c.log_types["pulsar"]) == "/data/pulsar_zip"
    assert c.log_types["normal"].zip_dir is None
    assert c.zip_dir_for(c.log_types["normal"]) == "/usr/local/ds/SFS2X/ziplogs"


def test_per_type_zip_dir_without_top_level(env):
    c = load_config(env.write((("zip_dir",), _DELETE), (("log_types", "normal", "zip_dir"), "/data/zip")))
    assert c.zip_dir_for(c.log_types["normal"]) == "/data/zip"
    assert c.zip_dir_for(c.log_types["login"]) is None


def test_same_suffix_allowed_in_different_zip_dirs(env):
    c = load_config(env.write((("log_types", "login", "suffix"), ""),
                              (("log_types", "login", "zip_dir"), "/data/login_zip")))
    assert c.log_types["login"].suffix == c.log_types["normal"].suffix == ""


# ---------------------------------------------------------------------------
# 凭据文件与 tokens.json 的内容
# ---------------------------------------------------------------------------


def test_missing_credential_file(env):
    env.files["key"].unlink()
    _raises(env.write(), "SSH 私钥文件")


def test_credential_path_is_directory(env):
    _raises(env.write((("admin_db", "password_file"), env.root.as_posix())), "数据库密码文件")


@pytest.mark.parametrize(
    "content, fragment",
    [(b"", "为空"), (b"\n", "为空"), (b"a\nb\n", "只能有一行"), (b"\xff\xfe", "不是 UTF-8")],
)
def test_invalid_password_file(env, content, fragment):
    env.write_file("password", content)
    _raises(env.write(), fragment)


def test_empty_known_hosts(env):
    env.write_file("known_hosts", b"\n")
    _raises(env.write(), "known_hosts 文件")


@pytest.mark.parametrize(
    "tokens, fragment",
    [
        ({"token_id": "a"}, "顶层必须是数组"),
        ([{k: v for k, v in TOKENS[0].items() if k != "sha256"}], "缺少 sha256"),
        ([{**TOKENS[0], "extra": 1}], "多出 extra"),
        ([{**TOKENS[0], "sha256": SHA.upper()}], "sha256"),
        ([{**TOKENS[0], "revoked": "false"}], "revoked"),
        ([{**TOKENS[0], "token_id": "a/b"}], "token_id"),
        ([TOKENS[0], TOKENS[0]], "重复"),
        ([{**TOKENS[0], "user": "x\ny"}], "user"),
    ],
)
def test_invalid_tokens(env, tokens, fragment):
    env.write_file("tokens", json.dumps(tokens).encode())
    _raises(env.write(), fragment)


@pytest.mark.parametrize("content", [b"[{", b"", b"\xff\xfe"])
def test_tokens_not_json(env, content):
    env.write_file("tokens", content)
    _raises(env.write(), "不是合法的 UTF-8 JSON")


def test_missing_tokens_file_gives_empty_set(env):
    env.files["tokens"].unlink()
    c = load_config(env.write())
    assert c.tokens == ()
    assert c.active_tokens == ()


def test_tokens_path_is_directory(env):
    _raises(env.write((("tokens_file",), env.root.as_posix())), "token 登记文件")


# ---------------------------------------------------------------------------
# admin_db 的 password 与 password_file
# ---------------------------------------------------------------------------

INLINE_PW = "inl1ne;'\"Pw"


def test_inline_password(env):
    c = load_config(env.write((("admin_db", "password_file"), _DELETE), (("admin_db", "password"), INLINE_PW)))
    assert c.admin_db.password == INLINE_PW
    assert c.admin_db.password_file is None
    assert INLINE_PW not in repr(c)


def test_password_file_sets_path(env):
    c = load_config(env.write())
    assert c.admin_db.password_file == env.files["password"].as_posix()


def test_password_and_password_file_both_given(env, capsys):
    path = env.write((("admin_db", "password"), INLINE_PW))
    e = _raises(path, "password 与 password_file")
    assert INLINE_PW not in str(e) and "s3cret" not in str(e)
    with pytest.raises(SystemExit):
        load_config_or_exit(path)
    assert INLINE_PW not in capsys.readouterr().err


def test_neither_password_nor_password_file(env):
    _raises(env.write((("admin_db", "password_file"), _DELETE)), "password 与 password_file")


@pytest.mark.parametrize("value", ["", 123, True])
def test_invalid_inline_password(env, value):
    _raises(env.write((("admin_db", "password_file"), _DELETE), (("admin_db", "password"), value)), "admin_db.password 必须是非空字符串")


# ---------------------------------------------------------------------------
# 默认值与可省略的项
# ---------------------------------------------------------------------------


def test_default_paths():
    assert cfg.DEFAULT_TOKENS_FILE == "/etc/log-viewer-mcp/tokens.json"
    assert cfg.DEFAULT_SSH_KEY_FILE == "/etc/log-viewer-mcp/ssh_key"
    assert cfg.AuditConfig().dir == "/var/log/log-viewer-mcp/audit"


def test_omitted_tokens_file_ssh_and_audit_use_defaults(env, monkeypatch):
    # 默认路径在开发机上不存在，换成临时目录中的文件，确认省略时取的是这两个默认值
    monkeypatch.setattr(cfg, "DEFAULT_TOKENS_FILE", env.files["tokens"].as_posix())
    monkeypatch.setattr(cfg, "DEFAULT_SSH_KEY_FILE", env.files["key"].as_posix())
    c = load_config(env.write((("tokens_file",), _DELETE), (("ssh",), _DELETE), (("audit",), _DELETE)))
    assert c.tokens_file == env.files["tokens"].as_posix()
    assert [t.token_id for t in c.tokens] == ["a1b2c3d4", "e5f6"]
    assert c.ssh.key_file == env.files["key"].as_posix()
    assert (c.ssh.user, c.ssh.port, c.ssh.known_hosts, c.ssh.connect_timeout_s) == ("root", 22, None, 10)
    assert c.audit == cfg.AuditConfig("/var/log/log-viewer-mcp/audit", 90)


def test_omitted_key_file_defaults_and_must_exist(env, monkeypatch):
    missing = (env.root / "nope" / "ssh_key").as_posix()
    monkeypatch.setattr(cfg, "DEFAULT_SSH_KEY_FILE", missing)
    e = _raises(env.write((("ssh", "key_file"), _DELETE)), "SSH 私钥文件")
    assert missing in str(e)


def test_omitted_known_hosts_passes(env):
    env.files["known_hosts"].unlink()
    c = load_config(env.write((("ssh", "known_hosts"), _DELETE)))
    assert c.ssh.known_hosts is None


def test_configured_known_hosts_must_exist(env):
    env.files["known_hosts"].unlink()
    _raises(env.write(), "known_hosts 文件")


# ---------------------------------------------------------------------------
# 不检查属主、权限位与是否以 root 运行
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fake_euid", ["root", "other"])
def test_owner_mode_and_root_not_checked(env, monkeypatch, fake_euid):
    for name in env.files:
        os.chmod(env.files[name], 0o644)
    owner = os.stat(env.files["key"]).st_uid
    euid = 0 if fake_euid == "root" else owner + 1
    monkeypatch.setattr(os, "geteuid", lambda: euid, raising=False)
    c = load_config(env.write())
    assert c.admin_db.password == "s3cret;'\"pw"
    assert len(c.tokens) == 2


# ---------------------------------------------------------------------------
# load_config_or_exit
# ---------------------------------------------------------------------------


def test_load_config_or_exit_reports_reason_without_secret(env, capsys):
    with pytest.raises(SystemExit) as e:
        load_config_or_exit(env.write((("listen", "port"), "x")))
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "listen.port" in err
    assert "s3cret" not in err


def test_load_config_or_exit_extra_check(env, capsys):
    def db_check(c):
        raise ConfigError("Admin_DB 无法连接")

    with pytest.raises(SystemExit) as e:
        load_config_or_exit(env.write(), extra_checks=[db_check])
    assert e.value.code == 2
    assert "Admin_DB 无法连接" in capsys.readouterr().err


def test_load_config_or_exit_success(env):
    seen = []
    c = load_config_or_exit(env.write(), extra_checks=[seen.append])
    assert seen == [c]


@posix_only
def test_fifo_rejected(env):
    fifo = env.root / "cred" / "fifo"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(ConfigError, match="不是普通文件"):
        load_config(env.write((("admin_db", "password_file"), fifo.as_posix())))


# ---------------------------------------------------------------------------
# 时区
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, minutes, label",
    [("-02:00", -120, "GMT-2"), ("+00:00", 0, "GMT"), ("-00:00", 0, "GMT"), ("+05:30", 330, "GMT+5:30"), ("-23:59", -1439, "GMT-23:59")],
)
def test_parse_server_timezone(value, minutes, label):
    tz = cfg.parse_server_timezone(value)
    assert tz.utcoffset(None) == timedelta(minutes=minutes)
    assert cfg.timezone_label(tz) == label
