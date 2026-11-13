"""Property 3：只读不变量（任务 6.8）。

**Validates: Requirements 2.4, 8.4, 8.6, 8.10**

在 gs（以 root 登录，样例文件权限 777）上按真实流程执行查询：validate_search_logs → compute_time_range →
① run_listing → select_files → build_query_script → ② run_query（写入内存中的 sink），结束方式为正常完成、
超时（很短的截止时刻）或取消（收到第一个 FILE 后，或随机的短延时后）。每例之后对日志目录、ziplogs、
/tmp 与 /root 拍快照（文件集合、内容、大小、权限、属主、mtime，不比较 atime），与该例开始前的快照比较。

快照方式：链式快照，每例只拍一次。测试开始前拍基准快照；每例结束后拍一次，与上一例结束时（第一例为基准）
的快照比较，然后把它作为下一例的"之前"。快照是确定性的（没有改动时两次结果相同，已由 6.2、6.3、6.6 的
assert_unchanged 验证），所以"上一例之后"就是"本例之前"，省掉每例一半的快照开销；同时每例只与紧邻的前一
状态比较，某例造成的改动只会算在该例上，Hypothesis 缩减时不会把之前的改动误算给别的例子。所有例子通过即
说明最终状态与基准相同；测试结束时再拍一次与基准比较，兜住最后一例之后才发生的改动。

运行时长：每例一次 SSH 连接（① 与 ②）加一次快照连接，实测每例约 0.2–0.4 秒，100 例约 20 秒；
因此取 200 例（约 40 秒），让各结束方式都有足够的例子。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from hypothesis import HealthCheck, assume, event, given, settings
from hypothesis import strategies as st

from log_viewer_mcp.common.errors import ErrorCode, LogViewerError
from log_viewer_mcp.server.remote import (
    FileStatus,
    QueryFile,
    SshConnector,
    build_listing_script,
    build_query_script,
    remote_seconds,
    run_listing,
    run_query,
)
from log_viewer_mcp.server.select import select_files
from log_viewer_mcp.server.validate import compute_time_range, validate_search_logs
from tests.integration import sample_logs
from tests.integration.itenv import GameServers, ItEnv, Snapshot, diff_snapshots, run_sync

LISTING_SECONDS = 30
QUERY_SECONDS = 30
NOW = sample_logs.GENERATED_AT  # 样例的"当前时刻"（Time_Range 与 Current 的终点）
MAX_PATTERN_LENGTH = 256  # 与 itenv.DEFAULT_LIMITS 一致
MAX_CONTEXT = 100

st_time = sample_logs.server_time

# 样例时段表中的关键时刻（服务器时间）：只在 ziplogs 的天与小时、两处都有、只在非压缩目录、
# 没有文件的 -13 与 16-05、normal 的损坏压缩包 09-16-09、分钟文件与 Current。
ANCHORS: tuple[int, ...] = (
    st_time(2026, 9, 14), st_time(2026, 9, 14, 12), st_time(2026, 9, 15), st_time(2026, 9, 15, 23, 30),
    st_time(2026, 9, 16, 9), st_time(2026, 9, 16, 9, 30), st_time(2026, 9, 16, 10), st_time(2026, 9, 16, 11),
    st_time(2026, 9, 16, 12), st_time(2026, 9, 16, 13), st_time(2026, 9, 16, 14),
    *(st_time(2026, 9, 17, 16, m) for m in range(0, 13)),
    NOW,
)

# 注入式的模式：Shell 片段、重定向、命令替换、选项、路径、伪造帧行等；只作为 grep 的匹配内容。
INJECTIONS: tuple[str, ...] = (
    "rm -rf /",
    "rm -rf /usr/local/ds/SFS2X/logs/*",
    "> /tmp/x",
    ">> /root/.bashrc",
    "$(touch /tmp/pwned)",
    "`touch /tmp/pwned`",
    "; :> file",
    "; :> /usr/local/ds/SFS2X/logs/smartfox.log",
    "| tee /tmp/tee",
    "&& chmod 000 /usr/local/ds/SFS2X/ziplogs",
    "' ; rm -rf / ; '",
    '" ; rm -rf / ; "',
    "${IFS}touch${IFS}/tmp/p",
    "<(touch /tmp/ps)",
    "exec 3>/tmp/fd",
    "mv smartfox.log smartfox.log.bak",
    "kill -9 -1",
    "-f /etc/passwd",
    "--help",
    "-r",
    "--",
    "-",
    "*",
    "../../etc/passwd",
    "\\$(id)",
    "!!",
    "~root",
    "#",
    "{a,b}",
    "a b",
    "中文 联盟名称=龙之谷",
    sample_logs.FAKE_BOUNDARY + " DONE",
)
KEYWORDS: tuple[str, ...] = (
    "CMD", "uid=123", sample_logs.MARKER_PREFIX, "Exception", "PUSHINIT", "ANALYSISLOG", "LogSendService",
    "ERROR", "INFO", "user.login", sample_logs.IGNORED_MARKER, "at ",
)
# regex=true 时直接使用的扩展正则（都合法，不含反向引用）。
ERE_PATTERNS: tuple[str, ...] = (
    "CMD|PUSH", "uid=1[0-9]+", "^2026-09-17 16:0[0-9]", "Exception$", "(ERROR|WARN) +\\|", "[[:digit:]]{3}",
    ".*", "a{1,3}", "rm -rf .*", "\\$\\(touch /tmp/pwned\\)", "^[^|]+\\| INFO", "SAMPLE_MARK file=[a-z]+/",
    "(;|&|`|>)", "[]x[]", "^$",
)
_TEXT_ALPHABET = "abzCMDIuid019 ;|&$`()<>*?[]{}~!#'\"\\-/=._中文"
_ERE_SPECIAL = set(".[]()*+?{}|^$")


def ere_escape(text: str) -> str:
    """把字面文本转成匹配同样内容的扩展正则：ERE 的特殊字符加反斜杠，反斜杠写成 [\\]
    （写成 \\\\ 时后面若跟数字，会被反向引用的保守规则拒绝）。"""
    return "".join("[\\]" if c == "\\" else "\\" + c if c in _ERE_SPECIAL else c for c in text)


_literal = st.one_of(
    st.sampled_from(INJECTIONS),
    st.sampled_from(KEYWORDS),
    st.text(alphabet=_TEXT_ALPHABET, min_size=1, max_size=40),
)


@st.composite
def patterns(draw, regex: bool) -> list[str]:
    def one() -> str:
        if regex:  # 转义后的字面文本至多 120 个字符，不超过上限
            return draw(st.one_of(st.sampled_from(ERE_PATTERNS), _literal.map(ere_escape)))
        if draw(st.integers(0, 19)) == 0:  # 长度恰为上限的值
            return ("rm -rf / ; " * 30)[:MAX_PATTERN_LENGTH]
        return draw(_literal)

    n = draw(st.integers(1, 10) if draw(st.integers(0, 9)) == 0 else st.integers(1, 3))
    return [one() for _ in range(n)]


@st.composite
def time_args(draw) -> dict:
    kind = draw(st.sampled_from(("none", "at", "start", "range")))
    if kind == "at":
        return {"at_time": draw(st.sampled_from(ANCHORS)) + draw(st.integers(-3600, 3600))}
    if kind == "start":
        return {"start_time": draw(st.integers(NOW - 86400, NOW - 1))}
    if kind == "range":
        start = draw(st.sampled_from(ANCHORS)) + draw(st.integers(-7200, 7200))
        span = draw(st.one_of(st.integers(1, 3600), st.integers(1, 86400)))
        return {"start_time": start, "end_time": start + span}
    return {}


@st.composite
def search_args(draw) -> dict:
    """合法的 search_logs 原始参数字典（未提供的可选参数有时显式给 null）。"""
    args: dict = {
        "svr_ids": draw(st.lists(st.one_of(st.just(sample_logs.SVR_ID), st.integers(1, 4294967295)),
                                 min_size=1, max_size=10)),
        "log_type": draw(st.sampled_from(sorted(sample_logs.LOG_TYPES))),
    }
    args.update(draw(time_args()))
    regex = draw(st.booleans())
    if regex or draw(st.booleans()):
        args["regex"] = regex
    for name in ("include_patterns", "exclude_patterns"):
        if draw(st.booleans()):
            args[name] = draw(patterns(regex))
    if draw(st.booleans()):
        args["ignore_case"] = draw(st.booleans())
    for name in ("context_before", "context_after"):
        if draw(st.booleans()):
            args[name] = draw(st.one_of(st.integers(0, 3), st.integers(0, MAX_CONTEXT)))
    for name in ("at_time", "start_time", "end_time", "include_patterns", "exclude_patterns", "regex",
                 "ignore_case", "context_before", "context_after"):
        if name not in args and draw(st.integers(0, 7)) == 0:
            args[name] = None  # null 视为未提供
    return args


@dataclass(frozen=True)
class EndMode:
    kind: str  # complete、timeout、cancel_first_file、cancel_delay
    delay: float = 0.0  # timeout：截止时刻距 ② 开始的秒数；cancel_delay：取消前等待的秒数；其余不用


# 样例上的一次 ② 通常在零点几秒内完成：延时以几十毫秒为主，使截止时刻与取消多落在读取过程中。
_delays = st.one_of(st.sampled_from((0.0, 0.01, 0.02, 0.04, 0.06, 0.08, 0.1, 0.15, 0.2)), st.floats(0.0, 0.6))

end_modes = st.builds(
    EndMode, st.sampled_from(("complete", "timeout", "cancel_first_file", "cancel_delay")), _delays,
)


class MemorySink:
    """ResultSink 的内存实现：失败的文件截回，超时截到最后一个完整行。"""

    def __init__(self) -> None:
        self.data = bytearray()
        self.offsets: dict[int, int] = {}
        self.open: int | None = None
        self.began = asyncio.Event()

    def begin_file(self, index: int, file: QueryFile) -> None:
        assert self.open is None
        self.offsets[index] = len(self.data)
        self.open = index
        self.began.set()

    def write(self, index: int, data: bytes) -> None:
        assert index == self.open
        self.data += data

    def end_file(self, index: int, status: FileStatus) -> None:
        assert index == self.open
        if not status.ok:
            del self.data[self.offsets[index]:]
        self.open = None

    def interrupt_file(self, index: int) -> None:
        assert index == self.open
        cut = self.data.rfind(b"\n", self.offsets[index])
        del self.data[(cut + 1) if cut >= 0 else self.offsets[index]:]
        self.open = None


async def _query(conn, query, mode: EndMode) -> None:
    """按结束方式执行 ②；只检查结束方式本身符合 run_query 的约定，读写内容不在本属性范围内。"""
    sink = MemorySink()
    if mode.kind in ("complete", "timeout"):
        deadline = asyncio.get_running_loop().time() + mode.delay if mode.kind == "timeout" else None
        try:
            outcome = await run_query(conn, query, sink, deadline=deadline)
        except LogViewerError as e:
            assert mode.kind == "timeout" and e.code is ErrorCode.TIMEOUT, e.code
            event("end: timeout before first FILE")
            return
        if mode.kind == "complete":
            assert outcome.done and not outcome.timed_out
            event("end: complete")
        else:
            assert outcome.done or outcome.timed_out
            event("end: timeout truncated" if outcome.timed_out else "end: finished before deadline")
        return

    task = asyncio.create_task(run_query(conn, query, sink))
    if mode.kind == "cancel_first_file":
        began = asyncio.create_task(sink.began.wait())
        await asyncio.wait({task, began}, return_when=asyncio.FIRST_COMPLETED, timeout=30)
        began.cancel()
    else:
        await asyncio.sleep(mode.delay)
    task.cancel()
    try:
        outcome = await task
    except asyncio.CancelledError:
        event("end: cancelled")
    else:
        assert outcome.done
        event("end: finished before cancel")


async def _example(config, ip: str, params, mode: EndMode) -> None:
    tr = compute_time_range(params, NOW, config.limits)
    lt = config.log_types[params.log_type]
    conn = await SshConnector(config.ssh).connect(ip)
    try:
        listing = await run_listing(conn, build_listing_script(lt.dir, config.zip_dir), seconds=LISTING_SECONDS)
        selection = select_files(
            listing.plain, listing.archives, start=tr.start, end=tr.end, now=NOW,
            max_scan_bytes=config.limits.max_scan_bytes, suffix=lt.suffix, tz=config.tz,
        )
        if not selection.files:
            event("no files selected (② not run)")
            return
        event("files selected")
        seconds = remote_seconds(mode.delay if mode.kind == "timeout" else QUERY_SECONDS)
        query = build_query_script(
            selection.files, plain_dir=lt.dir, zip_dir=config.zip_dir,
            include_patterns=params.include_patterns, exclude_patterns=params.exclude_patterns,
            regex=params.regex, ignore_case=params.ignore_case,
            context_before=params.context_before, context_after=params.context_after,
            seconds=seconds, ionice=listing.ionice,
        )
        await _query(conn, query, mode)
    finally:
        await conn.close()


@dataclass
class SnapshotChain:
    """链式快照：baseline 为测试开始前的快照，last 为上一例结束后的快照（即本例开始前的状态）。"""

    baseline: Snapshot
    last: Snapshot


@pytest.fixture
def snapshot_chain(it_env: ItEnv, game_servers: GameServers) -> Iterator[SnapshotChain]:
    baseline = run_sync(game_servers.snapshot(it_env.gs_ip))
    chain = SnapshotChain(baseline, baseline)
    yield chain
    final = run_sync(game_servers.snapshot(it_env.gs_ip))
    diffs = diff_snapshots(baseline, final)
    assert not diffs, f"{it_env.gs_ip} 上的文件在测试期间被改动：\n" + "\n".join(diffs[:50])


# Feature: log-viewer-mcp, Property 3: 只读不变量
@settings(
    # 每例约 0.2–0.4 秒，取 200 例（不少于 100），使四种结束方式与四种 log_type 都有足够的例子
    max_examples=200,
    deadline=None,
    database=None,  # 仓库在测试运行容器中以只读方式挂载
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(args=search_args(), mode=end_modes)
def test_query_never_modifies_game_server(it_env: ItEnv, service_files, game_servers: GameServers,
                                          snapshot_chain: SnapshotChain, args: dict, mode: EndMode) -> None:
    config = service_files.load()
    try:
        params = validate_search_logs(args, limits=config.limits, log_types=tuple(config.log_types))
    except LogViewerError as e:
        # 只有 regex=true 时由 grep 判定非法的扩展正则可能被拒绝（生成器按构造只出合法值）
        assert e.code is ErrorCode.INVALID_ARGUMENT and args.get("regex") is True, (e.code, e.message, args)
        assume(False)
    event(f"log_type={params.log_type}")
    event(f"mode={mode.kind}")

    ip = it_env.gs_ip

    async def body() -> Snapshot:
        await _example(config, ip, params, mode)
        return await game_servers.snapshot(ip)

    after = run_sync(body())
    before, snapshot_chain.last = snapshot_chain.last, after
    diffs = diff_snapshots(before, after)
    assert not diffs, f"{ip} 上的文件被改动：\n" + "\n".join(diffs[:50])
