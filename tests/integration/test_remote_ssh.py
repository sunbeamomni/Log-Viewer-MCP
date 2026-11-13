"""SSH 连接层的集成测试（任务 6.1、14.3）：登录 gs、未配置 known_hosts 时登录、主机公钥不符、主机不可达。"""

from __future__ import annotations

import pytest

from log_viewer_mcp.common.errors import ErrorCode, LogViewerError
from log_viewer_mcp.server.remote import SshConnector
from tests.integration.itenv import ItEnv

# 测试用的固定命令行（与 design.md「固定命令行」同样清掉登录 Shell 设置的环境变量）
COMMAND = "env -i PATH=/usr/bin:/bin LC_ALL=C bash --noprofile --norc -s"


@pytest.mark.asyncio
async def test_connect_and_run_script_on_gs(it_env: ItEnv, service_files) -> None:
    connector = SshConnector(service_files.load().ssh)
    script = "{ printf '\\0'; printf '%s|%s|%s\\n' \"$(id -u)\" \"${GREP_OPTIONS-unset}\" '中文'; printf 'err' >&2; exit 7; } </dev/null\n"
    async with await connector.connect(it_env.gs_ip) as conn:
        proc = await conn.start(COMMAND, script)
        out = b"".join([chunk async for chunk in proc])
        ex = await proc.wait()
    # ~/.bashrc 的输出在 NUL 之前，丢弃
    _, sep, body = out.partition(b"\0")
    assert sep and body == "0|unset|中文\n".encode()
    assert ex.exit_status == 7 and ex.exit_signal is None and ex.stderr_bytes >= 3


@pytest.mark.asyncio
async def test_connect_without_known_hosts(it_env: ItEnv, make_service_files) -> None:
    # 未配置 ssh.known_hosts：不校验主机公钥，照常登录（8.8）
    ssh = make_service_files(known_hosts=None).load().ssh
    assert ssh.known_hosts is None
    connector = SshConnector(ssh)
    async with await connector.connect(it_env.gs_ip) as conn:
        proc = await conn.start(COMMAND, "{ printf '\\0'; id -u; } </dev/null\n")
        out = b"".join([chunk async for chunk in proc])
        ex = await proc.wait()
    assert out.partition(b"\0")[2] == b"0\n"
    assert ex.exit_status == 0


@pytest.mark.asyncio
async def test_wrong_known_hosts_is_host_key_mismatch(it_env: ItEnv, make_service_files) -> None:
    connector = SshConnector(make_service_files(known_hosts="wrong").load().ssh)
    with pytest.raises(LogViewerError) as ei:
        await connector.connect(it_env.gs_ip)
    assert ei.value.code is ErrorCode.HOST_KEY_MISMATCH


@pytest.mark.asyncio
async def test_unreachable_host_is_ssh_failed(it_env: ItEnv, service_files) -> None:
    connector = SshConnector(service_files.load().ssh)
    with pytest.raises(LogViewerError) as ei:
        await connector.connect(it_env.gs_unreachable_ip)
    assert ei.value.code is ErrorCode.SSH_FAILED
