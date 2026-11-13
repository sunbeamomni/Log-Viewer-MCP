"""② 查询执行与终止的集成测试（任务 6.6）：在 gs 上超时或取消后，远端进程组已不存在，文件未被改动。

查询用同一个分钟文件重复 REPEAT 次（不带包含模式，匹配全部行）；timeout 的秒数取一个独特的值，
测试据此在 /proc 中找到本次查询的 timeout 进程（其 pid 即进程组号）。完整的远端执行集成测试见 6.10。

远端不靠"运行得慢"来保证在超时或取消时仍在运行（读完所需的时间随机器快慢而变，曾在 4 秒的截止时刻
之前读完，使超时测试偶发失败）：第一个文件
结束后，测试不再读取 ② 的输出（_stall_reads_after_first_file），SSH 窗口、sshd 的通道缓冲与管道
写满后远端阻塞在写入上，直到被 kill。为此总输出（REPEAT × 文件大小，约 33 MB）远大于这些缓冲之和
（asyncssh 默认窗口 2 MiB，sshd 缓冲不超过对端窗口，管道 64 KiB）。
"""

from __future__ import annotations

import asyncio

import pytest

from log_viewer_mcp.server.remote import (
    READ_CHUNK,
    FileState,
    FileStatus,
    QueryFile,
    RemoteConnection,
    RemoteProcess,
    SshConnector,
    build_query_script,
    run_query,
)
from log_viewer_mcp.server.select import SelectedFile
from tests.integration import sample_logs
from tests.integration.itenv import GameServers, ItEnv

LOGS = sample_logs.LOG_TYPES["normal"].dir
SECONDS = 97  # 远端 timeout 的秒数：远大于测试时长，进程只会被 kill 结束
REPEAT = 20000
TS = "2026-09-17-16-04"


class CountingSink:
    """只统计字节数，记录调用顺序。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.bytes = 0
        self.began = asyncio.Event()
        self.ended = asyncio.Event()

    def begin_file(self, index: int, file: QueryFile) -> None:
        self.calls.append(("begin", index))
        self.began.set()

    def write(self, index: int, data: bytes) -> None:
        self.bytes += len(data)

    def end_file(self, index: int, status: FileStatus) -> None:
        self.calls.append(("end", index, status.ok))
        self.ended.set()

    def interrupt_file(self, index: int) -> None:
        self.calls.append(("interrupt", index))


def _stall_reads_after_first_file(conn: RemoteConnection, sink: CountingSink) -> None:
    """conn 上第一个会话（②）的 read 在 sink 收到第一个 END 之后不再返回（直到被取消）。

    只影响本端读取：远端写满缓冲后阻塞，进程组一直存在；之后的会话（kill）照常读取。
    """
    start = conn.start
    wrapped = False

    async def stalled_start(command: str, script: str | bytes = b"") -> RemoteProcess:
        nonlocal wrapped
        process = await start(command, script)
        if not wrapped:
            wrapped = True
            read = process.read

            async def stalled_read(n: int = READ_CHUNK) -> bytes:
                if sink.ended.is_set():
                    await asyncio.Event().wait()  # 永不设置：等到截止时刻或取消
                return await read(n)

            process.read = stalled_read  # type: ignore[method-assign]
        return process

    conn.start = stalled_start  # type: ignore[method-assign]


def _long_query():
    files = [SelectedFile(plain=f"smartfox.log.{TS}", archive=None, inode=None, ts=TS, start=0, end=1, size=0)] * REPEAT
    return build_query_script(files, plain_dir=LOGS, zip_dir=sample_logs.ZIP_DIR, seconds=SECONDS, ionice=True)


_FIND_TIMEOUT = (
    "{ for d in /proc/[0-9]*; do c=$(tr '\\0' ' ' 2>/dev/null <\"$d/cmdline\") || continue\n"
    "  case $c in 'timeout -s KILL %d '*) printf '%%s\\n' \"${d#/proc/}\" ;; esac\n"
    "done; } </dev/null\n" % SECONDS
)


def _group_script(pgid: int) -> str:
    return (
        "{ for s in /proc/[0-9]*/stat; do read -r l 2>/dev/null <\"$s\" || continue\n"
        "  r=${l##*) }; set -- $r\n"
        "  [ \"$3\" = %d ] && printf '%%s %%s\\n' \"${s#/proc/}\" \"$1\"\n"
        "done; true; } </dev/null\n" % pgid
    )


async def _timeout_pids(game_servers: GameServers, ip: str) -> list[int]:
    out = await game_servers.run_ok(ip, _FIND_TIMEOUT)
    return [int(x) for x in out.split()]


async def _group_members(game_servers: GameServers, ip: str, pgid: int) -> list[str]:
    out = await game_servers.run_ok(ip, _group_script(pgid))
    return out.decode().splitlines()


async def _assert_group_gone(game_servers: GameServers, ip: str, pgid: int) -> None:
    members: list[str] = []
    for _ in range(20):
        members = await _group_members(game_servers, ip, pgid)
        if not members:
            return
        await asyncio.sleep(0.25)
    raise AssertionError(f"进程组 {pgid} 仍有进程：{members}")


async def _running_pgid(game_servers: GameServers, ip: str) -> int:
    pids = await _timeout_pids(game_servers, ip)
    assert len(pids) == 1, pids
    pgid = pids[0]
    assert await _group_members(game_servers, ip, pgid), "查询的进程组应仍在运行"
    return pgid


@pytest.mark.asyncio
async def test_cancel_kills_remote_process_group(it_env: ItEnv, service_files, game_servers) -> None:
    ip = it_env.gs_ip
    assert await _timeout_pids(game_servers, ip) == []
    async with game_servers.assert_unchanged(ip):
        conn = await SshConnector(service_files.load().ssh).connect(ip)
        sink = CountingSink()
        _stall_reads_after_first_file(conn, sink)
        task = asyncio.create_task(run_query(conn, _long_query(), sink))
        await asyncio.wait_for(sink.ended.wait(), 20)
        pgid = await _running_pgid(game_servers, ip)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert conn.closed
        await _assert_group_gone(game_servers, ip, pgid)
    assert await _timeout_pids(game_servers, ip) == []


@pytest.mark.asyncio
async def test_timeout_kills_remote_process_group(it_env: ItEnv, service_files, game_servers) -> None:
    ip = it_env.gs_ip
    assert await _timeout_pids(game_servers, ip) == []
    async with game_servers.assert_unchanged(ip):
        conn = await SshConnector(service_files.load().ssh).connect(ip)
        sink = CountingSink()
        _stall_reads_after_first_file(conn, sink)
        deadline = asyncio.get_running_loop().time() + 4
        task = asyncio.create_task(run_query(conn, _long_query(), sink, deadline=deadline))
        await asyncio.wait_for(sink.ended.wait(), 4)
        pgid = await _running_pgid(game_servers, ip)
        outcome = await task
        assert outcome.timed_out and not outcome.done and outcome.killed
        assert outcome.pgid == pgid
        assert conn.closed
        states = [f.state for f in outcome.files]
        assert FileState.READ in states and states[-1] is FileState.NOT_REACHED
        assert FileState.FAILED not in states
        assert sink.bytes > 0
        await _assert_group_gone(game_servers, ip, pgid)
    assert await _timeout_pids(game_servers, ip) == []
