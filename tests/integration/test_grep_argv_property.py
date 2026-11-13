"""Property 2：模式逐字节到达 grep（任务 6.7，需求 8.3、8.5、8.9）。

集成测试，只在 gs-grepspy（grep 换成"记录 argv 后 exec 真 grep"的替身，见 README.md「grep 调用记录」）上运行。
每个例子：

1. 用 Hypothesis 生成 include_patterns、exclude_patterns（含 8.9 所列的 Shell 元字符、以 "-" 开头的值、
   中文与长度等于 Max_Pattern_Length 的值）、regex、ignore_case 与上下文行数，先经 validate_search_logs 校验；
   regex 为 true 但模式不是合法的扩展正则时改用 regex=false（任何通过校验的模式都可用于 -F）。
2. 清空 grep 调用记录，在 gs-grepspy 上依次执行 ① 与 ②（只选一个非压缩文件，保证 grep 一定执行）。
3. 读取 .argv 记录：排除段的 argv 应恰为 ``-a -F|-E [-i] -v -e p1 -e p2 … --``，包含段为
   ``-a -F|-E [-i] [-B n] [-A n] -e p1 … --``（没有包含模式时为 ``-e ''``）；每个模式是一个独立参数，
   与其 UTF-8 编码逐字节相同。① 只用 ``command -v grep`` 探测，不执行 grep，所以记录全部来自 ②。
4. 交给 asyncssh 的命令行（SSHClientConnection.create_process 的参数）应与 build_command 的输出相同：
   ① 为 ``build_command(秒数)``，② 为 ``build_command(秒数, ionice=探测结果)``，与请求值无关（8.3）。
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

import asyncssh
import pytest
from hypothesis import HealthCheck, event, example, given, settings
from hypothesis import strategies as st

from log_viewer_mcp.common.errors import ErrorCode, LogViewerError
from log_viewer_mcp.server.remote import (
    FileState,
    FileStatus,
    QueryFile,
    SshConnector,
    build_command,
    build_listing_script,
    build_query_script,
    is_fixed_command,
    run_listing,
    run_query,
)
from log_viewer_mcp.server.select import SelectedFile
from log_viewer_mcp.server.validate import validate_search_logs
from tests.integration import sample_logs
from tests.integration.itenv import DEFAULT_LIMITS, ItEnv, run_sync

SECONDS = 60
LOG_TYPE = "normal"
SVR_ID = 1003  # tbl_webserver 中指向 gs-grepspy 的记录（只作参数校验用）
MAX_LEN = DEFAULT_LIMITS["max_pattern_length"]
MAX_PATTERNS = DEFAULT_LIMITS["max_patterns"]
MAX_CONTEXT = DEFAULT_LIMITS["max_context_lines"]

# 需求 8.9 所列字符：分号、竖线、与号、美元符、反引号、括号、尖括号、星号、问号、方括号、花括号、
# 波浪号、感叹号、井号、单双引号、反斜杠与空格。
SPECIAL_CHARS = ";|&$`()<>*?[]{}~!#'\"\\ "

SNIPPETS = [
    "$(id)", "`id`", "${IFS}", "$HOME", "$'\\x41'", "; rm -rf / #", "a|b", "a && b", "a || b", "&",
    "<(cat /etc/passwd)", ">/tmp/pwned", ">>x", "2>&1", "*", "?", "[a-z]", "{a,b}", "~root", "!!", "!$",
    "#comment", "'", "''", "\"", "\\", "\\'", "'\\''", "\" ; echo pwned ; \"", "' ; echo pwned ; '",
    "-", "--", "-e", "-v", "-f", "-r", "--help", "--version", "--file=/etc/passwd", "-e x", "-E", "-P",
    "中文", "错误 日志", "空指针异常", "SAMPLE_MARK", "ERROR", "INFO",
]

chars = st.one_of(
    st.sampled_from(SPECIAL_CHARS),
    st.characters(min_codepoint=0x4E00, max_codepoint=0x9FFF),
    # 通过校验的任意字符：可编码为 UTF-8（排除孤立代理项），不含 Cc 类字符
    st.characters(codec="utf-8", exclude_categories=("Cc", "Cs")),
)
fragment = st.one_of(st.sampled_from(SNIPPETS), st.text(chars, min_size=1, max_size=12))
body = st.lists(fragment, min_size=1, max_size=6).map("".join)

patterns_one = st.one_of(
    body.map(lambda s: s[:MAX_LEN]),
    body.map(lambda s: ("-" + s)[:MAX_LEN]),
    # 长度等于上限（按字符计）
    body.map(lambda s: (s * MAX_LEN)[:MAX_LEN]),
    st.text(chars, min_size=MAX_LEN, max_size=MAX_LEN),
)
pattern_lists = st.lists(patterns_one, min_size=0, max_size=MAX_PATTERNS)
context = st.one_of(st.just(0), st.integers(0, MAX_CONTEXT))

ALL_SPECIAL = SPECIAL_CHARS + "-x 中文"  # 一个模式含 8.9 的全部字符


class MemorySink:
    """在内存中收集 ② 的输出与结束状态。"""

    def __init__(self) -> None:
        self.data: dict[int, bytearray] = {}
        self.status: dict[int, FileStatus] = {}

    def begin_file(self, index: int, file: QueryFile) -> None:
        self.data[index] = bytearray()

    def write(self, index: int, data: bytes) -> None:
        self.data[index] += data

    def end_file(self, index: int, status: FileStatus) -> None:
        self.status[index] = status

    def interrupt_file(self, index: int) -> None:
        raise AssertionError("不应超时")


@contextlib.contextmanager
def capture_commands() -> Iterator[list[Any]]:
    """记录交给 asyncssh 的命令行（SSHClientConnection.create_process 的 command 参数）。"""
    commands: list[Any] = []
    original = asyncssh.SSHClientConnection.create_process

    async def spy(self, *args: Any, **kwargs: Any):
        commands.append(args[0] if args else kwargs.get("command"))
        return await original(self, *args, **kwargs)

    with mock.patch.object(asyncssh.SSHClientConnection, "create_process", spy):
        yield commands


def clear_records(directory: Path) -> None:
    for entry in directory.iterdir():
        if entry.name.endswith((".argv", ".tmp")):
            entry.unlink(missing_ok=True)


def read_records(directory: Path) -> list[list[bytes]]:
    """各次 grep 调用的 argv[1..]（每个参数以 NUL 结尾；零个参数时为空文件）。"""
    assert not [e.name for e in directory.iterdir() if e.name.endswith(".tmp")], "有未写完的记录"
    records = []
    for entry in sorted(directory.iterdir()):
        if not entry.name.endswith(".argv"):
            continue
        fields = entry.read_bytes().split(b"\0")
        assert fields[-1] == b"", f"记录 {entry.name} 不以 NUL 结尾"
        records.append(fields[:-1])
    return records


def expected_argv(include: tuple[str, ...], exclude: tuple[str, ...], *, regex: bool, ignore_case: bool,
                  before: int, after: int) -> list[list[bytes]]:
    """design.md「固定命令行」中 ② 的模板：排除段与包含段的 grep 参数。"""
    common = [b"-a", b"-E" if regex else b"-F"] + ([b"-i"] if ignore_case else [])

    def es(values: tuple[str, ...]) -> list[bytes]:
        return [a for v in values for a in (b"-e", v.encode("utf-8"))]

    result = []
    if exclude:
        result.append(common + [b"-v"] + es(exclude) + [b"--"])
    opts = ([b"-B", b"%d" % before] if before > 0 else []) + ([b"-A", b"%d" % after] if after > 0 else [])
    result.append(common + opts + (es(include) if include else [b"-e", b""]) + [b"--"])
    return result


def validated(include: list[str], exclude: list[str], *, regex: bool, ignore_case: bool, before: int,
              after: int, config) -> Any:
    args: dict[str, Any] = {"svr_ids": [SVR_ID], "log_type": LOG_TYPE, "regex": regex, "ignore_case": ignore_case,
                            "context_before": before, "context_after": after}
    if include:
        args["include_patterns"] = include
    if exclude:
        args["exclude_patterns"] = exclude
    return validate_search_logs(args, limits=config.limits, log_types=config.log_types.keys())


async def run_once(it_env: ItEnv, connector: SshConnector, params, argv_dir: Path):
    plain_dir = sample_logs.LOG_TYPES[LOG_TYPE].dir
    clear_records(argv_dir)
    with capture_commands() as commands:
        async with await connector.connect(it_env.gs_grepspy_ip) as conn:
            listing = await run_listing(conn, build_listing_script(plain_dir, sample_logs.ZIP_DIR), seconds=SECONDS)
            # 一个非压缩的分钟文件（最小的那个），读取分支为 F，grep 一定执行
            rotated = [e for e in listing.plain if e.name.startswith("smartfox.log.2026-09-17-16-")]
            assert rotated, "gs-grepspy 上没有样例分钟文件"
            entry = min(rotated, key=lambda e: (e.size, e.name))
            ts = entry.name.removeprefix("smartfox.log.")
            files = (SelectedFile(plain=entry.name, archive=None, inode=None, ts=ts, start=0, end=1,
                                  size=entry.size),)
            q = build_query_script(
                files, plain_dir=plain_dir, zip_dir=sample_logs.ZIP_DIR,
                include_patterns=params.include_patterns, exclude_patterns=params.exclude_patterns,
                regex=params.regex, ignore_case=params.ignore_case,
                context_before=params.context_before, context_after=params.context_after,
                seconds=SECONDS, ionice=listing.ionice,
            )
            sink = MemorySink()
            outcome = await run_query(conn, q, sink, deadline=None)
    return listing, q, outcome, sink, commands, read_records(argv_dir)


@pytest.fixture
def grepspy_ready(it_env: ItEnv) -> Path:
    argv_dir = Path(it_env.grep_argv_dir)
    assert argv_dir.is_dir() and os.access(argv_dir, os.W_OK | os.X_OK), "grep 调用记录目录不可写"
    return argv_dir


# Feature: log-viewer-mcp, Property 2: 模式逐字节到达 grep
@settings(deadline=None, database=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow,
                                 HealthCheck.large_base_example, HealthCheck.data_too_large])
@given(include=pattern_lists, exclude=pattern_lists, regex=st.booleans(), ignore_case=st.booleans(),
       before=context, after=context)
@example(include=[ALL_SPECIAL, "-v", "--", "-e"], exclude=["中" * MAX_LEN, "-f/etc/passwd"],
         regex=False, ignore_case=True, before=3, after=MAX_CONTEXT)
@example(include=[], exclude=[], regex=True, ignore_case=False, before=0, after=0)
@example(include=["-(a|b)*[0-9]{1,3}$", "^中文.+错误"], exclude=["x" * MAX_LEN], regex=True, ignore_case=False,
         before=0, after=1)
def test_patterns_reach_grep_byte_for_byte(it_env: ItEnv, service_files, grepspy_ready: Path,
                                           include: list[str], exclude: list[str], regex: bool, ignore_case: bool,
                                           before: int, after: int) -> None:
    """**Validates: Requirements 8.3, 8.5, 8.9**"""
    config = service_files.load()
    if regex:
        try:
            params = validated(include, exclude, regex=True, ignore_case=ignore_case, before=before, after=after,
                               config=config)
            event("regex=true")
        except LogViewerError as e:
            assert e.code is ErrorCode.INVALID_ARGUMENT
            regex = False
            event("regex=true 不合法，改用 regex=false")
    if not regex:
        # 生成的模式都符合校验规则：regex=false 时必须通过
        params = validated(include, exclude, regex=False, ignore_case=ignore_case, before=before, after=after,
                           config=config)
        event("regex=false")
    assert params.include_patterns == tuple(include) and params.exclude_patterns == tuple(exclude)

    connector = SshConnector(config.ssh)
    listing, q, outcome, sink, commands, records = run_sync(run_once(it_env, connector, params, grepspy_ready))

    # ② 正常完成，读取的是非压缩文件，grep 各段正常退出（0 有匹配，1 无匹配）
    assert outcome.done and not outcome.timed_out
    assert [f.state for f in outcome.files] == [FileState.READ]
    status = sink.status[0]
    assert status.branch == "F" and status.gzip == 0
    assert len(status.grep) == (2 if exclude else 1) and all(c in (0, 1) for c in status.grep), status

    # grep 收到的参数：每个模式一个独立参数，与其 UTF-8 编码逐字节相同（8.5、8.9）
    want = expected_argv(params.include_patterns, params.exclude_patterns, regex=params.regex,
                         ignore_case=params.ignore_case, before=params.context_before, after=params.context_after)
    assert sorted(records) == sorted(want)

    # 交给 asyncssh 的命令行是固定内容：只由秒数与 ionice 探测结果决定，不含请求值（8.3）
    assert commands == [build_command(SECONDS), build_command(SECONDS, ionice=listing.ionice)]
    assert all(is_fixed_command(c) for c in commands)
    for p in params.include_patterns + params.exclude_patterns:
        # 只由 [A-Za-z0-9 /:=._-] 组成的短模式（如 "e"）可能恰是固定内容的子串，上面的相等已排除其影响
        if not is_fixed_command(p):
            assert all(p not in c for c in commands)
