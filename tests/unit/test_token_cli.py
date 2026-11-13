"""Token_CLI 的单元测试（任务 7.7；需求 6.2）。

覆盖 create、list、revoke 三个子命令，明文只打印一次且不落盘，原子写入不留临时文件，
拒绝符号链接，以及写出的 tokens.json 能被 server/config.py 接受。权限位检查只在 POSIX
系统上进行，在 Windows 上跳过，在测试运行容器中执行。
"""

import hashlib
import json
import os
import re
import stat
from datetime import datetime

import pytest

from log_viewer_mcp.server.config import load_config, parse_tokens
from log_viewer_mcp.token_cli import main as tcli
from tests.unit.test_config import Env

posix_only = pytest.mark.skipif(not hasattr(os, "geteuid"), reason="权限位检查只在 POSIX 系统上进行")

TOKEN_LINE = re.compile(r"^token：(\S+)$", re.M)


def run(capsys, *argv):
    code = tcli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def records(path):
    return json.loads(path.read_bytes().decode("utf-8"))


def create(capsys, path, user):
    """执行 create，返回 (打印的明文 token, 标准输出)。"""
    code, out, err = run(capsys, "-f", str(path), "create", user)
    assert code == 0, err
    tokens = TOKEN_LINE.findall(out)
    assert len(tokens) == 1, out
    return tokens[0], out


@pytest.fixture
def tokens_file(tmp_path):
    return tmp_path / "tokens.json"


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def test_create_new_file_stores_only_sha256(capsys, tokens_file):
    token, out = create(capsys, tokens_file, "zhangsan")

    [rec] = records(tokens_file)
    assert set(rec) == {"token_id", "user", "sha256", "created_at", "revoked"}
    assert rec["user"] == "zhangsan"
    assert rec["revoked"] is False
    assert rec["sha256"] == hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert datetime.fromisoformat(rec["created_at"]).tzinfo is not None
    assert f"token_id {rec['token_id']}" in out
    assert "立即生效" in out and "重启" not in out
    # 明文只在标准输出中出现一次，登记文件中没有明文
    assert out.count(token) == 1
    assert token.encode("utf-8") not in tokens_file.read_bytes()


def test_create_appends_with_unique_token_ids(capsys, tokens_file):
    tokens = [create(capsys, tokens_file, f"user{i}")[0] for i in range(5)]

    recs = records(tokens_file)
    assert [r["user"] for r in recs] == [f"user{i}" for i in range(5)]
    assert len({r["token_id"] for r in recs}) == 5
    assert len(set(tokens)) == 5
    assert [r["sha256"] for r in recs] == [tcli.hash_token(t) for t in tokens]


def test_create_retries_on_token_id_collision(capsys, tokens_file, monkeypatch):
    create(capsys, tokens_file, "zhangsan")
    existing = records(tokens_file)[0]["token_id"]
    ids = iter([existing, existing, "0badc0de"])
    monkeypatch.setattr(tcli.secrets, "token_hex", lambda n: next(ids))

    create(capsys, tokens_file, "lisi")

    assert [r["token_id"] for r in records(tokens_file)] == [existing, "0badc0de"]


def test_create_keeps_existing_records_unchanged(capsys, tokens_file):
    create(capsys, tokens_file, "zhangsan")
    before = records(tokens_file)

    create(capsys, tokens_file, "李四")

    after = records(tokens_file)
    assert after[:1] == before
    assert after[1]["user"] == "李四"


@pytest.mark.parametrize("user", ["", "bad\nname", "tab\tname"])
def test_create_rejects_invalid_user_without_writing(capsys, tokens_file, user):
    code, out, err = run(capsys, "-f", str(tokens_file), "create", user)

    assert code == 1
    assert "使用者名称" in err
    assert out == ""
    assert not tokens_file.exists()


def test_create_rejects_malformed_existing_file(capsys, tokens_file):
    tokens_file.write_bytes(b"{not json")

    code, _, err = run(capsys, "-f", str(tokens_file), "create", "zhangsan")

    assert code == 1
    assert "JSON" in err
    assert tokens_file.read_bytes() == b"{not json"


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_prints_columns(capsys, tokens_file):
    create(capsys, tokens_file, "zhangsan")
    create(capsys, tokens_file, "李四")
    recs = records(tokens_file)
    run(capsys, "-f", str(tokens_file), "revoke", recs[1]["token_id"])

    code, out, _ = run(capsys, "-f", str(tokens_file), "list")

    assert code == 0
    lines = out.splitlines()
    assert lines[0].split() == ["token_id", "user", "created_at", "revoked"]
    assert lines[1].split() == [recs[0]["token_id"], "zhangsan", recs[0]["created_at"], "no"]
    assert lines[2].split() == [recs[1]["token_id"], "李四", recs[1]["created_at"], "yes"]
    # 列按显示宽度对齐：各行 created_at 列的起始显示位置相同
    starts = [tcli._width(line[: line.index(cell)]) for line, cell in zip(lines, ["created_at", recs[0]["created_at"], recs[1]["created_at"]])]
    assert len(set(starts)) == 1
    # list 不输出摘要
    assert recs[0]["sha256"] not in out


def test_list_empty_file(capsys, tokens_file):
    tokens_file.write_bytes(b"[]\n")

    code, out, _ = run(capsys, "-f", str(tokens_file), "list")

    assert code == 0
    assert "没有登记任何 token" in out


def test_list_missing_file_fails(capsys, tokens_file):
    code, out, err = run(capsys, "-f", str(tokens_file), "list")

    assert code == 1
    assert out == ""
    assert str(tokens_file) in err


# ---------------------------------------------------------------------------
# revoke
# ---------------------------------------------------------------------------


def test_revoke_sets_revoked(capsys, tokens_file):
    create(capsys, tokens_file, "zhangsan")
    create(capsys, tokens_file, "lisi")
    before = records(tokens_file)
    target = before[0]["token_id"]

    code, out, _ = run(capsys, "-f", str(tokens_file), "revoke", target)

    assert code == 0
    assert f"已吊销 token_id {target}" in out
    assert "立即生效" in out and "重启" not in out
    after = records(tokens_file)
    assert after[0] == {**before[0], "revoked": True}
    assert after[1] == before[1]


def test_revoke_unknown_token_id_exits_1(capsys, tokens_file):
    create(capsys, tokens_file, "zhangsan")
    before = tokens_file.read_bytes()

    code, out, err = run(capsys, "-f", str(tokens_file), "revoke", "nosuchid")

    assert code == 1
    assert out == ""
    assert "找不到 token_id nosuchid" in err
    assert tokens_file.read_bytes() == before


def test_revoke_already_revoked_does_not_rewrite(capsys, tokens_file):
    create(capsys, tokens_file, "zhangsan")
    token_id = records(tokens_file)[0]["token_id"]
    run(capsys, "-f", str(tokens_file), "revoke", token_id)
    before_bytes = tokens_file.read_bytes()
    before_stat = os.stat(tokens_file)

    code, out, _ = run(capsys, "-f", str(tokens_file), "revoke", token_id)

    assert code == 0
    assert "已是吊销状态" in out
    assert tokens_file.read_bytes() == before_bytes
    after_stat = os.stat(tokens_file)
    assert (after_stat.st_ino, after_stat.st_mtime_ns) == (before_stat.st_ino, before_stat.st_mtime_ns)


def test_revoke_missing_file_fails(capsys, tokens_file):
    code, _, err = run(capsys, "-f", str(tokens_file), "revoke", "a1b2c3d4")

    assert code == 1
    assert str(tokens_file) in err
    assert not tokens_file.exists()


def test_help_names_command_and_does_not_mention_restart():
    parser = tcli.build_parser()
    assert parser.prog == "log-viewer-mcp-token"
    text = parser.format_help()
    assert text.startswith("usage: log-viewer-mcp-token")
    assert "立即生效" in text and "重启" not in text


def test_usage_error_exits_2(capsys):
    with pytest.raises(SystemExit) as e:
        tcli.main([])
    assert e.value.code == 2
    with pytest.raises(SystemExit) as e:
        tcli.main(["revoke"])
    assert e.value.code == 2


# ---------------------------------------------------------------------------
# 原子写入、符号链接与权限
# ---------------------------------------------------------------------------


def test_writes_leave_no_temp_files(capsys, tokens_file):
    create(capsys, tokens_file, "zhangsan")
    create(capsys, tokens_file, "lisi")
    run(capsys, "-f", str(tokens_file), "revoke", records(tokens_file)[0]["token_id"])

    assert sorted(p.name for p in tokens_file.parent.iterdir()) == ["tokens.json"]


def test_failed_replace_keeps_old_file_and_removes_temp(capsys, tokens_file, monkeypatch):
    create(capsys, tokens_file, "zhangsan")
    before = tokens_file.read_bytes()

    def fail_replace(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(tcli.os, "replace", fail_replace)
    code, out, err = run(capsys, "-f", str(tokens_file), "create", "lisi")

    assert code == 1
    assert "写入 token 登记文件" in err
    # 写入失败时不打印明文：明文作废，避免交出一个不能用的 token
    assert TOKEN_LINE.search(out) is None
    assert tokens_file.read_bytes() == before
    assert sorted(p.name for p in tokens_file.parent.iterdir()) == ["tokens.json"]


def _symlink_or_skip(target, link):
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"无法创建符号链接：{e}")


@pytest.mark.parametrize("argv", [("create", "lisi"), ("revoke", "TOKEN_ID")])
def test_refuses_symlink(capsys, tmp_path, argv):
    real = tmp_path / "real.json"
    create(capsys, real, "zhangsan")
    before = real.read_bytes()
    link = tmp_path / "tokens.json"
    _symlink_or_skip(real, link)
    argv = [records(real)[0]["token_id"] if a == "TOKEN_ID" else a for a in argv]

    code, out, err = run(capsys, "-f", str(link), *argv)

    assert code == 1
    assert "不是普通文件" in err
    assert TOKEN_LINE.search(out) is None
    assert os.path.islink(link)
    assert real.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["real.json", "tokens.json"]


@posix_only
def test_new_file_has_mode_0600(capsys, tokens_file):
    old = os.umask(0o022)
    try:
        create(capsys, tokens_file, "zhangsan")
    finally:
        os.umask(old)

    assert stat.S_IMODE(os.stat(tokens_file).st_mode) == 0o600


@posix_only
def test_rewrite_tightens_mode_to_0600(capsys, tokens_file):
    create(capsys, tokens_file, "zhangsan")
    os.chmod(tokens_file, 0o644)

    create(capsys, tokens_file, "lisi")

    assert stat.S_IMODE(os.stat(tokens_file).st_mode) == 0o600
    st = os.stat(tokens_file)
    assert (st.st_uid, st.st_gid) == (os.geteuid(), os.getegid())


# ---------------------------------------------------------------------------
# 与 server/config.py 的兼容
# ---------------------------------------------------------------------------


def test_output_accepted_by_parse_tokens(capsys, tokens_file):
    t1, _ = create(capsys, tokens_file, "zhangsan")
    create(capsys, tokens_file, "李四")
    run(capsys, "-f", str(tokens_file), "revoke", records(tokens_file)[1]["token_id"])

    parsed = parse_tokens(tokens_file.read_bytes())

    assert [(r.user, r.revoked) for r in parsed] == [("zhangsan", False), ("李四", True)]
    assert parsed[0].sha256 == tcli.hash_token(t1)
    assert tcli.serialize_tokens(parsed) == tokens_file.read_bytes()


def test_output_accepted_by_load_config(capsys, tmp_path):
    env = Env(tmp_path)
    path = env.files["tokens"]
    token, _ = create(capsys, path, "wangwu")
    run(capsys, "-f", str(path), "revoke", records(path)[0]["token_id"])

    c = load_config(env.write())

    assert len(c.tokens) == 3
    assert [(r.token_id, r.revoked) for r in c.tokens[:2]] == [("a1b2c3d4", True), ("e5f6", True)]
    assert c.tokens[2].user == "wangwu"
    assert c.tokens[2].sha256 == tcli.hash_token(token)
    assert c.tokens[2].revoked is False
