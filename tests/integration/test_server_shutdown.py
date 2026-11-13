"""断开取消与 SIGTERM 停机的集成测试（任务 8.7，需求 10.5、10.6）。

以子进程运行 ``python -m log_viewer_mcp.server``（同 test_server_main.py），经真实的工具接口发起一次在 gs 上
长时间运行的 search_logs，再让客户端断开或向服务发 SIGTERM，确认远端进程组已不存在。

长时间运行的查询：样例日志很小，grep 很快就读完，所以测试在 gs 上只读快照范围之外的临时目录
/var/tmp/lvm-it87-<随机>/logs 中放一个 Rotated_Log_File（smartfox.log.2026-09-17-15-30），内容是同一个 gzip 成员
（64 MiB 的重复日志行，压缩后约 64 KB）拼接 MEMBERS 次：磁盘上约 20 MB，gzip -dcf 解出约 19 GiB，远端要运行很久。
服务配置的 normal 类型指向该目录（zip_dir 仍是样例的 ziplogs，其中没有覆盖该时段的压缩包），包含模式不会匹配，
结果文件为空。远端进程组按 gzip 的命令行（含随机目录名）在 /proc 中找到，其组长应为
``timeout -s KILL <秒>``，秒数由独特的 query_timeout_s 算出（remote_seconds：剩余时长向上取整再加 5）。
结束时删除临时目录；样例目录用 game_servers.assert_unchanged 确认没有改动。
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import signal
import socket
import time
from collections.abc import Iterator
from datetime import datetime

import httpx
import pytest

from tests.integration import sample_logs
from tests.integration.itenv import GameServers, ItEnv, ServiceFiles, run_sync
from tests.integration.test_remote_exec import _assert_group_gone
from tests.integration.test_server_main import HANDLE, RunningServer

QUERY_TIMEOUT_S = 53  # 独特的值：远端 timeout 的秒数在 54 至 58 之间（① 为 58）
REMOTE_SECONDS = range(QUERY_TIMEOUT_S + 1, QUERY_TIMEOUT_S + 6)
TS = "2026-09-17-15-30"
MEMBER_BYTES = 64 * 1024 * 1024
MEMBERS = 300
NEVER = "LVM_IT87_NEVER_MATCHES"
WAIT_S = 20


def _ts(text: str) -> int:
    return int(datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=sample_logs.SERVER_TZ).timestamp())


SEARCH_ARGS = {
    "svr_ids": [1001],
    "log_type": "normal",
    "start_time": _ts("2026-09-17 15:30"),
    "end_time": _ts("2026-09-17 15:31"),
    "include_patterns": [NEVER],
}


def _setup_script(d: str) -> str:
    return (
        "set -e\n"
        "{ d=%s\n"
        "  mkdir -p -- \"$d/logs\"\n"
        "  line=$(printf '%%01000d' 0)\n"
        "  yes \"2026-09-17 15:30:00,000 | INFO  | t | FILL | $line\" | head -c %d | gzip -9 -c >\"$d/member.gz\"\n"
        "  for i in $(seq 1 %d); do cat -- \"$d/member.gz\"; done >\"$d/logs/smartfox.log.%s\"\n"
        "  rm -f -- \"$d/member.gz\"\n"
        "  stat -c %%s -- \"$d/logs/smartfox.log.%s\"\n"
        "} </dev/null\n" % (d, MEMBER_BYTES, MEMBERS, TS, TS)
    )


def _find_query_groups_script(d: str) -> str:
    """输出读取 d 下文件的 gzip 进程所在的进程组号，以及组长的命令行。"""
    return (
        "{ for s in /proc/[0-9]*/stat; do p=${s%%/stat}\n"
        "  c=$(tr '\\0' ' ' 2>/dev/null <\"$p/cmdline\") || continue\n"
        "  case $c in 'gzip -dcf -- %s/'*)\n"
        "    read -r l 2>/dev/null <\"$s\" || continue\n"
        "    r=${l##*) }; set -- $r\n"
        "    g=$(tr '\\0' ' ' 2>/dev/null <\"/proc/$3/cmdline\") || g=\n"
        "    printf '%%s\\t%%s\\n' \"$3\" \"$g\" ;;\n"
        "  esac\n"
        "done; true; } </dev/null\n" % d
    )


@pytest.fixture
def long_log_dir(it_env: ItEnv, game_servers: GameServers) -> Iterator[str]:
    d = f"/var/tmp/lvm-it87-{secrets.token_hex(6)}"
    out = run_sync(game_servers.run_ok(it_env.gs_ip, _setup_script(d), timeout=300))
    assert int(out.split()[-1]) > 0
    try:
        yield d
    finally:
        run_sync(game_servers.run_ok(it_env.gs_ip, "{ rm -rf -- %s; } </dev/null\n" % d))


@pytest.fixture
def long_server(make_service_files, long_log_dir: str) -> Iterator[RunningServer]:
    files: ServiceFiles = make_service_files(
        limits={"query_timeout_s": QUERY_TIMEOUT_S},
        log_types={"normal": {"dir": f"{long_log_dir}/logs", "suffix": ""}},
    )
    running = RunningServer(files)
    try:
        running.wait_until_listening()
        yield running
    finally:
        running.stop()


def open_tool_call(files: ServiceFiles, arguments: dict) -> socket.socket:
    """经一条新的 TCP 连接发出 tools/call（stateless，不需要 initialize），不等待答复。"""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "search_logs", "arguments": arguments}}).encode()
    head = (
        f"POST /mcp HTTP/1.1\r\nHost: {files.listen_address}:{files.listen_port}\r\n"
        f"Authorization: Bearer {files.token}\r\n"
        "Accept: application/json, text/event-stream\r\nContent-Type: application/json\r\n"
        f"Connection: close\r\nContent-Length: {len(body)}\r\n\r\n"
    ).encode()
    sock = socket.create_connection((files.listen_address, files.listen_port), timeout=WAIT_S)
    sock.sendall(head + body)
    return sock


def read_http_response(sock: socket.socket) -> tuple[int, bytes]:
    data = b""
    with sock:
        while chunk := sock.recv(65536):
            data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    return int(head.split()[1]), body


async def wait_query_group(game_servers: GameServers, ip: str, d: str) -> int:
    """等到读取 d 的 gzip 在运行，返回其进程组号（组长为 timeout -s KILL <秒>）。"""
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        out = (await game_servers.run_ok(ip, _find_query_groups_script(d))).decode()
        groups = {line.split("\t", 1)[0]: line.split("\t", 1)[1] for line in out.splitlines() if line}
        if groups:
            assert len(groups) == 1, groups
            ((pgid, leader),) = groups.items()
            words = leader.split()
            assert words[:3] == ["timeout", "-s", "KILL"] and int(words[3]) in REMOTE_SECONDS, leader
            return int(pgid)
        await asyncio.sleep(0.2)
    raise AssertionError("远端查询没有在时限内开始")


def audit_records(files: ServiceFiles) -> list[dict]:
    return [json.loads(line) for p in sorted(files.audit_dir.glob("audit-*.jsonl"))
            for line in p.read_text(encoding="utf-8").splitlines()]


async def wait_audit(files: ServiceFiles, count: int) -> list[dict]:
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        records = audit_records(files)
        if len(records) >= count:
            return records
        await asyncio.sleep(0.1)
    raise AssertionError(f"审计记录不足 {count} 条：{audit_records(files)}")


def assert_cancelled_record(record: dict, files: ServiceFiles) -> None:
    assert (record["user"], record["tool"], record["status"]) == (files.user, "search_logs", "cancelled")
    (target,) = record["targets"]
    assert target["svr_id"] == 1001 and target["status"] == "cancelled" and len(target["scripts"]) == 2


@pytest.mark.asyncio
async def test_client_disconnect_kills_remote_query(it_env: ItEnv, game_servers: GameServers,
                                                    long_server: RunningServer, long_log_dir: str) -> None:
    ip = it_env.gs_ip
    files = long_server.files
    async with game_servers.assert_unchanged(ip):
        sock = open_tool_call(files, SEARCH_ARGS)
        pgid = await wait_query_group(game_servers, ip, long_log_dir)
        sock.close()  # 客户端断开（Local_MCP 被 Agent 取消时关闭连接）
        await _assert_group_gone(game_servers, ip, pgid)

        (record,) = await wait_audit(files, 1)
        assert_cancelled_record(record, files)
        assert os.listdir(files.results_dir) == []  # 本次结果目录已删除

    # 服务照常工作。
    r = httpx.get(f"{files.base_url}/results/{HANDLE}/1001_normal.log",
                  headers={"Authorization": f"Bearer {files.token}"}, timeout=5)
    assert r.status_code == 404
    stderr = long_server.stop()
    assert "工具调用已取消（disconnected）" in stderr
    assert files.token not in stderr


@pytest.mark.asyncio
async def test_sigterm_kills_remote_query_and_exits(it_env: ItEnv, game_servers: GameServers,
                                                    long_server: RunningServer, long_log_dir: str) -> None:
    ip = it_env.gs_ip
    files = long_server.files
    probe_url = f"{files.base_url}/results/{HANDLE}/1001_normal.log"
    probe_headers = {"Authorization": f"Bearer {files.token}"}
    async with game_servers.assert_unchanged(ip):
        sock = open_tool_call(files, SEARCH_ARGS)
        pgid = await wait_query_group(game_servers, ip, long_log_dir)
        reply = asyncio.create_task(asyncio.to_thread(read_http_response, sock))

        long_server.proc.send_signal(signal.SIGTERM)
        statuses: list[int | str] = []
        deadline = time.monotonic() + WAIT_S
        while long_server.proc.poll() is None and time.monotonic() < deadline and len(statuses) < 300:
            try:
                statuses.append(httpx.get(probe_url, headers=probe_headers, timeout=2).status_code)
            except httpx.TransportError:
                statuses.append("refused")
            await asyncio.sleep(0.02)

        status, body = await asyncio.wait_for(reply, WAIT_S)
        assert status == 200
        result = json.loads(body)["result"]
        assert result["isError"] is True
        assert result["structuredContent"]["error"] == {
            "code": "INTERNAL_ERROR", "message": "服务正在停机，调用已取消，请稍后重试"}
        await _assert_group_gone(game_servers, ip, pgid)

    stderr = long_server.stop()
    # 收到 SIGTERM 之前到达的探测返回 404；之后只有 503（停机中）或连不上（已停止监听）。
    first = next((i for i, s in enumerate(statuses) if s != 404), len(statuses))
    assert set(statuses[:first]) <= {404} and set(statuses[first:]) <= {503, "refused"}, statuses
    assert 503 in statuses, statuses
    assert long_server.proc.returncode in (0, -signal.SIGTERM), stderr
    assert "已取消 1 个进行中的调用" in stderr and "Application shutdown complete" in stderr

    (record,) = audit_records(files)
    assert_cancelled_record(record, files)
    assert os.listdir(files.results_dir) == []
    assert files.token not in stderr
