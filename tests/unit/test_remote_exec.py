"""server/remote.py 的 ② 查询执行与终止（任务 6.6）。

用进程内的 asyncssh 服务端代替游戏服：② 的固定命令行由替身按场景回复帧输出，
``kill -KILL -- -<pgid>`` 会话被记录下来，并让"仍在运行"的 ② 以 KILL 信号结束。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import asyncssh
import pytest
import pytest_asyncio

from log_viewer_mcp.common.errors import ErrorCode, LogViewerError
from log_viewer_mcp.server import remote
from log_viewer_mcp.server.config import SshConfig
from log_viewer_mcp.server.remote import (
    FileState,
    FileStatus,
    QueryFile,
    SshConnector,
    build_kill_command,
    build_query_script,
    is_fixed_command,
    run_query,
)
from log_viewer_mcp.server.select import SelectedFile

HOST = "127.0.0.1"
PGID = 4242
QUERY_PREFIX = "env -i PATH=/usr/bin:/bin LC_ALL=C timeout -s KILL "


class Ctx:
    """一个测试的远端替身状态（事件属于当前事件循环）。"""

    def __init__(self) -> None:
        self.scenario = "normal"
        self.kill_hangs = False
        self.commands: list[str] = []
        self.kills: list[int] = []
        self.killed = asyncio.Event()
        self.release = asyncio.Event()


CTX: Ctx


async def _hang() -> None:
    """模拟仍在运行的远端进程：直到收到 kill 或测试结束。"""
    waits = [asyncio.ensure_future(CTX.killed.wait()), asyncio.ensure_future(CTX.release.wait())]
    try:
        await asyncio.wait(waits, timeout=10, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for w in waits:
            w.cancel()


async def _handle(process: asyncssh.SSHServerProcess) -> None:
    cmd = process.command or ""
    data = await process.stdin.read()
    CTX.commands.append(cmd)
    try:
        m = re.fullmatch(r"kill -KILL -- -([0-9]+)", cmd)
        if m:
            if CTX.kill_hangs:
                await _hang()
                return
            CTX.kills.append(int(m.group(1)))
            CTX.killed.set()
            process.exit(0)
            return
        if not cmd.startswith(QUERY_PREFIX):
            process.exit(127)
            return
        b = re.match(rb"\{ B=([0-9a-f]{32})\n", data).group(1)
        out = process.stdout

        def frame(text: bytes) -> None:
            out.write(b + b" " + text + b"\n")

        s = CTX.scenario
        if s == "127":
            process.exit(127)
            return
        out.write(b"bashrc noise " + b + b" FILE 0\n")  # READY 之前的登录 Shell 输出，丢弃
        if s == "noready":
            await out.drain()
            await _hang()
            process.exit_with_signal("KILL")
            return
        frame(b"READY %d" % PGID)
        if s == "ready_hang":
            await out.drain()
            await _hang()
            process.exit_with_signal("KILL")
            return
        if s == "badframe":
            frame(b"FILE 1")
            await out.drain()
            await _hang()
            process.exit_with_signal("KILL")
            return
        frame(b"FILE 0")
        if s == "slow":
            out.write(b"done line\npartial")
            await out.drain()
            await _hang()
            process.exit_with_signal("KILL")
            return
        if s == "eof":
            out.write(b"a\n")
            process.exit(0)
            return
        # normal：一个成功、一个解压失败、一个两个文件都不存在
        out.write(b"a1 " + b + b" END 0\n" + b + b"END 0 fake\na2\n")  # 不在行首、或 B 后没有空格：数据
        frame(b"END 0 F 0 0")
        frame(b"FILE 1")
        out.write(b"x\n")
        frame(b"END 1 Z 1 0")
        frame(b"FILE 2")
        frame(b"END 2 N")
        frame(b"DONE")
        process.exit(0)
    except (asyncssh.Error, OSError, BrokenPipeError):
        pass  # 客户端已关闭连接


@pytest.fixture
def keys() -> dict[str, asyncssh.SSHKey]:
    return {name: asyncssh.generate_private_key("ssh-ed25519") for name in ("host", "client")}


@pytest_asyncio.fixture
async def ctx() -> AsyncIterator[Ctx]:
    global CTX
    CTX = Ctx()
    yield CTX
    CTX.release.set()
    await asyncio.sleep(0.05)


@pytest_asyncio.fixture
async def ssh_server(keys, ctx) -> AsyncIterator[int]:
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
def connector(tmp_path: Path, keys, ssh_server) -> SshConnector:
    kh = tmp_path / "known_hosts"
    kh.write_text(f"[{HOST}]:{ssh_server} {keys['host'].export_public_key().decode().strip()}\n", encoding="utf-8")
    cfg = SshConfig("root", ssh_server, "id_ed25519", str(kh), 5, keys["client"].export_private_key())
    return SshConnector(cfg)


def _query() -> remote.QueryScript:
    files = [
        SelectedFile(plain=f"smartfox.log.2026-09-17-16-0{m}", archive=f"smartfox.log.2026-09-17-16-0{m}.tar.gz",
                     inode=None, ts=f"2026-09-17-16-0{m}", start=0, end=1, size=0)
        for m in range(3)
    ]
    return build_query_script(files, plain_dir="/usr/local/ds/SFS2X/logs", zip_dir="/usr/local/ds/SFS2X/ziplogs",
                              include_patterns=["CMD"], seconds=45, ionice=True)


class RecordingSink:
    """记录 run_query 调用写入接口的顺序（写入的数据按序号拼接）。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.data: dict[int, bytes] = {}
        self.began = asyncio.Event()

    def begin_file(self, index: int, file: QueryFile) -> None:
        assert file.index == index
        self.calls.append(("begin", index))
        self.data[index] = b""
        self.began.set()

    def write(self, index: int, data: bytes) -> None:
        self.data[index] += data

    def end_file(self, index: int, status: FileStatus) -> None:
        self.calls.append(("end", index, status))

    def interrupt_file(self, index: int) -> None:
        self.calls.append(("interrupt", index))


# ---------------------------------------------------------------------------
# 正常完成
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_normal_completion_reports_each_file(connector, ctx) -> None:
    q = _query()
    sink = RecordingSink()
    async with await connector.connect(HOST) as conn:
        outcome = await run_query(conn, q, sink, deadline=asyncio.get_running_loop().time() + 30)
        assert not conn.closed  # 连接留给调用方
    b = q.boundary.encode()
    assert sink.calls == [
        ("begin", 0), ("end", 0, FileStatus("F", 0, (0,))),
        ("begin", 1), ("end", 1, FileStatus("Z", 1, (0,))),
        ("begin", 2), ("end", 2, FileStatus("N", None, ())),
    ]
    assert sink.data == {0: b"a1 " + b + b" END 0\n" + b + b"END 0 fake\na2\n", 1: b"x\n", 2: b""}
    assert [f.state for f in outcome.files] == [FileState.READ, FileState.FAILED, FileState.FAILED]
    assert outcome.done and not outcome.timed_out and not outcome.killed
    assert outcome.pgid == PGID and outcome.exit.exit_status == 0
    assert outcome.files_read == ("smartfox.log.2026-09-17-16-00",)
    assert outcome.files_skipped == ("smartfox.log.2026-09-17-16-01", "smartfox.log.2026-09-17-16-02")
    assert ctx.kills == [] and ctx.commands == [q.command]


# ---------------------------------------------------------------------------
# 超时与取消
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_keeps_lines_and_kills_process_group(connector, ctx) -> None:
    ctx.scenario = "slow"
    q = _query()
    sink = RecordingSink()
    conn = await connector.connect(HOST)
    outcome = await run_query(conn, q, sink, deadline=asyncio.get_running_loop().time() + 0.5)
    assert outcome.timed_out and not outcome.done and outcome.killed and outcome.exit is None
    assert [f.state for f in outcome.files] == [FileState.TRUNCATED, FileState.NOT_REACHED, FileState.NOT_REACHED]
    assert sink.calls == [("begin", 0), ("interrupt", 0)]
    assert sink.data[0] == b"done line\npartial"  # 截到完整行由 sink 完成
    assert outcome.files_read == ("smartfox.log.2026-09-17-16-00",)
    assert ctx.kills == [PGID]
    assert ctx.commands == [q.command, build_kill_command(PGID)]
    assert conn.closed


@pytest.mark.asyncio
async def test_cancel_kills_process_group_and_propagates(connector, ctx) -> None:
    ctx.scenario = "slow"
    sink = RecordingSink()
    conn = await connector.connect(HOST)
    task = asyncio.create_task(run_query(conn, _query(), sink))
    await asyncio.wait_for(sink.began.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ctx.kills == [PGID]
    assert conn.closed
    assert sink.calls == [("begin", 0)]  # 取消时不再调用 sink


@pytest.mark.asyncio
async def test_cancel_before_ready_only_closes_connection(connector, ctx) -> None:
    ctx.scenario = "noready"
    conn = await connector.connect(HOST)
    task = asyncio.create_task(run_query(conn, _query(), RecordingSink()))
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ctx.kills == [] and len(ctx.commands) == 1
    assert conn.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario, kills", [("noready", []), ("ready_hang", [PGID])])
async def test_timeout_before_reading_is_timeout_error(connector, ctx, scenario, kills) -> None:
    ctx.scenario = scenario
    sink = RecordingSink()
    conn = await connector.connect(HOST)
    with pytest.raises(LogViewerError) as ei:
        await run_query(conn, _query(), sink, deadline=asyncio.get_running_loop().time() + 0.4)
    assert ei.value.code is ErrorCode.TIMEOUT
    assert ctx.kills == kills and sink.calls == []
    assert conn.closed


@pytest.mark.asyncio
async def test_kill_session_is_time_limited(connector, ctx, monkeypatch) -> None:
    monkeypatch.setattr(remote, "KILL_TIMEOUT_S", 0.3)
    ctx.scenario = "slow"
    ctx.kill_hangs = True
    conn = await connector.connect(HOST)
    outcome = await run_query(conn, _query(), RecordingSink(), deadline=asyncio.get_running_loop().time() + 0.3)
    assert outcome.timed_out and not outcome.killed
    assert ctx.commands[-1] == build_kill_command(PGID)
    assert conn.closed


# ---------------------------------------------------------------------------
# 错误
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_frame_error_is_internal_error_and_kills(connector, ctx) -> None:
    ctx.scenario = "badframe"
    conn = await connector.connect(HOST)
    with pytest.raises(LogViewerError) as ei:
        await run_query(conn, _query(), RecordingSink())
    assert ei.value.code is ErrorCode.INTERNAL_ERROR
    assert ctx.kills == [PGID]
    assert conn.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario, code", [("eof", ErrorCode.SSH_FAILED), ("127", ErrorCode.REMOTE_TOOL_UNAVAILABLE)])
async def test_output_ending_before_done(connector, ctx, scenario, code) -> None:
    ctx.scenario = scenario
    async with await connector.connect(HOST) as conn:
        with pytest.raises(LogViewerError) as ei:
            await run_query(conn, _query(), RecordingSink())
    assert ei.value.code is code
    assert ctx.kills == []  # 会话已结束，不执行 kill


@pytest.mark.asyncio
async def test_sink_error_kills_and_propagates(connector, ctx) -> None:
    ctx.scenario = "slow"

    class FailingSink(RecordingSink):
        def write(self, index: int, data: bytes) -> None:
            raise LogViewerError(ErrorCode.RESULT_STORE_FAILED)

    conn = await connector.connect(HOST)
    with pytest.raises(LogViewerError) as ei:
        await run_query(conn, _query(), FailingSink())
    assert ei.value.code is ErrorCode.RESULT_STORE_FAILED
    assert ctx.kills == [PGID]
    assert conn.closed


def test_query_script_grep_stages() -> None:
    assert _query().grep_stages == 1
    files = [SelectedFile(plain="smartfox.log.2026-09-17-16-00", archive=None, inode=None,
                          ts="2026-09-17-16-00", start=0, end=1, size=0)]
    q = build_query_script(files, plain_dir="/a", zip_dir="/b", exclude_patterns=["x"], seconds=5, ionice=False)
    assert q.grep_stages == 2


def test_build_kill_command() -> None:
    assert build_kill_command(4242) == "kill -KILL -- -4242"
    assert build_kill_command(2) == "kill -KILL -- -2"
    assert build_kill_command(9_999_999_999) == "kill -KILL -- -9999999999"
    assert is_fixed_command(build_kill_command(4242))
    for bad in (0, 1, -5, 10**10, True, 4242.0, "4242", None):
        with pytest.raises(ValueError):
            build_kill_command(bad)
