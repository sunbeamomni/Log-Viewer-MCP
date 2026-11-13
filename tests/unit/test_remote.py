"""server/remote.py 的 SSH 连接层（任务 6.1）。

用进程内的 asyncssh 服务端代替游戏服：真实的 SSH 握手、主机公钥校验、公钥认证与会话。
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import asyncssh
import pytest
import pytest_asyncio

from log_viewer_mcp.common.errors import ErrorCode, LogViewerError
from log_viewer_mcp.server import remote
from log_viewer_mcp.server.config import ConfigError, SshConfig
from log_viewer_mcp.server.remote import RemoteExit, SshConnector, is_fixed_command

HOST = "127.0.0.1"

LISTING_CALLS: list[tuple[str, bytes]] = []


async def _handle(process: asyncssh.SSHServerProcess) -> None:
    """测试用的远端程序：按命令行决定行为。"""
    cmd = process.command or ""
    data = await process.stdin.read()
    if cmd == "echo":
        process.stdout.write(data)
        process.exit(0)
    elif cmd == "stderr":
        process.stderr.write(b"E" * 10000)
        process.stdout.write(b"ok")
        process.exit(3)
    elif cmd == "big":
        for _ in range(64):
            process.stdout.write(b"x" * 65536)
            await process.stdout.drain()
        process.exit(0)
    elif cmd == "session":
        info = {"term_type": process.get_terminal_type(), "env": {str(k): str(v) for k, v in process.env.items()},
                "stdin": data.decode()}
        process.stdout.write(json.dumps(info).encode())
        process.exit(0)
    elif cmd == "signal":
        process.exit_with_signal("KILL")
    elif cmd.startswith("env -i PATH=/usr/bin:/bin LC_ALL=C timeout -s KILL "):
        # ① 的替身：记录命令行与脚本，按脚本中的目录回复 NUL 记录
        LISTING_CALLS.append((cmd, data))
        process.stdout.write(b"bashrc noise\nL 1 2 fake.log\n\0")
        if b"/missing" in data:
            process.stdout.write(b"EXIT L 2\0EXIT Z 0\0")
        else:
            process.stdout.write(b"IONICE\0L 11 22 smartfox.log\0EXIT L 0\0Z 33 smartfox.log.2026-09-17.tar.gz\0EXIT Z 0\0")
        process.exit(0)
    else:
        process.exit(127)


@pytest.fixture
def keys() -> dict[str, asyncssh.SSHKey]:
    return {name: asyncssh.generate_private_key("ssh-ed25519") for name in ("host", "other_host", "client", "other_client")}


@pytest_asyncio.fixture
async def ssh_server(keys) -> AsyncIterator[int]:
    authorized = asyncssh.import_authorized_keys(keys["client"].export_public_key().decode())
    server = await asyncssh.create_server(
        asyncssh.SSHServer, HOST, 0, server_host_keys=[keys["host"]], authorized_client_keys=authorized,
        process_factory=_handle, encoding=None,
    )
    try:
        yield server.get_port()
    finally:
        server.close()
        await server.wait_closed()


@pytest.fixture
def make_connector(tmp_path: Path, keys) -> Callable[..., SshConnector]:
    def make(port: int, *, host_key: str = "host", client_key: str = "client", kh_host: str | None = None,
             connect_timeout_s: int = 5, with_known_hosts: bool = True) -> SshConnector:
        kh: Path | None = None
        if with_known_hosts:
            pub = keys[host_key].export_public_key().decode().strip()
            kh = tmp_path / f"known_hosts_{port}_{host_key}_{kh_host}"
            kh.write_text(f"[{kh_host or HOST}]:{port} {pub}\n", encoding="utf-8")
        private = keys[client_key].export_private_key()
        cfg = SshConfig("root", port, "id_ed25519", None if kh is None else str(kh), connect_timeout_s, private)
        return SshConnector(cfg)

    return make


def _free_port() -> int:
    with socket.socket() as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


async def _run(conn: remote.RemoteConnection, command: str, script: str | bytes = b"") -> tuple[bytes, RemoteExit]:
    proc = await conn.start(command, script)
    out = b"".join([chunk async for chunk in proc])
    return out, await proc.wait()


# ---------------------------------------------------------------------------
# 成功的连接与会话
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_script_goes_to_stdin_and_stdout_is_streamed(ssh_server, make_connector) -> None:
    script = "{ printf '%s\\n' '中文 -e x'; } </dev/null\n"
    async with await make_connector(ssh_server).connect(HOST) as conn:
        out, ex = await _run(conn, "echo", script)
        assert out == script.encode("utf-8")
        assert ex == RemoteExit(0, None, 0)

        proc = await conn.start("big")
        sizes = []
        while chunk := await proc.read():
            assert len(chunk) <= remote.READ_CHUNK
            sizes.append(len(chunk))
        assert sum(sizes) == 64 * 65536 and len(sizes) > 1
        assert (await proc.wait()).exit_status == 0
    assert conn.closed


@pytest.mark.asyncio
async def test_session_has_no_pty_env_or_agent(ssh_server, make_connector, monkeypatch) -> None:
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/nonexistent/agent.sock")
    async with await make_connector(ssh_server).connect(HOST) as conn:
        out, ex = await _run(conn, "session", b"abc")
    info = json.loads(out)
    assert info == {"term_type": None, "env": {}, "stdin": "abc"}
    assert ex.exit_status == 0


@pytest.mark.asyncio
async def test_stderr_read_separately_truncated_and_logged(ssh_server, make_connector, caplog) -> None:
    caplog.set_level(logging.WARNING, logger=remote.__name__)
    async with await make_connector(ssh_server).connect(HOST) as conn:
        out, ex = await _run(conn, "stderr")
    assert out == b"ok"
    assert ex == RemoteExit(3, None, 10000)
    records = [r.getMessage() for r in caplog.records if "标准错误" in r.getMessage()]
    assert len(records) == 1
    assert "共 10000 字节，已截断" in records[0]
    assert "E" * remote.STDERR_LOG_LIMIT in records[0] and "E" * (remote.STDERR_LOG_LIMIT + 1) not in records[0]


@pytest.mark.asyncio
async def test_exit_signal_and_missing_command(ssh_server, make_connector) -> None:
    async with await make_connector(ssh_server).connect(HOST) as conn:
        assert (await _run(conn, "signal"))[1] == RemoteExit(None, "KILL", 0)
        assert (await _run(conn, "no-such-command"))[1] == RemoteExit(127, None, 0)


@pytest.mark.asyncio
async def test_start_rejects_non_fixed_command_and_closed_connection(ssh_server, make_connector) -> None:
    conn = await make_connector(ssh_server).connect(HOST)
    with pytest.raises(ValueError):
        await conn.start("echo; rm -rf /")
    await conn.close()
    await conn.close()  # 可重复调用
    with pytest.raises(LogViewerError) as ei:
        await conn.start("echo")
    assert ei.value.code is ErrorCode.SSH_FAILED


def test_is_fixed_command() -> None:
    assert is_fixed_command("env -i PATH=/usr/bin:/bin LC_ALL=C timeout -s KILL 45 nice -n 19 bash --noprofile --norc -s")
    assert is_fixed_command("kill -KILL -- -1234")
    for bad in ("", "a;b", "a'b", 'a"b', "a$b", "a`b", "a|b", "a>b", "a\nb", "a\tb", "中文", "x" * 1025, None, b"ls"):
        assert not is_fixed_command(bad), bad


@pytest.mark.asyncio
async def test_run_listing_sends_fixed_command_and_script(ssh_server, make_connector) -> None:
    LISTING_CALLS.clear()
    script = remote.build_listing_script("/usr/local/ds/SFS2X/logs", "/usr/local/ds/SFS2X/ziplogs")
    async with await make_connector(ssh_server).connect(HOST) as conn:
        listing = await remote.run_listing(conn, script, seconds=45)
        bad = remote.build_listing_script("/missing", "/usr/local/ds/SFS2X/ziplogs")
        with pytest.raises(LogViewerError) as ei:
            await remote.run_listing(conn, bad, seconds=45)
    assert ei.value.code is ErrorCode.LOG_DIR_UNAVAILABLE
    assert listing == remote.Listing(
        (remote.PlainEntry("smartfox.log", 11, 22),), (remote.ArchiveEntry("smartfox.log.2026-09-17.tar.gz", 33),),
        ionice=True,
    )
    assert LISTING_CALLS[0] == (remote.build_command(45), script.encode())


# ---------------------------------------------------------------------------
# 失败的连接
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["wrong_key", "not_registered"])
async def test_host_key_mismatch(ssh_server, make_connector, variant, caplog) -> None:
    caplog.set_level(logging.WARNING, logger=remote.__name__)
    if variant == "wrong_key":
        connector = make_connector(ssh_server, host_key="other_host")
    else:
        connector = make_connector(ssh_server, kh_host="10.9.9.9")  # known_hosts 中没有这台主机
    with pytest.raises(LogViewerError) as ei:
        await connector.connect(HOST)
    assert ei.value.code is ErrorCode.HOST_KEY_MISMATCH
    assert HOST not in ei.value.message
    assert any(HOST in r.getMessage() for r in caplog.records)  # 详情只在运行日志


@pytest.mark.asyncio
async def test_without_known_hosts_host_key_is_not_verified(ssh_server, make_connector) -> None:
    # 未配置 known_hosts：服务器的主机公钥不在任何地方登记，也能登录（8.8）
    connector = make_connector(ssh_server, with_known_hosts=False)
    assert connector._options()["known_hosts"] is None
    async with await connector.connect(HOST) as conn:
        out, ex = await _run(conn, "echo", b"hi")
    assert out == b"hi" and ex.exit_status == 0


@pytest.mark.asyncio
async def test_authentication_failure_is_ssh_failed(ssh_server, make_connector) -> None:
    with pytest.raises(LogViewerError) as ei:
        await make_connector(ssh_server, client_key="other_client").connect(HOST)
    assert ei.value.code is ErrorCode.SSH_FAILED


@pytest.mark.asyncio
async def test_connection_refused_is_ssh_failed(make_connector) -> None:
    with pytest.raises(LogViewerError) as ei:
        await make_connector(_free_port()).connect(HOST)
    assert ei.value.code is ErrorCode.SSH_FAILED


@pytest.mark.asyncio
async def test_connect_timeout_is_ssh_failed(make_connector) -> None:
    async def silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read()  # 不发送 SSH 标识行
        writer.close()

    server = await asyncio.start_server(silent, HOST, 0)
    port = server.sockets[0].getsockname()[1]
    try:
        with pytest.raises(LogViewerError) as ei:
            await make_connector(port, connect_timeout_s=1).connect(HOST)
        assert ei.value.code is ErrorCode.SSH_FAILED
    finally:
        server.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("ip", ["localhost", "127.0.0.01", "127.0.0.1\n", "::1", "", None])
async def test_invalid_ip_inner_is_not_connected(make_connector, ip, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(asyncssh, "connect", lambda *a, **k: calls.append(a))
    with pytest.raises(LogViewerError) as ei:
        await make_connector(22).connect(ip)
    assert ei.value.code is ErrorCode.INVALID_SERVER_ADDRESS
    assert calls == []


def test_connect_options_disable_agent_config_pty_and_forwarding(make_connector) -> None:
    opts = make_connector(22)._options()
    assert opts["config"] is None
    assert isinstance(opts["known_hosts"], asyncssh.SSHKnownHosts)
    assert opts["agent_path"] is None and opts["agent_forwarding"] is False
    assert opts["x11_forwarding"] is False and opts["request_pty"] is False
    assert opts["preferred_auth"] == "publickey" and len(opts["client_keys"]) == 1
    assert opts["username"] == "root" and opts["port"] == 22 and opts["connect_timeout"] == 5


# ---------------------------------------------------------------------------
# 启动检查
# ---------------------------------------------------------------------------


def test_bad_private_key_is_config_error(tmp_path: Path, keys) -> None:
    kh = tmp_path / "known_hosts"
    kh.write_text(f"{HOST} {keys['host'].export_public_key().decode()}", encoding="utf-8")
    secret = b"-----BEGIN OPENSSH PRIVATE KEY-----\nnot-a-key\n-----END OPENSSH PRIVATE KEY-----\n"
    encrypted = keys["client"].export_private_key("pkcs8-pem", passphrase="pw")
    for data in (secret, encrypted):
        with pytest.raises(ConfigError) as ei:
            SshConnector(SshConfig("root", 22, "/k/id", str(kh), 5, data))
        message = str(ei.value)
        assert "/k/id" in message and "not-a-key" not in message
        for word in ("RSA", "ECDSA", "Ed25519", "OpenSSH", "PEM", "口令"):
            assert word in message, word


@pytest.mark.parametrize(
    ("alg", "fmt"),
    [
        ("ssh-rsa", "openssh"), ("ssh-rsa", "pkcs1-pem"), ("ssh-rsa", "pkcs8-pem"),
        ("ecdsa-sha2-nistp256", "openssh"), ("ecdsa-sha2-nistp256", "pkcs1-pem"), ("ecdsa-sha2-nistp256", "pkcs8-pem"),
        ("ssh-ed25519", "openssh"), ("ssh-ed25519", "pkcs8-pem"),
    ],
)
def test_supported_private_key_types_and_formats_are_imported(alg: str, fmt: str) -> None:
    key = asyncssh.generate_private_key(alg)
    data = key.export_private_key(fmt)
    assert data.startswith(b"-----BEGIN ")
    connector = SshConnector(SshConfig("root", 22, "/k/id", None, 5, data))
    (imported,) = connector._options()["client_keys"]
    assert imported.get_algorithm() == alg
    assert imported.export_public_key() == key.export_public_key()


def test_known_hosts_option_is_none_or_imported(tmp_path: Path, keys) -> None:
    private = keys["client"].export_private_key()
    assert SshConnector(SshConfig("root", 22, "/k/id", None, 5, private))._options()["known_hosts"] is None

    kh = tmp_path / "known_hosts"
    kh.write_text(f"{HOST} {keys['host'].export_public_key().decode()}", encoding="utf-8")
    kh.chmod(0o666)  # 属主与权限位不作要求
    imported = SshConnector(SshConfig("root", 22, "/k/id", str(kh), 5, private))._options()["known_hosts"]
    assert isinstance(imported, asyncssh.SSHKnownHosts)
    trusted = imported.match(HOST, HOST, 22)[0]  # 主机名与地址各匹配一次
    assert {k.export_public_key() for k in trusted} == {keys["host"].export_public_key()}


def test_unreadable_known_hosts_is_config_error(tmp_path: Path, keys) -> None:
    private = keys["client"].export_private_key()
    with pytest.raises(ConfigError):
        SshConnector(SshConfig("root", 22, "/k/id", str(tmp_path / "missing"), 5, private))


@pytest.mark.parametrize(
    "content",
    [
        "garbage\n",  # 只有一个字段：asyncssh 抛出 ValueError
        f"{HOST} ssh-ed25519 not-base64!!\n{HOST} AAAA\n",  # 公钥无法解析：asyncssh 静默跳过，导入为空
        "# only a comment\n\n",
    ],
)
def test_known_hosts_without_usable_entries_is_config_error(tmp_path: Path, keys, content: str) -> None:
    # 10.2：配置了的 known_hosts 无法解析时拒绝启动
    private = keys["client"].export_private_key()
    kh = tmp_path / "known_hosts"
    kh.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError) as ei:
        SshConnector(SshConfig("root", 22, "/k/id", str(kh), 5, private))
    assert str(kh) in str(ei.value)


def test_known_hosts_with_one_usable_entry_among_garbage_is_imported(tmp_path: Path, keys) -> None:
    private = keys["client"].export_private_key()
    kh = tmp_path / "known_hosts"
    kh.write_text(f"10.9.9.9 ssh-ed25519 not-base64!!\n{HOST} {keys['host'].export_public_key().decode()}",
                  encoding="utf-8")
    imported = SshConnector(SshConfig("root", 22, "/k/id", str(kh), 5, private))._options()["known_hosts"]
    assert {k.export_public_key() for k in imported.match(HOST, HOST, 22)[0]} == {keys["host"].export_public_key()}
