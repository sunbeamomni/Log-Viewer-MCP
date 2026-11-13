"""远端执行的端到端集成测试（任务 6.10）：同一连接上依次执行 ①、select_files 与 ②（run_query）。

- 主机公钥不符或未登记：三台游戏服都返回 HOST_KEY_MISMATCH（8.8）。
- ionice 缺失：gs-noionice 上 ① 报告没有 ionice，② 的命令行不加 ionice，查询照常完成（9.1）。
- 登录 Shell 的输出被丢弃：~/.bashrc 打印的伪造记录不出现在结果中，结果与参照过滤逐字节相同。
- ① 与 ② 之间删除 Rotated：有同 TS 压缩包的改读压缩包（2.6），没有的记为失败。
- ① 与 ② 之间切割 smartfox.log（改名后新建）：按 inode 读取改名后的文件，即 ① 时看到的内容（2.8）。
- 超时与取消（没有 ionice 的命令行）：远端进程组已结束（9.3、10.5）。

会改动文件的测试只在 gs-noionice 上、自建于只读快照范围之外的临时目录（/var/tmp/lvm-it610-*）中进行：
从样例目录复制所需文件后再删除或改名，结束时删除整个临时目录。样例目录本身只读，不需要 reset_samples，
也不影响同时在各游戏服上拍快照的其他测试。
"""

from __future__ import annotations

import asyncio
import secrets
import shlex
from collections.abc import Awaitable, Callable, Iterator, Sequence

import pytest

from log_viewer_mcp.common.errors import ErrorCode, LogViewerError
from log_viewer_mcp.server.remote import (
    FileState,
    FileStatus,
    Listing,
    QueryFile,
    SshConnector,
    build_listing_script,
    build_query_script,
    run_listing,
    run_query,
)
from log_viewer_mcp.server.select import select_files
from tests.integration import sample_logs
from tests.integration.itenv import GameServers, ItEnv, run_sync
from tests.integration.test_remote_exec import _assert_group_gone, _group_members

SECONDS = 30
LOGS = sample_logs.LOG_TYPES["normal"].dir
ZIP = sample_logs.ZIP_DIR
MARK = sample_logs.MARKER_PREFIX

# ~/.bashrc（docker/gameserver/root.bashrc）打印的行
BASHRC_LINES = (
    b"Welcome to SFS2X game server",
    b"0123456789abcdef0123456789abcdef READY 1",
    b"0123456789abcdef0123456789abcdef FILE 0",
    b"EXIT L 0",
    b"L 1 2 smartfox.log",
    b"MISSING grep",
    b"bashrc: stderr noise",
)


class CollectingSink:
    """按序号收集 grep 输出；失败截回、超时截到最后一个完整行（与 results.py 的约定相同）。"""

    def __init__(self) -> None:
        self.data: dict[int, bytearray] = {}
        self.began = asyncio.Event()

    def begin_file(self, index: int, file: QueryFile) -> None:
        self.data[index] = bytearray()
        self.began.set()

    def write(self, index: int, data: bytes) -> None:
        self.data[index] += data

    def end_file(self, index: int, status: FileStatus) -> None:
        if not status.ok:
            self.data[index].clear()

    def interrupt_file(self, index: int) -> None:
        buf = self.data[index]
        del buf[buf.rfind(b"\n") + 1:]

    def get(self, index: int) -> bytes:
        return bytes(self.data.get(index, b""))


def grep_ref(content: bytes, patterns: Sequence[str]) -> bytes:
    """grep -a -F -e p… 的参照实现（LC_ALL=C，区分大小写，无上下文）。"""
    pats = [p.encode("utf-8") for p in patterns]
    lines = content.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    return b"".join(line + b"\n" for line in lines if any(p in line for p in pats))


def select(listing: Listing, start: int, end: int):
    return select_files(
        listing.plain, listing.archives, start=start, end=end, now=sample_logs.GENERATED_AT,
        max_scan_bytes=1 << 40, suffix="", tz=sample_logs.SERVER_TZ,
    ).files


async def listing_then_query(
    service_files, host: str, *, plain_dir: str = LOGS, zip_dir: str = ZIP, start: int, end: int,
    between: Callable[[Listing], Awaitable[None]] | None = None, **query_kwargs,
):
    """同一连接上执行 ①、选取与 ②；between 在 ① 之后、② 之前执行（模拟期间的切割、压缩与删除）。"""
    connector = SshConnector(service_files.load().ssh)
    async with await connector.connect(host) as conn:
        listing = await run_listing(conn, build_listing_script(plain_dir, zip_dir), seconds=SECONDS)
        files = select(listing, start, end)
        if between is not None:
            await between(listing)
        q = build_query_script(files, plain_dir=plain_dir, zip_dir=zip_dir, seconds=SECONDS,
                               ionice=listing.ionice, **query_kwargs)
        sink = CollectingSink()
        deadline = asyncio.get_running_loop().time() + SECONDS
        outcome = await run_query(conn, q, sink, deadline=deadline)
    return listing, q, outcome, sink


def expected_content(samples: sample_logs.SampleSet, f: QueryFile) -> tuple[bytes, str]:
    """样例中该项的内容与应读取的分支（F 非压缩文件，Z 压缩包）。"""
    if f.is_current:
        return samples.current("normal").content, "F"
    period = samples.period("normal", f.source.ts)
    return period.content, ("F" if period.rotated else "Z")


def assert_no_login_shell_output(samples: sample_logs.SampleSet, q, sink: CollectingSink,
                                 patterns: Sequence[str]) -> None:
    """~/.bashrc 打印的各行在结果中出现的次数与参照过滤相同（normal 16-07 的日志内容本身也含其中几行）。"""
    got = b"".join(sink.get(f.index) for f in q.files)
    expected = b"".join(grep_ref(expected_content(samples, f)[0], patterns) for f in q.files)
    for line in BASHRC_LINES:
        assert got.count(line) == expected.count(line), line
    assert b"Welcome to SFS2X game server" not in got


# ---------------------------------------------------------------------------
# 主机公钥（8.8）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["gs", "gs-noionice", "gs-grepspy"])
async def test_wrong_host_key_on_every_server(it_env: ItEnv, make_service_files, name) -> None:
    connector = SshConnector(make_service_files(known_hosts="wrong").load().ssh)
    with pytest.raises(LogViewerError) as ei:
        await connector.connect(it_env.game_servers[name])
    assert ei.value.code is ErrorCode.HOST_KEY_MISMATCH


@pytest.mark.asyncio
async def test_unregistered_host_key_is_host_key_mismatch(it_env: ItEnv, service_files) -> None:
    """known_hosts 中没有该 IP 的行（其他游戏服的行仍在）：同样不登录，返回 HOST_KEY_MISMATCH。"""
    ip = it_env.gs_noionice_ip
    lines = service_files.known_hosts.read_bytes().splitlines(keepends=True)
    kept = [line for line in lines if not line.startswith(ip.encode() + b" ")]
    assert len(kept) < len(lines) and kept
    service_files.known_hosts.write_bytes(b"".join(kept))
    connector = SshConnector(service_files.load().ssh)
    with pytest.raises(LogViewerError) as ei:
        await connector.connect(ip)
    assert ei.value.code is ErrorCode.HOST_KEY_MISMATCH
    # 已登记的另一台照常连接
    async with await connector.connect(it_env.gs_ip) as conn:
        assert not conn.closed


# ---------------------------------------------------------------------------
# ionice 缺失（9.1）与登录 Shell 的输出
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_without_ionice_reads_full_content(it_env: ItEnv, service_files, samples) -> None:
    """gs-noionice：① 报告没有 ionice，② 的命令行不含 ionice；不带包含模式时结果与各文件内容逐字节相同。"""
    listing, q, outcome, sink = await listing_then_query(
        service_files, it_env.gs_noionice_ip,
        start=sample_logs.server_time(2026, 9, 17, 16, 0), end=sample_logs.server_time(2026, 9, 17, 16, 12),
    )
    assert listing.ionice is False
    assert "ionice" not in q.command and "nice -n 19 bash" in q.command
    assert outcome.done and not outcome.timed_out and outcome.exit.exit_status == 0
    assert [f.source.ts for f in q.files] == (
        ["2026-09-17-16-%02d" % m for m in (0, 1, 2, 3, 4, 6, 7, 8, 9)] + [None])
    assert {f.state for f in outcome.files} == {FileState.READ}
    assert outcome.files_skipped == ()
    for fo in outcome.files:
        content, branch = expected_content(samples, fo.file)
        assert fo.status.branch == branch, fo.file.name
        # 含 normal 16-07 中的伪造帧行与长行：作为数据原样到达
        assert sink.get(fo.file.index) == grep_ref(content, [""]), fo.file.name
    assert_no_login_shell_output(samples, q, sink, [""])


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["gs", "gs-noionice"])
async def test_login_shell_output_never_reaches_results(it_env: ItEnv, service_files, samples, name) -> None:
    """包含模式正好匹配 ~/.bashrc 打印的伪造记录：结果只有日志中的匹配行，与参照过滤逐字节相同。"""
    patterns = ["READY", "FILE 0", "EXIT L", "MISSING", "Welcome", "bashrc", "smartfox.log"]
    listing, q, outcome, sink = await listing_then_query(
        service_files, it_env.game_servers[name],
        start=sample_logs.server_time(2026, 9, 17, 16, 0), end=sample_logs.server_time(2026, 9, 17, 16, 12),
        include_patterns=patterns,
    )
    assert listing.ionice is (name == "gs")
    assert outcome.done and {f.state for f in outcome.files} == {FileState.READ}
    for fo in outcome.files:
        content, _ = expected_content(samples, fo.file)
        assert sink.get(fo.file.index) == grep_ref(content, patterns), fo.file.name
    assert_no_login_shell_output(samples, q, sink, patterns)


# ---------------------------------------------------------------------------
# ① 与 ② 之间的文件变化（gs-noionice 上的临时目录）
# ---------------------------------------------------------------------------


@pytest.fixture
def sandbox(it_env: ItEnv, game_servers: GameServers) -> Iterator[str]:
    """gs-noionice 上只读快照范围之外的临时目录；测试结束时删除。"""
    root = f"/var/tmp/lvm-it610-{secrets.token_hex(6)}"
    yield root
    run_sync(game_servers.run_ok(it_env.gs_noionice_ip, f"rm -rf -- {shlex.quote(root)} </dev/null\n"))


async def populate(game_servers: GameServers, ip: str, root: str, plain: Sequence[str], archives: Sequence[str]) -> None:
    """建 root/logs 与 root/ziplogs（0777），从样例目录复制文件（保留权限与 mtime）。"""
    q = shlex.quote
    cmds = [f"mkdir -m 0777 -- {q(root)} {q(root + '/logs')} {q(root + '/ziplogs')}"]
    cmds += [f"cp -p -- {q(LOGS + '/' + n)} {q(root + '/logs/')}" for n in plain]
    cmds += [f"cp -p -- {q(ZIP + '/' + n)} {q(root + '/ziplogs/')}" for n in archives]
    await game_servers.run_ok(ip, "{ set -e\n" + "\n".join(cmds) + "\n} </dev/null\n")


@pytest.mark.asyncio
async def test_deleted_rotated_falls_back_to_archive(it_env: ItEnv, service_files, samples, game_servers,
                                                     sandbox) -> None:
    """① 之后 16-02（有同 TS 压缩包）与 16-04（没有）的 Rotated 被删除：16-02 改读压缩包，16-04 记为失败。"""
    ip = it_env.gs_noionice_ip
    ts = ["2026-09-17-16-02", "2026-09-17-16-03", "2026-09-17-16-04"]
    await populate(game_servers, ip, sandbox, [sample_logs.rotated_name(t) for t in ts],
                   [sample_logs.archive_name("normal", t) for t in ts[:2]])
    plain_dir, zip_dir = f"{sandbox}/logs", f"{sandbox}/ziplogs"

    async def delete_rotated(listing: Listing) -> None:
        assert {e.name for e in listing.plain} == {sample_logs.rotated_name(t) for t in ts}
        names = " ".join(shlex.quote(f"{plain_dir}/{sample_logs.rotated_name(t)}") for t in (ts[0], ts[2]))
        await game_servers.run_ok(ip, f"rm -- {names} </dev/null\n")

    _, q, outcome, sink = await listing_then_query(
        service_files, ip, plain_dir=plain_dir, zip_dir=zip_dir, between=delete_rotated,
        start=sample_logs.server_time(2026, 9, 17, 16, 2), end=sample_logs.server_time(2026, 9, 17, 16, 4),
    )
    assert [f.source.ts for f in q.files] == ts
    assert q.files[0].plain is not None and q.files[0].archive is not None and q.files[2].archive is None
    assert outcome.done
    assert [f.state for f in outcome.files] == [FileState.READ, FileState.READ, FileState.FAILED]
    assert [f.status.branch for f in outcome.files] == ["Z", "F", "N"]
    assert sink.get(0) == grep_ref(samples.log_file("normal", "archive", ts[0]).content, [""])
    assert sink.get(1) == grep_ref(samples.period("normal", ts[1]).content, [""])
    assert sink.get(2) == b""
    assert outcome.files_read == tuple(sample_logs.rotated_name(t) for t in ts[:2])
    assert outcome.files_skipped == (sample_logs.rotated_name(ts[2]),)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["rotate", "delete"])
async def test_current_changed_between_listing_and_query(it_env: ItEnv, service_files, samples, game_servers,
                                                         sandbox, change) -> None:
    """① 之后 smartfox.log 被切割（改名后新建）：② 按 inode 读改名后的文件，不读新建的；被删除时记为失败。"""
    ip = it_env.gs_noionice_ip
    last = "2026-09-17-16-09"
    await populate(game_servers, ip, sandbox, ["smartfox.log", sample_logs.rotated_name(last)], [])
    plain_dir, zip_dir = f"{sandbox}/logs", f"{sandbox}/ziplogs"
    current_path = f"{plain_dir}/smartfox.log"
    renamed_path = f"{plain_dir}/smartfox.log.2026-09-17-16-12"
    new_marker = "NEW_CURRENT_AFTER_ROTATION"
    seen: dict[str, int] = {}

    async def change_current(listing: Listing) -> None:
        entry = next(e for e in listing.plain if e.name == "smartfox.log")
        seen.update(inode=entry.inode, size=entry.size)
        if change == "rotate":
            line = sample_logs.log_line(sample_logs.server_time(2026, 9, 17, 16, 13) * 1000, "INFO", "main", "CMD",
                                        f"{new_marker} {MARK}normal/current")
            script = (f"{{ mv -- {shlex.quote(current_path)} {shlex.quote(renamed_path)} && "
                      f"printf '%s' {shlex.quote(line)} >{shlex.quote(current_path)} && "
                      f"stat -c %i -- {shlex.quote(renamed_path)}; }} </dev/null\n")
            out = await game_servers.run_ok(ip, script)
            assert int(out) == entry.inode  # 同一目录内改名，inode 不变
        else:
            await game_servers.run_ok(ip, f"rm -- {shlex.quote(current_path)} </dev/null\n")

    _, q, outcome, sink = await listing_then_query(
        service_files, ip, plain_dir=plain_dir, zip_dir=zip_dir, between=change_current,
        start=sample_logs.server_time(2026, 9, 17, 16, 9), end=sample_logs.server_time(2026, 9, 17, 16, 12),
    )
    assert [f.source.ts for f in q.files] == [last, None] and q.files[1].is_current
    assert q.files[1].inode == seen["inode"]
    assert outcome.done
    assert outcome.files[0].state is FileState.READ
    assert sink.get(0) == grep_ref(samples.period("normal", last).content, [""])
    if change == "rotate":
        current = samples.current("normal").content
        assert outcome.files[1].state is FileState.READ and outcome.files[1].status.branch == "F"
        assert sink.get(1) == current and len(current) == seen["size"]
        assert new_marker.encode() not in sink.get(1)
        assert outcome.files_skipped == ()
    else:
        assert outcome.files[1].state is FileState.FAILED and outcome.files[1].status.branch == "N"
        assert sink.get(1) == b""
        assert outcome.files_skipped == ("smartfox.log",)


# ---------------------------------------------------------------------------
# 超时与取消后远端进程已结束（9.3、10.5），命令行不含 ionice
# ---------------------------------------------------------------------------

LONG_SECONDS = 83  # 独特的 timeout 秒数，测试据此在 /proc 中找到本次查询的 timeout 进程
LONG_ENTRIES = 5000

# timeout 进程的命令行：nice 之后直接是 bash，没有 ionice
_FIND_TIMEOUT = (
    "{ for d in /proc/[0-9]*; do c=$(tr '\\0' ' ' 2>/dev/null <\"$d/cmdline\") || continue\n"
    "  case $c in 'timeout -s KILL %d '*) printf '%%s\\t%%s\\n' \"${d#/proc/}\" \"$c\" ;; esac\n"
    "done; } </dev/null\n" % LONG_SECONDS
)


async def _timeout_processes(game_servers: GameServers, ip: str) -> list[tuple[int, str]]:
    out = await game_servers.run_ok(ip, _FIND_TIMEOUT)
    return [(int(pid), cmd) for pid, cmd in (line.split("\t", 1) for line in out.decode().splitlines())]


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["cancel", "timeout"])
async def test_query_without_ionice_is_killed_on_cancel_and_timeout(it_env: ItEnv, service_files, game_servers,
                                                                   ending) -> None:
    """gs-noionice：真实选取（16:00–16:09 的分钟文件，含压缩包分支）重复到数千项，只匹配标记行。"""
    ip = it_env.gs_noionice_ip
    assert await _timeout_processes(game_servers, ip) == []
    conn = await SshConnector(service_files.load().ssh).connect(ip)
    try:
        listing = await run_listing(conn, build_listing_script(LOGS, ZIP), seconds=SECONDS)
        files = [f for f in select(listing, sample_logs.server_time(2026, 9, 17, 16, 0),
                                   sample_logs.server_time(2026, 9, 17, 16, 9)) if f.inode is None]
        assert len(files) == 9 and any(f.plain is None for f in files)
        q = build_query_script((files * (LONG_ENTRIES // len(files) + 1))[:LONG_ENTRIES], plain_dir=LOGS, zip_dir=ZIP,
                               include_patterns=[MARK], seconds=LONG_SECONDS, ionice=listing.ionice)
        assert "ionice" not in q.command
        sink = CollectingSink()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 4 if ending == "timeout" else None
        task = asyncio.create_task(run_query(conn, q, sink, deadline=deadline))
        await asyncio.wait_for(sink.began.wait(), 4)
        procs = await _timeout_processes(game_servers, ip)
        assert len(procs) == 1, procs
        pgid, cmdline = procs[0]
        assert cmdline == f"timeout -s KILL {LONG_SECONDS} nice -n 19 bash --noprofile --norc -s "
        assert await _group_members(game_servers, ip, pgid), "查询的进程组应仍在运行"
        assert not task.done()
        if ending == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            outcome = await task
            assert outcome.timed_out and not outcome.done and outcome.killed and outcome.pgid == pgid
            states = [f.state for f in outcome.files]
            assert FileState.READ in states and states[-1] is FileState.NOT_REACHED
            assert FileState.FAILED not in states
            for fo in outcome.files:
                if fo.state is FileState.READ:
                    assert sink.get(fo.file.index).count(MARK.encode()) == 1
        assert conn.closed
        await _assert_group_gone(game_servers, ip, pgid)
    finally:
        await conn.close()
    assert await _timeout_processes(game_servers, ip) == []
