"""Log_MCP_Server 的配置读取与启动检查（需求 6.2、6.6、10.1、10.2；design.md「Data Models」）。

读取 TOML 配置文件、凭据文件（SSH 私钥，以及配置了的数据库密码文件与 known_hosts）与
tokens.json，任一项不满足即抛出 ConfigError；``load_config_or_exit`` 把原因写到标准错误并退出。

凭据文件只检查存在、是普通文件、可读且非空；不检查属主与权限位，也不检查是否以 root
运行（服务在容器中以 root 运行）。tokens.json 不存在时 token 集合为空。
Admin_DB 连通检查由 db.py 提供，经 ``load_config_or_exit`` 的 extra_checks 传入。
"""

from __future__ import annotations

import ipaddress
import json
import os
import posixpath
import re
import stat
import sys
import tomllib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta, timezone
from typing import Any

from log_viewer_mcp.common.formats import LOG_TYPE_PATTERN

__all__ = [
    "AdminDbConfig",
    "AuditConfig",
    "Config",
    "ConfigError",
    "Limits",
    "ListenConfig",
    "LogTypeConfig",
    "ResultsConfig",
    "SshConfig",
    "TokenRecord",
    "load_config",
    "load_config_or_exit",
    "parse_server_timezone",
    "parse_tokens",
    "read_regular_file",
    "read_tokens_file",
    "timezone_label",
]

# Config_Dir 在容器中的挂载点；下面的默认路径都在其中（design.md「Data Models」）。
CONFIG_DIR = "/etc/log-viewer-mcp"
DEFAULT_TOKENS_FILE = f"{CONFIG_DIR}/tokens.json"
DEFAULT_SSH_KEY_FILE = f"{CONFIG_DIR}/ssh_key"
DEFAULT_AUDIT_DIR = "/var/log/log-viewer-mcp/audit"


class ConfigError(Exception):
    """配置或凭据不满足启动条件；说明面向运维人员，写到标准错误，不含凭据内容。"""


# ---------------------------------------------------------------------------
# 格式规则（一律 fullmatch，字符类显式写出）
# ---------------------------------------------------------------------------

# 游戏服上的目录：绝对路径，只含 [A-Za-z0-9/._-]（design.md「固定命令行」的动态值表）。
REMOTE_DIR_PATTERN = re.compile(r"/[A-Za-z0-9/._-]*")

# 服务器时区：±HH:MM 固定偏移。
TIMEZONE_PATTERN = re.compile(r"([+-])([0-9]{2}):([0-9]{2})")

# 压缩文件名中的类型后缀：空串，或以 "." 加字母开头（不会与文件名时间戳混淆）。
SUFFIX_PATTERN = re.compile(r"(\.[A-Za-z][A-Za-z0-9_-]{0,31})?")

# 监听地址为主机名时的格式。
HOSTNAME_PATTERN = re.compile(r"[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")

TOKEN_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")
SHA256_HEX_PATTERN = re.compile(r"[0-9a-f]{64}")

# 不含 Unicode Cc 类（控制）字符、非空的普通文本。
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ListenConfig:
    address: str
    port: int


@dataclass(frozen=True)
class AdminDbConfig:
    """password 为实际使用的密码：直接取自 admin_db.password，或读自 password_file（此时 password_file 不为 None）。"""

    host: str
    port: int
    user: str
    database: str
    password_file: str | None
    password: str = field(repr=False)


@dataclass(frozen=True)
class SshConfig:
    """known_hosts 为 None 表示未配置（不校验游戏服主机公钥）。"""

    user: str
    port: int
    key_file: str
    known_hosts: str | None
    connect_timeout_s: int
    private_key: bytes = field(repr=False)


@dataclass(frozen=True)
class Limits:
    """默认值见 requirements.md「可配置参数」。"""

    max_servers: int = 10
    max_time_span_h: int = 24
    default_lookback_h: int = 1
    point_window_min: int = 5
    max_patterns: int = 10
    max_pattern_length: int = 256
    max_context_lines: int = 100
    query_timeout_s: int = 40
    max_scan_bytes: int = 8 * 1024**3
    max_concurrent: int = 8
    max_concurrent_per_server: int = 2

    @property
    def max_time_span_s(self) -> int:
        return self.max_time_span_h * 3600

    @property
    def default_lookback_s(self) -> int:
        return self.default_lookback_h * 3600

    @property
    def point_window_s(self) -> int:
        return self.point_window_min * 60


@dataclass(frozen=True)
class ResultsConfig:
    dir: str = "/tmp/query_log"
    ttl_min: int = 60
    cleanup_interval_min: int = 5


@dataclass(frozen=True)
class AuditConfig:
    dir: str = DEFAULT_AUDIT_DIR
    retention_days: int = 90


@dataclass(frozen=True)
class LogTypeConfig:
    """一种日志类型：dir 为非压缩日志目录；zip_dir 为该类型自己的压缩目录（None 表示沿用顶层 zip_dir）。"""

    name: str
    dir: str
    suffix: str
    zip_dir: str | None = None


@dataclass(frozen=True)
class TokenRecord:
    token_id: str
    user: str
    sha256: str
    created_at: str
    revoked: bool


@dataclass(frozen=True)
class Config:
    listen: ListenConfig
    server_timezone: str
    tz: timezone
    tokens_file: str
    tokens: tuple[TokenRecord, ...]
    admin_db: AdminDbConfig
    ssh: SshConfig
    zip_dir: str | None
    limits: Limits
    results: ResultsConfig
    audit: AuditConfig
    log_types: Mapping[str, LogTypeConfig]

    @property
    def timezone_label(self) -> str:
        """摘要中注明的服务器时区，例如 GMT-2。"""
        return timezone_label(self.tz)

    @property
    def active_tokens(self) -> tuple[TokenRecord, ...]:
        return tuple(t for t in self.tokens if not t.revoked)

    def zip_dir_for(self, log_type: LogTypeConfig) -> str | None:
        """该类型实际使用的压缩目录：类型自己的 zip_dir，否则顶层 zip_dir；都没配置时为 None（不读压缩日志）。"""
        return log_type.zip_dir if log_type.zip_dir is not None else self.zip_dir


# ---------------------------------------------------------------------------
# 时区
# ---------------------------------------------------------------------------


def parse_server_timezone(value: Any) -> timezone:
    """把 ±HH:MM 解析为固定偏移时区；不接受 IANA 名（Etc/GMT-2 表示 UTC+2，容易写反）。"""
    if type(value) is not str:
        raise ConfigError("server_timezone 必须是 ±HH:MM 形式的字符串，例如 \"-02:00\"")
    m = TIMEZONE_PATTERN.fullmatch(value)
    if m is None:
        raise ConfigError(f"server_timezone 必须是 ±HH:MM 形式，例如 \"-02:00\"，实际为 {value!r}")
    sign, hh, mm = m.group(1), int(m.group(2)), int(m.group(3))
    if hh > 23 or mm > 59:
        raise ConfigError(f"server_timezone 超出范围：{value!r}")
    offset = timedelta(hours=hh, minutes=mm)
    return timezone(-offset if sign == "-" else offset)


def timezone_label(tz: timezone) -> str:
    """固定偏移时区的 GMT 写法：-02:00 → GMT-2，+05:30 → GMT+5:30，+00:00 → GMT。"""
    offset = tz.utcoffset(None)
    total_min = int(offset.total_seconds()) // 60
    if total_min == 0:
        return "GMT"
    sign = "-" if total_min < 0 else "+"
    hh, mm = divmod(abs(total_min), 60)
    return f"GMT{sign}{hh}" if mm == 0 else f"GMT{sign}{hh}:{mm:02d}"


# ---------------------------------------------------------------------------
# 凭据文件与 tokens.json 的读取
# ---------------------------------------------------------------------------


def read_regular_file(path: str, what: str) -> bytes:
    """读取一个普通文件，返回内容；不存在、不是普通文件或不可读时抛出 ConfigError。

    不检查属主与权限位（10.2）。
    """
    data = _read_file(path, what, missing_ok=False)
    assert data is not None
    return data


def read_tokens_file(path: str) -> tuple[TokenRecord, ...]:
    """读取并解析 tokens.json；文件不存在时 token 集合为空（6.6），存在但无法解析时抛出 ConfigError。"""
    data = _read_file(path, "token 登记文件", missing_ok=True)
    return () if data is None else parse_tokens(data, path)


def _read_file(path: str, what: str, *, missing_ok: bool) -> bytes | None:
    """先打开再对同一个文件描述符做 fstat，避免检查与读取之间被替换；O_NONBLOCK 防止指向 FIFO 时阻塞。"""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError as e:
        if missing_ok:
            return None
        raise ConfigError(f"{what} {path} 不存在：{e.strerror}") from None
    except OSError as e:
        raise ConfigError(f"{what} {path} 不可读：{e.strerror}") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ConfigError(f"{what} {path} 不是普通文件")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
    except OSError as e:
        raise ConfigError(f"{what} {path} 不可读：{e.strerror}") from None
    finally:
        os.close(fd)
    return b"".join(chunks)


# ---------------------------------------------------------------------------
# tokens.json
# ---------------------------------------------------------------------------

_TOKEN_KEYS = frozenset({"token_id", "user", "sha256", "created_at", "revoked"})


def parse_tokens(data: bytes, source: str = "tokens.json") -> tuple[TokenRecord, ...]:
    """解析 tokens.json：数组，每项含 token_id、user、sha256、created_at、revoked。"""
    try:
        items = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ConfigError(f"{source} 不是合法的 UTF-8 JSON：{e}") from None
    if type(items) is not list:
        raise ConfigError(f"{source} 顶层必须是数组")
    records: list[TokenRecord] = []
    seen: set[str] = set()
    for i, item in enumerate(items):
        where = f"{source} 第 {i + 1} 项"
        if type(item) is not dict:
            raise ConfigError(f"{where} 必须是对象")
        keys = set(item)
        if keys != _TOKEN_KEYS:
            missing = sorted(_TOKEN_KEYS - keys)
            extra = sorted(keys - _TOKEN_KEYS)
            parts = []
            if missing:
                parts.append("缺少 " + "、".join(missing))
            if extra:
                parts.append("多出 " + "、".join(extra))
            raise ConfigError(f"{where}：{'；'.join(parts)}")
        token_id, user, digest = item["token_id"], item["user"], item["sha256"]
        created_at, revoked = item["created_at"], item["revoked"]
        if type(token_id) is not str or TOKEN_ID_PATTERN.fullmatch(token_id) is None:
            raise ConfigError(f"{where}：token_id 必须是 1 至 64 个字母、数字、\"_\" 或 \"-\"")
        if token_id in seen:
            raise ConfigError(f"{where}：token_id {token_id} 重复")
        seen.add(token_id)
        if not _is_plain_text(user):
            raise ConfigError(f"{where}：user 必须是不含控制字符的非空字符串")
        if type(digest) is not str or SHA256_HEX_PATTERN.fullmatch(digest) is None:
            raise ConfigError(f"{where}：sha256 必须是 64 位小写十六进制")
        if not _is_plain_text(created_at):
            raise ConfigError(f"{where}：created_at 必须是非空字符串")
        if type(revoked) is not bool:
            raise ConfigError(f"{where}：revoked 必须是布尔值")
        records.append(TokenRecord(token_id, user, digest, created_at, revoked))
    return tuple(records)


def _is_plain_text(value: Any) -> bool:
    return type(value) is str and value != "" and _CONTROL_CHARS.search(value) is None


# ---------------------------------------------------------------------------
# TOML 表的读取
# ---------------------------------------------------------------------------

_REQUIRED = object()


class _Table:
    """按名读取一个 TOML 表的项，类型不对或缺项时抛出 ConfigError，最后检查未知项。"""

    def __init__(self, data: Any, prefix: str) -> None:
        if type(data) is not dict:
            raise ConfigError(f"配置项 {prefix or '顶层'} 必须是表")
        self._data: dict[str, Any] = data
        self._prefix = prefix
        self._used: set[str] = set()

    def name(self, key: str) -> str:
        return f"{self._prefix}.{key}" if self._prefix else key

    def raw(self, key: str, default: Any = _REQUIRED) -> Any:
        self._used.add(key)
        if key not in self._data:
            if default is _REQUIRED:
                raise ConfigError(f"缺少配置项 {self.name(key)}")
            return default
        return self._data[key]

    def has(self, key: str) -> bool:
        return key in self._data

    def table(self, key: str, *, required: bool = True) -> _Table:
        value = self.raw(key, _REQUIRED if required else {})
        return _Table(value, self.name(key))

    def text(self, key: str, default: Any = _REQUIRED) -> str:
        value = self.raw(key, default)
        if not _is_plain_text(value):
            raise ConfigError(f"配置项 {self.name(key)} 必须是不含控制字符的非空字符串")
        return value

    def integer(self, key: str, default: Any = _REQUIRED, *, lo: int = 1, hi: int | None = None) -> int:
        value = self.raw(key, default)
        if type(value) is not int or value < lo or (hi is not None and value > hi):
            bound = f" {lo} 至 {hi} " if hi is not None else f"不小于 {lo} "
            raise ConfigError(f"配置项 {self.name(key)} 必须是{bound}的整数")
        return value

    def local_path(self, key: str, default: Any = _REQUIRED) -> str:
        value = self.text(key, default)
        # 跳板机是 Linux，两者等价；在 Windows 上另接受 POSIX 绝对路径，便于开发机导入与测试。
        if not (os.path.isabs(value) or posixpath.isabs(value)):
            raise ConfigError(f"配置项 {self.name(key)} 必须是绝对路径：{value!r}")
        return value

    def remote_dir(self, key: str) -> str:
        value = self.raw(key)
        if type(value) is not str or REMOTE_DIR_PATTERN.fullmatch(value) is None:
            raise ConfigError(
                f"配置项 {self.name(key)} 必须是只含字母、数字、\"/\"、\".\"、\"_\"、\"-\" 的绝对路径：{value!r}"
            )
        if ".." in value.split("/"):
            raise ConfigError(f"配置项 {self.name(key)} 不能含 \"..\"：{value!r}")
        return value

    def keys(self) -> list[str]:
        return list(self._data)

    def finish(self) -> None:
        unknown = [k for k in self._data if k not in self._used]
        if unknown:
            names = "、".join(self.name(k) for k in unknown)
            raise ConfigError(f"未知的配置项 {names}")


def _listen_address(t: _Table, key: str) -> str:
    value = t.text(key)
    try:
        ipaddress.ip_address(value)
    except ValueError:
        if HOSTNAME_PATTERN.fullmatch(value) is None:
            raise ConfigError(f"配置项 {t.name(key)} 不是合法的 IP 地址或主机名：{value!r}") from None
    return value


# ---------------------------------------------------------------------------
# 读取与检查
# ---------------------------------------------------------------------------

def load_config(path: str) -> Config:
    """读取配置文件、凭据与 tokens.json，并做启动检查（不检查属主、权限位与是否以 root 运行）。"""
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except OSError as e:
        raise ConfigError(f"配置文件 {path} 不可读：{e.strerror}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"配置文件 {path} 不是合法的 TOML：{e}") from None

    root = _Table(data, "")

    lt = root.table("listen")
    listen = ListenConfig(
        address=_listen_address(lt, "address"),
        port=lt.integer("port", lo=1, hi=65535),
    )
    lt.finish()

    server_timezone = root.raw("server_timezone", "-02:00")
    tz = parse_server_timezone(server_timezone)

    tokens_file = root.local_path("tokens_file", DEFAULT_TOKENS_FILE)

    dt = root.table("admin_db")
    db_host = dt.text("host")
    db_port = dt.integer("port", 3306, lo=1, hi=65535)
    db_user = dt.text("user")
    db_name = dt.text("database")
    inline_password = _inline_password(dt)
    password_file = dt.local_path("password_file") if dt.has("password_file") else None
    if (inline_password is None) == (password_file is None):
        raise ConfigError("admin_db 中 password 与 password_file 须恰好给出一项")
    dt.finish()

    st = root.table("ssh", required=False)
    ssh_user = st.text("user", "root")
    ssh_port = st.integer("port", 22, lo=1, hi=65535)
    key_file = st.local_path("key_file", DEFAULT_SSH_KEY_FILE)
    known_hosts = st.local_path("known_hosts") if st.has("known_hosts") else None
    connect_timeout_s = st.integer("connect_timeout_s", 10, lo=1)
    st.finish()

    # 可省略：没有压缩日志的环境不配置，各类型也可以在 [log_types] 中单独配置 zip_dir。
    zip_dir = root.remote_dir("zip_dir") if root.has("zip_dir") else None

    limits = _read_limits(root.table("limits", required=False))

    rt = root.table("results", required=False)
    d = ResultsConfig()
    results = ResultsConfig(
        dir=rt.local_path("dir", d.dir),
        ttl_min=rt.integer("ttl_min", d.ttl_min),
        cleanup_interval_min=rt.integer("cleanup_interval_min", d.cleanup_interval_min),
    )
    rt.finish()

    at = root.table("audit", required=False)
    da = AuditConfig()
    audit = AuditConfig(
        dir=at.local_path("dir", da.dir),
        retention_days=at.integer("retention_days", da.retention_days),
    )
    at.finish()

    log_types = _read_log_types(root.table("log_types"), zip_dir)

    root.finish()

    # 凭据文件：存在、是普通文件、可读且非空；不检查属主与权限位（10.2）。
    if password_file is not None:
        password = _decode_password(read_regular_file(password_file, "数据库密码文件"), password_file)
    else:
        assert inline_password is not None
        password = inline_password
    private_key = read_regular_file(key_file, "SSH 私钥文件")
    if not private_key.strip():
        raise ConfigError(f"SSH 私钥文件 {key_file} 为空")
    if known_hosts is not None and not read_regular_file(known_hosts, "known_hosts 文件").strip():
        raise ConfigError(f"known_hosts 文件 {known_hosts} 为空")
    # tokens.json 不存在时 token 集合为空，存在但无法解析时拒绝启动（6.6）。
    tokens = read_tokens_file(tokens_file)

    return Config(
        listen=listen,
        server_timezone=server_timezone,
        tz=tz,
        tokens_file=tokens_file,
        tokens=tokens,
        admin_db=AdminDbConfig(db_host, db_port, db_user, db_name, password_file, password),
        ssh=SshConfig(ssh_user, ssh_port, key_file, known_hosts, connect_timeout_s, private_key),
        zip_dir=zip_dir,
        limits=limits,
        results=results,
        audit=audit,
        log_types=log_types,
    )


def _read_limits(t: _Table) -> Limits:
    d = Limits()
    limits = Limits(
        max_servers=t.integer("max_servers", d.max_servers),
        max_time_span_h=t.integer("max_time_span_h", d.max_time_span_h),
        default_lookback_h=t.integer("default_lookback_h", d.default_lookback_h),
        point_window_min=t.integer("point_window_min", d.point_window_min),
        max_patterns=t.integer("max_patterns", d.max_patterns),
        max_pattern_length=t.integer("max_pattern_length", d.max_pattern_length),
        max_context_lines=t.integer("max_context_lines", d.max_context_lines, lo=0),
        query_timeout_s=t.integer("query_timeout_s", d.query_timeout_s),
        max_scan_bytes=t.integer("max_scan_bytes", d.max_scan_bytes),
        max_concurrent=t.integer("max_concurrent", d.max_concurrent),
        max_concurrent_per_server=t.integer("max_concurrent_per_server", d.max_concurrent_per_server),
    )
    t.finish()
    # 不满足时默认查询（回溯）或时间点查询必然因超过跨度被拒绝。
    if limits.default_lookback_s > limits.max_time_span_s:
        raise ConfigError("limits.default_lookback_h 不能大于 limits.max_time_span_h")
    if 2 * limits.point_window_s > limits.max_time_span_s:
        raise ConfigError("limits.point_window_min 的两倍不能超过 limits.max_time_span_h")
    return limits


def _read_log_types(t: _Table, default_zip_dir: str | None) -> dict[str, LogTypeConfig]:
    names = t.keys()
    if not names:
        raise ConfigError("[log_types] 至少要定义一种日志类型")
    result: dict[str, LogTypeConfig] = {}
    # 同一压缩目录中各类型靠 suffix 区分，所以 suffix 只须在共用同一压缩目录的类型之间唯一。
    suffixes: dict[tuple[str, str], str] = {}
    for name in names:
        if LOG_TYPE_PATTERN.fullmatch(name) is None:
            raise ConfigError(
                f"[log_types] 的键 {name!r} 不合法：须以小写字母开头，只含小写字母、数字与 \"_\"，最长 32 个字符"
            )
        item = t.table(name)
        directory = item.remote_dir("dir")
        suffix = item.raw("suffix", "")
        if type(suffix) is not str or SUFFIX_PATTERN.fullmatch(suffix) is None:
            raise ConfigError(
                f"配置项 {item.name('suffix')} 必须为空串，或以 \".\" 加字母开头、只含字母、数字、\"_\"、\"-\"：{suffix!r}"
            )
        own_zip_dir = item.remote_dir("zip_dir") if item.has("zip_dir") else None
        item.finish()
        effective_zip_dir = own_zip_dir if own_zip_dir is not None else default_zip_dir
        if effective_zip_dir is not None:
            key = (effective_zip_dir.rstrip("/") or "/", suffix)
            if key in suffixes:
                raise ConfigError(
                    f"[log_types] 中 {suffixes[key]} 与 {name} 使用同一压缩目录 {effective_zip_dir}，suffix 却相同：{suffix!r}"
                )
            suffixes[key] = name
        result[name] = LogTypeConfig(name=name, dir=directory, suffix=suffix, zip_dir=own_zip_dir)
    t.finish()
    return result


def _inline_password(t: _Table) -> str | None:
    """admin_db.password：未给出时为 None；给出时须为非空字符串。错误说明不含密码值。"""
    if not t.has("password"):
        return None
    value = t.raw("password")
    if type(value) is not str or value == "":
        raise ConfigError(f"配置项 {t.name('password')} 必须是非空字符串")
    return value


def _decode_password(raw: bytes, path: str) -> str:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigError(f"数据库密码文件 {path} 不是 UTF-8 文本") from None
    # 只去掉末尾的换行（编辑器常会加上），密码中的其他字符原样保留。
    password = text.rstrip("\r\n")
    if password == "":
        raise ConfigError(f"数据库密码文件 {path} 为空")
    if "\n" in password or "\r" in password:
        raise ConfigError(f"数据库密码文件 {path} 只能有一行")
    return password


def load_config_or_exit(
    path: str,
    *,
    extra_checks: Iterable[Callable[[Config], None]] = (),
) -> Config:
    """启动检查：失败时在标准错误中说明原因并以退出码 2 退出（10.2）。

    extra_checks 为附加检查（例如 Admin_DB 连通检查），失败时应抛出 ConfigError。
    """
    try:
        config = load_config(path)
        for check in extra_checks:
            check(config)
    except ConfigError as e:
        print(f"log-viewer-mcp 启动失败：{e}", file=sys.stderr)
        raise SystemExit(2) from None
    return config
