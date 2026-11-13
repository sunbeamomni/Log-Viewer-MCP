"""集成测试的环境、服务配置与游戏服操作（任务 5.2）。

只在 compose 的测试运行容器中使用（见 README.md）；conftest.py 把这里的对象包装成夹具。

- ItEnv：测试运行容器的 LVM_IT_* 环境变量。
- write_service_files：在临时目录中生成 Log_MCP_Server 的 config.toml、凭据副本（数据库密码、SSH 私钥、
  known_hosts，均可省去）与 tokens.json，可直接交给 config.load_config（不要求属主与权限位）。
- GameServers：以 root 登录游戏服执行测试用的固定脚本（不经过被测代码），提供只读快照
  （snapshot、diff_snapshots、assert_unchanged）、远端 manifest 与样例恢复（reset_samples）。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import secrets
import socket
from collections.abc import AsyncIterator, Coroutine, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

from tests.integration import sample_logs

T = TypeVar("T")

__all__ = [
    "CLEAN_SHELL",
    "SNAPSHOT_ROOTS",
    "GameServers",
    "ItEnv",
    "RemoteResult",
    "ServiceFiles",
    "SnapEntry",
    "Snapshot",
    "diff_snapshots",
    "it_enabled",
    "run_sync",
    "write_service_files",
]


def it_enabled() -> bool:
    return os.environ.get("LVM_IT") == "1"


def run_sync(coro: Coroutine[Any, Any, T]) -> T:
    """在同步测试（例如 Hypothesis 属性测试）中运行协程；不能在已运行的事件循环中调用。"""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 环境变量
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ItEnv:
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str = field(repr=False)
    ssh_user: str
    ssh_port: int
    ssh_key: str
    known_hosts: str
    known_hosts_wrong: str
    gs_ip: str
    gs_noionice_ip: str
    gs_grepspy_ip: str
    gs_unreachable_ip: str
    grep_argv_dir: str

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> ItEnv:
        e = os.environ if environ is None else environ

        def get(name: str) -> str:
            value = e.get(name)
            if not value:
                raise RuntimeError(f"测试运行容器缺少环境变量 {name}（见 tests/integration/README.md）")
            return value

        return cls(
            db_host=get("LVM_IT_DB_HOST"),
            db_port=int(get("LVM_IT_DB_PORT")),
            db_name=get("LVM_IT_DB_NAME"),
            db_user=get("LVM_IT_DB_USER"),
            db_password=get("LVM_IT_DB_PASSWORD"),
            ssh_user=get("LVM_IT_SSH_USER"),
            ssh_port=int(get("LVM_IT_SSH_PORT")),
            ssh_key=get("LVM_IT_SSH_KEY"),
            known_hosts=get("LVM_IT_KNOWN_HOSTS"),
            known_hosts_wrong=get("LVM_IT_KNOWN_HOSTS_WRONG"),
            gs_ip=get("LVM_IT_GS_IP"),
            gs_noionice_ip=get("LVM_IT_GS_NOIONICE_IP"),
            gs_grepspy_ip=get("LVM_IT_GS_GREPSPY_IP"),
            gs_unreachable_ip=get("LVM_IT_GS_UNREACHABLE_IP"),
            grep_argv_dir=get("LVM_IT_GREP_ARGV_DIR"),
        )

    @property
    def game_servers(self) -> dict[str, str]:
        """可以登录的游戏服：名称 → IP。"""
        return {"gs": self.gs_ip, "gs-noionice": self.gs_noionice_ip, "gs-grepspy": self.gs_grepspy_ip}


# ---------------------------------------------------------------------------
# 服务配置与凭据
# ---------------------------------------------------------------------------

DEFAULT_LIMITS: dict[str, int] = {
    "max_servers": 10,
    "max_time_span_h": 24,
    "default_lookback_h": 1,
    "point_window_min": 5,
    "max_patterns": 10,
    "max_pattern_length": 256,
    "max_context_lines": 100,
    "query_timeout_s": 40,
    "max_scan_bytes": 8 * 1024**3,
    "max_concurrent": 8,
    "max_concurrent_per_server": 2,
}


@dataclass(frozen=True)
class ServiceFiles:
    """write_service_files 生成的文件；token 是 tokens.json 中登记的有效 token 明文。"""

    dir: Path
    config_path: Path
    tokens_file: Path
    password_file: Path | None  # None：密码直接写在 admin_db.password 中
    key_file: Path
    known_hosts: Path | None  # None：未配置 ssh.known_hosts
    results_dir: Path
    audit_dir: Path
    listen_address: str
    listen_port: int
    token: str = field(repr=False)
    token_id: str
    user: str
    revoked_token: str = field(repr=False)

    @property
    def base_url(self) -> str:
        return f"http://{self.listen_address}:{self.listen_port}"

    def load(self):
        """按服务的启动检查读取配置（log_viewer_mcp.server.config.load_config）。"""
        from log_viewer_mcp.server.config import load_config

        return load_config(str(self.config_path))


def free_port(address: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((address, 0))
        return s.getsockname()[1]


def _toml(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)  # JSON 字符串转义与 TOML 基本字符串兼容
    if isinstance(value, Mapping):
        return "{ " + ", ".join(f"{k} = {_toml(v)}" for k, v in value.items()) + " }"
    raise TypeError(type(value))


def token_record(token: str, *, token_id: str, user: str, revoked: bool = False) -> dict[str, Any]:
    return {
        "token_id": token_id,
        "user": user,
        "sha256": hashlib.sha256(token.encode("ascii")).hexdigest(),
        "created_at": datetime.now(sample_logs.SERVER_TZ).isoformat(timespec="seconds"),
        "revoked": revoked,
    }


def write_service_files(
    directory: Path,
    env: ItEnv,
    *,
    known_hosts: str | None = "good",
    inline_password: bool = False,
    limits: Mapping[str, int] | None = None,
    listen_address: str = "127.0.0.1",
    listen_port: int | None = None,
    log_types: Mapping[str, Mapping[str, str]] | None = None,
) -> ServiceFiles:
    """在 directory（应为空的临时目录）中生成服务配置与凭据副本。

    凭据以普通方式写入（权限取决于 umask），服务不检查属主与权限位。
    known_hosts 为 "good"（正确的主机公钥）、"wrong"（同样的 IP 配另一把公钥）或 None（不配置）。
    inline_password 为真时密码直接写在 admin_db.password 中，否则写入 password_file。
    """
    directory.mkdir(parents=True, exist_ok=True)

    password_file: Path | None = None
    db: dict[str, Any] = {"host": env.db_host, "port": env.db_port, "user": env.db_user, "database": env.db_name}
    if inline_password:
        db["password"] = env.db_password
    else:
        password_file = directory / "db_password"
        password_file.write_bytes(env.db_password.encode("utf-8") + b"\n")
        db["password_file"] = str(password_file)
    key_file = directory / "id_ed25519"
    key_file.write_bytes(Path(env.ssh_key).read_bytes())
    ssh: dict[str, Any] = {"user": env.ssh_user, "port": env.ssh_port, "key_file": str(key_file), "connect_timeout_s": 5}
    kh_file: Path | None = None
    if known_hosts is not None:
        kh_source = {"good": env.known_hosts, "wrong": env.known_hosts_wrong}[known_hosts]
        kh_file = directory / "known_hosts"
        kh_file.write_bytes(Path(kh_source).read_bytes())
        ssh["known_hosts"] = str(kh_file)

    token = secrets.token_urlsafe(32)
    revoked_token = secrets.token_urlsafe(32)
    token_id = secrets.token_hex(4)
    user = "it-tester"
    tokens = [
        token_record(token, token_id=token_id, user=user),
        token_record(revoked_token, token_id=secrets.token_hex(4), user="it-revoked", revoked=True),
    ]
    tokens_file = directory / "tokens.json"
    tokens_file.write_bytes(json.dumps(tokens, ensure_ascii=False, indent=1).encode("utf-8"))

    results_dir = directory / "results"  # 由服务启动时以 0700 创建
    audit_dir = directory / "audit"
    audit_dir.mkdir(mode=0o700, exist_ok=True)
    port = listen_port if listen_port is not None else free_port(listen_address)

    merged_limits = dict(DEFAULT_LIMITS)
    merged_limits.update(limits or {})
    types = log_types if log_types is not None else {
        name: {"dir": spec.dir, "suffix": spec.suffix} for name, spec in sample_logs.LOG_TYPES.items()
    }

    lines = [
        "# 集成测试生成的配置（tests/integration/itenv.py）",
        f"listen = {_toml({'address': listen_address, 'port': port})}",
        f"server_timezone = {_toml(sample_logs.SERVER_TZ_TEXT)}",
        f"tokens_file = {_toml(str(tokens_file))}",
        "admin_db = " + _toml(db),
        "ssh = " + _toml(ssh),
        f"zip_dir = {_toml(sample_logs.ZIP_DIR)}",
        f"limits = {_toml(merged_limits)}",
        "results = " + _toml({"dir": str(results_dir), "ttl_min": 60, "cleanup_interval_min": 5}),
        "audit = " + _toml({"dir": str(audit_dir), "retention_days": 90}),
        "[log_types]",
        *(f"{name} = {_toml(dict(spec))}" for name, spec in types.items()),
    ]
    config_path = directory / "config.toml"
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return ServiceFiles(
        dir=directory, config_path=config_path, tokens_file=tokens_file, password_file=password_file,
        key_file=key_file, known_hosts=kh_file, results_dir=results_dir, audit_dir=audit_dir,
        listen_address=listen_address, listen_port=port, token=token, token_id=token_id, user=user,
        revoked_token=revoked_token,
    )


# ---------------------------------------------------------------------------
# 游戏服操作（测试自己的固定脚本，不经过被测代码）
# ---------------------------------------------------------------------------

# 与 design.md「固定命令行」一样清掉登录 Shell 设置的环境变量（~/.bashrc 设置了 GREP_OPTIONS 等）。
CLEAN_SHELL = "env -i PATH=/usr/bin:/bin LC_ALL=C bash --noprofile --norc -s"

# 只读快照的范围（需求 8.10）：日志目录与 ziplogs（都在 SFS2X 下）、/tmp、root 主目录。
SNAPSHOT_ROOTS = (sample_logs.SFS2X_ROOT, "/tmp", "/root")


@dataclass(frozen=True)
class RemoteResult:
    exit_status: int | None
    stdout: bytes  # 已去掉登录 Shell 在脚本输出之前打印的内容
    stderr: bytes


@dataclass(frozen=True)
class SnapEntry:
    type: str  # find 的 %y：f 普通文件、d 目录、l 符号链接……
    mode: str  # 八进制权限位
    uid: int
    gid: int
    size: int
    mtime: str  # find 的 %T@（含小数部分）
    link: str  # 符号链接目标
    sha256: str | None  # 普通文件的内容摘要


@dataclass(frozen=True)
class Snapshot:
    host: str
    entries: Mapping[str, SnapEntry]


def diff_snapshots(before: Snapshot, after: Snapshot) -> list[str]:
    """比较文件集合、内容、大小、权限、属主与 mtime（不比较 atime），返回差异说明。"""
    diffs: list[str] = []
    for path in sorted(after.entries.keys() - before.entries.keys()):
        diffs.append(f"新增 {path!r}")
    for path in sorted(before.entries.keys() - after.entries.keys()):
        diffs.append(f"删除 {path!r}")
    for path in sorted(before.entries.keys() & after.entries.keys()):
        a, b = before.entries[path], after.entries[path]
        if a != b:
            fields = [f"{k}: {getattr(a, k)!r} → {getattr(b, k)!r}"
                      for k in SnapEntry.__dataclass_fields__ if getattr(a, k) != getattr(b, k)]
            diffs.append(f"修改 {path!r}（{'；'.join(fields)}）")
    return diffs


_SNAPSHOT_SCRIPT = (
    "{ find -P " + " ".join(SNAPSHOT_ROOTS) + " -printf '%y\\0%m\\0%U\\0%G\\0%s\\0%T@\\0%l\\0%p\\0'\n"
    "  printf 'HASHES\\0'\n"
    "  find -P " + " ".join(SNAPSHOT_ROOTS) + " -type f -exec sha256sum -z -- {} +\n"
    "} </dev/null\n"
)


def parse_snapshot(host: str, payload: bytes) -> Snapshot:
    listing, sep, hashes = payload.partition(b"HASHES\0")
    if not sep:
        raise AssertionError(f"{host} 的快照输出不完整")
    fields = listing.split(b"\0")
    if fields[-1] != b"" or (len(fields) - 1) % 8:
        raise AssertionError(f"{host} 的快照列表格式不符")
    digests: dict[str, str] = {}
    for rec in hashes.split(b"\0")[:-1]:
        digest, _, name = rec.partition(b"  ")
        digests[name.decode("utf-8", "surrogateescape")] = digest.decode("ascii")
    entries: dict[str, SnapEntry] = {}
    for i in range(0, len(fields) - 1, 8):
        y, m, u, g, s, t, link, p = (f.decode("utf-8", "surrogateescape") for f in fields[i:i + 8])
        entries[p] = SnapEntry(y, m, int(u), int(g), int(s), t, link, digests.get(p) if y == "f" else None)
    return Snapshot(host, entries)


class GameServers:
    """以 root 登录游戏服执行测试脚本。每次调用新建 SSH 连接；known_hosts 严格校验。"""

    def __init__(self, env: ItEnv) -> None:
        self.env = env

    async def connect(self, host: str, *, known_hosts: str | None = None):
        import asyncssh

        key = asyncssh.import_private_key(Path(self.env.ssh_key).read_bytes())
        return await asyncssh.connect(
            host, port=self.env.ssh_port, username=self.env.ssh_user, client_keys=[key],
            known_hosts=known_hosts or self.env.known_hosts, agent_path=None, config=None,
            connect_timeout=10, login_timeout=10,
        )

    async def run(self, host: str, script: str | bytes, *, timeout: float = 120) -> RemoteResult:
        """经标准输入把 script 交给干净环境中的 bash；只返回脚本自己的输出。"""
        body = script.encode("utf-8") if isinstance(script, str) else script
        marker = secrets.token_hex(16).encode("ascii")
        full = b"printf '%s\\0' " + marker + b"\n" + body
        async with await self.connect(host) as conn:
            r = await conn.run(CLEAN_SHELL, input=full, encoding=None, check=False, timeout=timeout)
        stdout = r.stdout or b""
        head, sep, rest = stdout.partition(marker + b"\0")
        if not sep:
            raise AssertionError(f"{host} 上的脚本没有输出起始标记：{stdout[:200]!r} {r.stderr!r}")
        return RemoteResult(r.exit_status, rest, r.stderr or b"")

    async def run_ok(self, host: str, script: str | bytes, *, timeout: float = 120) -> bytes:
        r = await self.run(host, script, timeout=timeout)
        if r.exit_status != 0:
            raise AssertionError(f"{host} 上的脚本失败（{r.exit_status}）：{r.stderr.decode(errors='replace')}")
        return r.stdout

    async def snapshot(self, host: str) -> Snapshot:
        return parse_snapshot(host, await self.run_ok(host, _SNAPSHOT_SCRIPT))

    @contextlib.asynccontextmanager
    async def assert_unchanged(self, host: str) -> AsyncIterator[Snapshot]:
        """进入时拍快照，退出时再拍一次并断言没有差异（不比较 atime）。"""
        before = await self.snapshot(host)
        yield before
        after = await self.snapshot(host)
        diffs = diff_snapshots(before, after)
        assert not diffs, f"{host} 上的文件被改动：\n" + "\n".join(diffs[:50])

    async def manifest(self, host: str) -> dict[str, Any]:
        """游戏服镜像中的 manifest.json（构建镜像时由 sample_logs.py 生成）。"""
        out = await self.run_ok(host, f"cat -- {sample_logs.SAMPLES_DIR}/manifest.json </dev/null\n")
        return json.loads(out.decode("utf-8"))

    async def reset_samples(self, host: str) -> None:
        """重新运行 init.d 中的样例脚本，把样例日志恢复为镜像中的初始状态。"""
        await self.run_ok(host, "bash /opt/testenv/init.d/10-sample-logs </dev/null\n")
