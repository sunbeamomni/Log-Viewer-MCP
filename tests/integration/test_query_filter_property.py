"""属性 8 的集成测试（任务 6.9）：② 的过滤结果与 Python 参照实现逐字节相同。

每例在 gs-noionice 上的测试专用目录（PT8_ROOT，位于只读快照范围之外，不碰样例日志）写入随机的
Rotated、只在压缩目录中的压缩文件（偶尔是截断的坏压缩包）与 Current，然后按生产路径依次执行
①（run_listing）、select_files、build_query_script 与 run_query，结果交给内存中的写入接口
（失败的文件截回），与参照实现比较。

参照实现的语义（GNU grep 3.8、LC_ALL=C、-a）：
- 逐文件独立处理（上下文不跨文件）；内容按 LF 切成行，末尾没有 LF 的最后一行照样算一行，输出的每行都以 LF 结尾。
- 先排除：去掉匹配任一排除模式的行（grep -v）；再包含：保留匹配任一包含模式的行（没有包含模式时保留全部行），
  加上前 B 行、后 A 行的上下文；设置了上下文时，不相邻的两组输出之间有一行 "--"。
- LC_ALL=C 下按字节匹配；-i 只折叠 ASCII 大小写，参照为模式与行都做 bytes.lower() 后再匹配。
- -F：任一模式是行的子串；-E：只生成 Python re 与 POSIX ERE 语义一致的子集（字面字符、转义的元字符、"."、
  [abc]、[^x]、分组、单个 * + ?、|、分支首尾的 ^ $），没有 \\d、\\w、{n,m}、反向引用与叠加的量词。
- 内容不含 NUL（POSIX 语法下 "." 不匹配 NUL），其余字节（含 CR、TAB、0x80–0xFF 与 UTF-8 中文）都可出现。
- 同一时间戳两处都有时读非压缩文件；读取失败（坏压缩包）的文件整体截回，不计入结果。
"""

from __future__ import annotations

import base64
import gzip
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import pytest
from hypothesis import HealthCheck, event, example, given, settings
from hypothesis import strategies as st

from log_viewer_mcp.server.remote import (
    FileState,
    FileStatus,
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

# 测试专用目录：只在 gs-noionice 上使用；放在 /var/tmp 下，避开只读快照范围（SFS2X、/tmp、/root），
# 不影响同时在该主机上做快照对比或改动样例的其他测试。
PT8_ROOT = "/var/tmp/lvm-pt8"
PLAIN_DIR = f"{PT8_ROOT}/logs"
ZIP_DIR = f"{PT8_ROOT}/ziplogs"

SECONDS = 60
NOW = sample_logs.server_time(2026, 9, 17, 16, 30)
RANGE_START = sample_logs.server_time(2026, 9, 17, 15, 0)


# ---------------------------------------------------------------------------
# 参照实现
# ---------------------------------------------------------------------------


def split_lines(content: bytes) -> list[bytes]:
    """按 LF 切行（不含 LF）；末尾没有 LF 的最后一段也是一行。"""
    if not content:
        return []
    parts = content.split(b"\n")
    if parts[-1] == b"":
        parts.pop()
    return parts


def line_matcher(patterns: Sequence[str], *, regex: bool, ignore_case: bool) -> Callable[[bytes], bool]:
    pats = [p.encode("utf-8") for p in patterns]
    if ignore_case:
        pats = [p.lower() for p in pats]  # 只折叠 ASCII，与 LC_ALL=C 的 grep -i 一致
    if regex:
        compiled = [re.compile(p) for p in pats]

        def test(line: bytes) -> bool:
            return any(r.search(line) is not None for r in compiled)
    else:
        def test(line: bytes) -> bool:
            return any(p in line for p in pats)
    if ignore_case:
        return lambda line: test(line.lower())
    return test


@dataclass(frozen=True)
class Filter:
    include_patterns: tuple[str, ...]
    exclude_patterns: tuple[str, ...]
    regex: bool
    ignore_case: bool
    context_before: int
    context_after: int


def reference_grep(content: bytes, f: Filter) -> bytes:
    """一个文件经过 ``grep -v 排除 | grep [-B][-A] 包含`` 的输出。"""
    lines = split_lines(content)
    if f.exclude_patterns:
        excluded = line_matcher(f.exclude_patterns, regex=f.regex, ignore_case=f.ignore_case)
        lines = [line for line in lines if not excluded(line)]
    if f.include_patterns:
        included = line_matcher(f.include_patterns, regex=f.regex, ignore_case=f.ignore_case)
        matched = [included(line) for line in lines]
    else:
        matched = [True] * len(lines)
    selected: set[int] = set()
    for i, m in enumerate(matched):
        if m:
            selected.update(range(max(0, i - f.context_before), min(len(lines), i + f.context_after + 1)))
    context = f.context_before > 0 or f.context_after > 0
    out: list[bytes] = []
    prev: int | None = None
    for i in sorted(selected):
        if context and prev is not None and i != prev + 1:
            out.append(b"--\n")
        out.append(lines[i] + b"\n")
        prev = i
    return b"".join(out)


def test_reference_grep_examples() -> None:
    """参照实现自身的几个固定例子（不需要游戏服）。"""
    f = Filter(("a",), (), False, False, 1, 0)
    assert reference_grep(b"x\na\ny\nz\nq\na", f) == b"x\na\n--\nq\na\n"
    f = Filter(("E",), ("skip",), True, True, 0, 1)
    assert reference_grep(b"e1\nskip\nn\nn\nE2\n", f) == b"e1\nn\n--\nE2\n"
    assert reference_grep(b"", Filter((), (), False, False, 2, 2)) == b""
    assert reference_grep(b"\n\n", Filter((), (), False, False, 0, 0)) == b"\n\n"
    assert reference_grep(b"abc\n", Filter(("zzz",), (), False, False, 3, 3)) == b""


# ---------------------------------------------------------------------------
# 生成器
# ---------------------------------------------------------------------------

LINE_TOKENS = [
    b"INFO", b"ERROR", b"error", b"Warn", b"abc", b"aXc", b"a.c", b"xx", b"X", b"E", b"o", b"r",
    b":", b"=", b"-", b"--", b"_", b" ", b"  ", b"\t", b"\r", b"*", b"[", b"]", b"(", b")", b"|",
    b"^", b"$", b"\\", b"'", b'"', b".", b"-e", "中文".encode(), b"\xff", b"\x80",
]

lines_strategy = st.lists(st.lists(st.sampled_from(LINE_TOKENS), max_size=6).map(b"".join), max_size=25)


@st.composite
def contents(draw) -> bytes:
    lines = draw(lines_strategy)
    trailing_lf = draw(st.booleans()) or draw(st.booleans())  # 多数文件以 LF 结尾
    return b"\n".join(lines) + (b"\n" if lines and trailing_lf else b"")


FIXED_WORDS = ["INFO", "error", "ERROR", "abc", "a.c", "x", "X", "-e", "--", "中文", "o r", "[a]", "a*",
               "\\", "'", '"', "$(id)", "|", "^", "$"]
fixed_patterns = st.one_of(
    st.sampled_from(FIXED_WORDS),
    st.text(alphabet="aAbcxXEeINFOro:=-_ .*[]()|^$\\'\"中", min_size=1, max_size=5),
)

# 多字符的字面量（日志中出现的词）提高匹配的比例；其后的量词只作用于最后一个字符，两种语义相同
REGEX_LITERALS = list("abcxXEeINFOro:=_- ") + ["中", "ERROR", "error", "INFO", "abc", "Warn"]
REGEX_ESCAPED = ["\\.", "\\*", "\\[", "\\(", "\\)", "\\$", "\\^", "\\\\", "\\+", "\\?"]
BRACKET_CHARS = "abcxXEo.* "


@st.composite
def regex_alternation(draw, depth: int = 0) -> str:
    branches = []
    for _ in range(draw(st.integers(1, 3))):
        pieces = []
        for _ in range(draw(st.integers(1, 3))):
            kind = draw(st.sampled_from(["lit", "lit", "lit", "esc", "dot", "bracket", "group"]))
            if kind == "group" and depth >= 2:
                kind = "lit"
            if kind == "lit":
                atom = draw(st.sampled_from(REGEX_LITERALS))
            elif kind == "esc":
                atom = draw(st.sampled_from(REGEX_ESCAPED))
            elif kind == "dot":
                atom = "."
            elif kind == "bracket":
                chars = draw(st.lists(st.sampled_from(BRACKET_CHARS), min_size=1, max_size=3, unique=True))
                atom = "[" + ("^" if draw(st.booleans()) else "") + "".join(chars) + "]"
            else:
                atom = "(" + draw(regex_alternation(depth + 1)) + ")"
            pieces.append(atom + draw(st.sampled_from(["", "", "", "*", "+", "?"])))
        branch = "".join(pieces)
        if draw(st.integers(0, 4)) == 0:
            branch = "^" + branch
        if draw(st.integers(0, 4)) == 0:
            branch += "$"
        branches.append(branch)
    return "|".join(branches)


@st.composite
def filters(draw) -> Filter:
    regex = draw(st.booleans())
    pattern = regex_alternation() if regex else fixed_patterns
    return Filter(
        include_patterns=tuple(draw(st.lists(pattern, max_size=3))),
        exclude_patterns=tuple(draw(st.lists(pattern, max_size=2))),
        regex=regex,
        ignore_case=draw(st.booleans()),
        context_before=draw(st.integers(0, 3)),
        context_after=draw(st.integers(0, 3)),
    )


@dataclass(frozen=True)
class LogItem:
    """一个时段（ts 为 None 时是 Current）：kind 为 plain、archive、both 或 corrupt（只有截断的压缩包）。"""

    ts: str | None
    kind: str
    content: bytes
    other: bytes  # both 时压缩包中的内容（应当不被读取）

    @property
    def plain(self) -> str | None:
        if self.ts is None:
            return "smartfox.log"
        return f"smartfox.log.{self.ts}" if self.kind in ("plain", "both") else None

    @property
    def archive(self) -> str | None:
        return f"smartfox.log.{self.ts}.tar.gz" if self.ts is not None and self.kind != "plain" else None

    @property
    def name(self) -> str:
        return self.plain or self.archive  # type: ignore[return-value]

    @property
    def ok(self) -> bool:
        return self.kind != "corrupt"


@st.composite
def log_items(draw) -> list[LogItem]:
    minutes = sorted(draw(st.lists(st.integers(0, 20), max_size=4, unique=True)))
    has_current = draw(st.booleans()) or not minutes
    items = [
        LogItem(f"2026-09-17-16-{m:02d}",
                draw(st.sampled_from(["plain", "plain", "archive", "both", "corrupt"])),
                draw(contents()), draw(contents()))
        for m in minutes
    ]
    if has_current:
        items.append(LogItem(None, "plain", draw(contents()), b""))
    return items


# ---------------------------------------------------------------------------
# 游戏服操作与写入接口
# ---------------------------------------------------------------------------


def _put(path: str, data: bytes) -> str:
    return f"printf '%s' '{base64.b64encode(data).decode('ascii')}' | base64 -d >{path} || exit 1"


def _corrupt_gzip(content: bytes) -> bytes:
    # 去掉尾部 ISIZE 的 4 个字节：仍以 gzip 魔数开头，gzip -dcf 报 unexpected end of file 并以非 0 退出
    return gzip.compress(content, mtime=0)[:-4]


def write_script(items: Sequence[LogItem]) -> str:
    lines = [f"rm -rf -- {PT8_ROOT} && mkdir -p -- {PLAIN_DIR} {ZIP_DIR} || exit 1"]
    for item in items:
        if item.plain is not None:
            lines.append(_put(f"{PLAIN_DIR}/{item.plain}", item.content))
        if item.kind == "archive":
            lines.append(_put(f"{ZIP_DIR}/{item.archive}", gzip.compress(item.content, mtime=0)))
        elif item.kind == "both":
            lines.append(_put(f"{ZIP_DIR}/{item.archive}", gzip.compress(item.other, mtime=0)))
        elif item.kind == "corrupt":
            lines.append(_put(f"{ZIP_DIR}/{item.archive}", _corrupt_gzip(item.content)))
    return "\n".join(lines) + "\n"


CLEANUP_SCRIPT = f"rm -rf -- {PT8_ROOT}\n"


class MemorySink:
    """内存中的写入接口：失败的文件截回到 begin_file 时的位置（与结果文件的语义相同）。"""

    def __init__(self) -> None:
        self.data = bytearray()
        self.offset: int | None = None
        self.current: int | None = None
        self.statuses: dict[int, FileStatus] = {}

    def begin_file(self, index: int, file: QueryFile) -> None:
        assert self.current is None
        self.current, self.offset = index, len(self.data)

    def write(self, index: int, data: bytes) -> None:
        assert index == self.current
        self.data += data

    def end_file(self, index: int, status: FileStatus) -> None:
        assert index == self.current and self.offset is not None
        if not status.ok:
            del self.data[self.offset:]
        self.statuses[index] = status
        self.current = self.offset = None

    def interrupt_file(self, index: int) -> None:
        raise AssertionError("deadline=None 时不应超时")


@pytest.fixture
def pt8_host(it_env: ItEnv, game_servers: GameServers):
    ip = it_env.gs_noionice_ip
    run_sync(game_servers.run_ok(ip, CLEANUP_SCRIPT))
    yield ip
    run_sync(game_servers.run_ok(ip, CLEANUP_SCRIPT))


async def _run_example(ip: str, game_servers: GameServers, ssh_config, items: list[LogItem], f: Filter):
    await game_servers.run_ok(ip, write_script(items))
    connector = SshConnector(ssh_config)
    async with await connector.connect(ip) as conn:
        listing = await run_listing(conn, build_listing_script(PLAIN_DIR, ZIP_DIR), seconds=SECONDS)
        selection = select_files(listing.plain, listing.archives, start=RANGE_START, end=NOW, now=NOW,
                                 max_scan_bytes=1 << 40, suffix="", tz=sample_logs.SERVER_TZ)
        query = build_query_script(
            selection.files, plain_dir=PLAIN_DIR, zip_dir=ZIP_DIR,
            include_patterns=list(f.include_patterns), exclude_patterns=list(f.exclude_patterns),
            regex=f.regex, ignore_case=f.ignore_case,
            context_before=f.context_before, context_after=f.context_after,
            seconds=SECONDS, ionice=listing.ionice,
        )
        sink = MemorySink()
        outcome = await run_query(conn, query, sink, deadline=None)
    return listing, selection, outcome, sink


# Feature: log-viewer-mcp, Property 8: 过滤结果与参照 grep 一致
@settings(deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow,
                                                HealthCheck.data_too_large])
@given(items=log_items(), f=filters())
# 固定一例必定含 "--" 分隔行、坏压缩包截回、两处都有时读非压缩文件、末尾没有 LF 的行与 Current
@example(
    items=[LogItem("2026-09-17-16-01", "both", b"x\nERROR a\ny\nz\nq\nerror b", b"ERROR other\n"),
           LogItem("2026-09-17-16-02", "corrupt", b"ERROR c\n" * 50, b""),
           LogItem("2026-09-17-16-03", "archive", b"Error d\n\xff\r\n", b""),
           LogItem(None, "plain", "中文 ERROR\nskip ERROR\n".encode(), b"")],
    f=Filter(("err(or)?",), ("skip",), True, True, 1, 0),
)
def test_filter_matches_reference_grep(it_env: ItEnv, service_files, game_servers: GameServers, pt8_host: str,
                                       items: list[LogItem], f: Filter) -> None:
    """**Validates: Requirements 1.4, 1.5**"""
    listing, selection, outcome, sink = run_sync(
        _run_example(pt8_host, game_servers, service_files.load().ssh, items, f))
    assert not listing.ionice  # gs-noionice：② 不加 ionice
    assert [s.name for s in selection.files] == [item.name for item in items]
    assert outcome.done and not outcome.timed_out
    assert [o.state for o in outcome.files] == [FileState.READ if item.ok else FileState.FAILED for item in items]
    for o, item in zip(outcome.files, items):
        assert o.status is not None
        assert o.status.branch == ("F" if item.plain is not None else "Z"), item.name
    expected = b"".join(reference_grep(item.content, f) for item in items if item.ok)
    event("结果为空" if not expected else ("含 -- 分隔行" if b"\n--\n" in b"\n" + expected else "有匹配行"))
    assert bytes(sink.data) == expected
