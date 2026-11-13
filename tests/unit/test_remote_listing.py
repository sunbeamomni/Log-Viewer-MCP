"""server/remote.py 的固定命令行与 ① 列目录和探测（任务 6.2）：命令行、脚本与 NUL 记录解析。"""

from __future__ import annotations

import pytest

from log_viewer_mcp.common.errors import ErrorCode, LogViewerError
from log_viewer_mcp.server import remote
from log_viewer_mcp.server.remote import (
    ArchiveEntry,
    Listing,
    ListingParser,
    PlainEntry,
    RemoteExit,
    build_command,
    build_listing_script,
    is_fixed_command,
    is_safe_file_name,
    remote_seconds,
)

OK = RemoteExit(0, None, 0)

# 登录 Shell（~/.bashrc）在脚本之前打印的内容，含形似记录的文本（与测试镜像的 root.bashrc 相同风格）
BASHRC_NOISE = (
    b"Welcome -- \xe8\xbf\x99\xe6\x98\xaf ~/.bashrc\n"
    b"MISSING grep\nL 1 2 smartfox.log\nEXIT L 0\nIONICE\n"
)

NORMAL = (
    b"IONICE\0"
    b"L 1234567 52311 smartfox.log\0"
    b"L 1234568 100 smartfox.log.2026-09-17-16\0"
    b"L 99 7 gc.log\0"
    b"EXIT L 0\0"
    b"Z 4096 smartfox.log.2026-09-17-15.tar.gz\0"
    b"EXIT Z 0\0"
)


def parse(payload: bytes, exit: RemoteExit = OK, *, noise: bytes = BASHRC_NOISE, chunk: int | None = None) -> Listing:
    data = noise + b"\0" + payload
    parser = ListingParser("10.0.0.1")
    step = chunk or len(data) or 1
    for i in range(0, len(data), step):
        parser.feed(data[i:i + step])
    return parser.finish(exit)


def error_of(payload: bytes, exit: RemoteExit = OK, **kwargs) -> ErrorCode:
    with pytest.raises(LogViewerError) as ei:
        parse(payload, exit, **kwargs)
    return ei.value.code


# ---------------------------------------------------------------------------
# 固定命令行
# ---------------------------------------------------------------------------


def test_build_command_is_fixed_and_matches_design() -> None:
    assert build_command(45) == "env -i PATH=/usr/bin:/bin LC_ALL=C timeout -s KILL 45 nice -n 19 bash --noprofile --norc -s"
    assert build_command(45, ionice=True) == (
        "env -i PATH=/usr/bin:/bin LC_ALL=C timeout -s KILL 45 nice -n 19 ionice -c 3 bash --noprofile --norc -s"
    )
    assert is_fixed_command(build_command(remote.MAX_REMOTE_SECONDS, ionice=True))


@pytest.mark.parametrize("bad", [0, -1, remote.MAX_REMOTE_SECONDS + 1, True, 1.0, "45", None])
def test_build_command_rejects_non_integer_seconds(bad) -> None:
    with pytest.raises(ValueError):
        build_command(bad)


def test_build_command_rejects_non_bool_ionice() -> None:
    with pytest.raises(ValueError):
        build_command(45, ionice=1)


def test_remote_seconds_rounds_up_and_adds_margin() -> None:
    assert remote_seconds(40) == 45
    assert remote_seconds(39.01) == 45
    assert remote_seconds(0.2) == 6
    assert remote_seconds(0) == 5
    assert remote_seconds(-3.5) == 5
    assert remote_seconds(10**9) == remote.MAX_REMOTE_SECONDS
    for bad in (float("nan"), float("inf"), True, "40", None):
        with pytest.raises(ValueError):
            remote_seconds(bad)


# ---------------------------------------------------------------------------
# ① 脚本
# ---------------------------------------------------------------------------


def test_listing_script_matches_design() -> None:
    script = build_listing_script("/usr/local/ds/SFS2X/logs", "/usr/local/ds/SFS2X/ziplogs")
    assert script == (
        "{ printf '\\0'\n"
        "  for t in find grep gzip; do command -v \"$t\" >/dev/null || printf 'MISSING %s\\0' \"$t\"; done\n"
        "  ionice -c 3 true && printf 'IONICE\\0'\n"
        "  find -H /usr/local/ds/SFS2X/logs -mindepth 1 -maxdepth 1 -type f -printf 'L %i %s %f\\0'\n"
        "  printf 'EXIT L %d\\0' \"$?\"\n"
        "  if [ -e /usr/local/ds/SFS2X/ziplogs ] || [ -L /usr/local/ds/SFS2X/ziplogs ]; then\n"
        "    find -H /usr/local/ds/SFS2X/ziplogs -mindepth 1 -maxdepth 1 -type f -printf 'Z %s %f\\0'\n"
        "    printf 'EXIT Z %d\\0' \"$?\"\n"
        "  else printf 'EXIT Z 0\\0'; fi\n"
        "} </dev/null\n"
    )
    assert "<<" not in script and "eval" not in script


def test_listing_script_without_zip_dir() -> None:
    script = build_listing_script("/usr/local/ds/SFS2X/logs", None)
    assert script.endswith("  printf 'EXIT L %d\\0' \"$?\"\n  printf 'EXIT Z 0\\0'\n} </dev/null\n")
    assert script.count("find -H ") == 1


def test_listing_without_archives_parses_as_empty() -> None:
    # 未配置或游戏服上没有压缩目录时 ① 只输出 EXIT Z 0：结果中没有压缩文件，也不是 LOG_DIR_UNAVAILABLE
    p = ListingParser()
    p.feed(b"\0L 7 10 smartfox.log\0EXIT L 0\0EXIT Z 0\0")
    listing = p.finish(RemoteExit(0, None, 0))
    assert listing.archives == () and [e.name for e in listing.plain] == ["smartfox.log"]


@pytest.mark.parametrize("bad", [
    "relative/logs", "", "/logs; rm -rf /", "/lo gs", "/logs'", "/logs\n", "/logs$(id)", "/日志", "/a:b", None,
])
def test_listing_script_rejects_bad_dirs(bad) -> None:
    with pytest.raises(ValueError):
        build_listing_script(bad, "/usr/local/ds/SFS2X/ziplogs")
    if bad is not None:  # zip_dir 为 None 表示未配置压缩目录
        with pytest.raises(ValueError):
            build_listing_script("/usr/local/ds/SFS2X/logs", bad)


# ---------------------------------------------------------------------------
# NUL 记录解析
# ---------------------------------------------------------------------------


def test_parse_normal_listing_discards_bashrc_noise() -> None:
    listing = parse(NORMAL)
    assert listing == Listing(
        plain=(PlainEntry("smartfox.log", 1234567, 52311), PlainEntry("smartfox.log.2026-09-17-16", 1234568, 100),
               PlainEntry("gc.log", 99, 7)),
        archives=(ArchiveEntry("smartfox.log.2026-09-17-15.tar.gz", 4096),),
        ionice=True,
        ignored=0,
    )


@pytest.mark.parametrize("chunk", [1, 2, 3, 7, 64])
def test_parse_is_independent_of_chunking(chunk) -> None:
    assert parse(NORMAL, chunk=chunk) == parse(NORMAL)


def test_parse_without_noise_and_without_ionice() -> None:
    listing = parse(b"EXIT L 0\0EXIT Z 0\0", noise=b"")
    assert listing == Listing((), (), False, 0)


def test_unsafe_names_are_ignored_and_spaces_kept_in_name_field() -> None:
    payload = (
        b"L 1 10 smartfox.log.2026-09-17-16-03 \0"     # 末尾空格
        b"L 2 10 smartfox.log.2026-09-17-16-03\n\0"    # 末尾换行
        b"L 3 10 $(touch pwned)\0"
        b"L 4 10 \xe6\x97\xa5\xe5\xbf\x97.log\0"       # 中文
        b"L 5 10 a b c\0"
        b"L 6 10 -rf\0"                                # 字符安全，交给 select.py 忽略
        b"L 7 10 smartfox.log\0"
        b"EXIT L 0\0"
        b"Z 5 x'y\0"
        b"Z 6 smartfox.log.2026-09-17-16-00.tar.gz\0"
        b"EXIT Z 0\0"
    )
    listing = parse(payload)
    assert listing.plain == (PlainEntry("-rf", 6, 10), PlainEntry("smartfox.log", 7, 10))
    assert listing.archives == (ArchiveEntry("smartfox.log.2026-09-17-16-00.tar.gz", 6),)
    assert listing.ignored == 6


def test_is_safe_file_name() -> None:
    for good in ("smartfox.log", "smartfox.log.2026-09-17-16-03", "a_b-c.d", b"x"):
        assert is_safe_file_name(good)
    for bad in ("", ".", "..", "a b", "a/b", "a\nb", "日志", "ａ", "x" * 256, None, 1, b"a\0b"):
        assert not is_safe_file_name(bad), bad


def test_large_inode_and_size() -> None:
    listing = parse(b"L 18446744073709551615 99999999999 smartfox.log\0EXIT L 0\0EXIT Z 0\0")
    assert listing.plain == (PlainEntry("smartfox.log", 18446744073709551615, 99999999999),)


# ---- 错误映射 ----


def test_exit_127_without_script_output_is_remote_tool_unavailable() -> None:
    parser = ListingParser()
    parser.feed(b"Welcome\nenv: 'timeout': No such file or directory\n")  # 只有登录 Shell 的输出，没有 NUL
    with pytest.raises(LogViewerError) as ei:
        parser.finish(RemoteExit(127, None, 40))
    assert ei.value.code is ErrorCode.REMOTE_TOOL_UNAVAILABLE


@pytest.mark.parametrize("exit", [RemoteExit(0, None, 0), RemoteExit(1, None, 0), RemoteExit(None, "KILL", 0),
                                  RemoteExit(None, None, 0)])
def test_no_script_output_otherwise_is_ssh_failed(exit) -> None:
    parser = ListingParser()
    parser.feed(BASHRC_NOISE)
    with pytest.raises(LogViewerError) as ei:
        parser.finish(exit)
    assert ei.value.code is ErrorCode.SSH_FAILED


@pytest.mark.parametrize("missing", [b"find", b"grep", b"gzip"])
def test_missing_tool_is_remote_tool_unavailable(missing) -> None:
    payload = b"MISSING " + missing + b"\0IONICE\0EXIT L 127\0EXIT Z 127\0"
    assert error_of(payload) is ErrorCode.REMOTE_TOOL_UNAVAILABLE


def test_missing_tool_wins_over_incomplete_output() -> None:
    assert error_of(b"MISSING gzip\0IONICE\0L 1 2 smartfox.log\0", RemoteExit(None, None, 0)) \
        is ErrorCode.REMOTE_TOOL_UNAVAILABLE


@pytest.mark.parametrize("payload", [
    b"EXIT L 1\0Z 4096 smartfox.log.2026-09-17-15.tar.gz\0EXIT Z 0\0",   # 非压缩目录不可列举
    b"L 1 2 smartfox.log\0EXIT L 0\0EXIT Z 1\0",                         # ziplogs 不可列举
    b"EXIT L 1\0EXIT Z 1\0",
])
def test_unlistable_dir_is_log_dir_unavailable(payload) -> None:
    assert error_of(payload) is ErrorCode.LOG_DIR_UNAVAILABLE


def test_find_exit_1_with_records_is_ok() -> None:
    # 文件在列举中被删除时 find 返回 1，但该目录有记录：正常
    listing = parse(b"L 1 2 smartfox.log\0EXIT L 1\0Z 3 a.tar.gz\0EXIT Z 1\0")
    assert listing.plain == (PlainEntry("smartfox.log", 1, 2),)
    assert listing.archives == (ArchiveEntry("a.tar.gz", 3),)


def test_ignored_records_count_as_records_for_dir_check() -> None:
    listing = parse(b"L 1 2 \xe6\x97\xa5.log\0EXIT L 1\0EXIT Z 0\0")
    assert listing.plain == () and listing.ignored == 1


@pytest.mark.parametrize("payload", [
    b"",                                                    # 只有起始 NUL
    b"IONICE\0L 1 2 smartfox.log\0",                        # 缺 EXIT L
    b"IONICE\0EXIT L 0\0Z 1 a.tar.gz\0",                    # 缺 EXIT Z
    b"EXIT L 0\0EXIT Z 0\0Z 1 a.tar",                       # 末尾有不完整的记录
    b"EXIT L 0\0EXIT Z 0",
])
def test_incomplete_output_is_ssh_failed(payload) -> None:
    assert error_of(payload) is ErrorCode.SSH_FAILED


@pytest.mark.parametrize("payload", [
    b"BOGUS\0EXIT L 0\0EXIT Z 0\0",
    b"L 1 smartfox.log\0EXIT L 0\0EXIT Z 0\0",               # 缺字段
    b"L x 2 smartfox.log\0EXIT L 0\0EXIT Z 0\0",             # inode 不是数字
    b"L 123456789012345678901 2 smartfox.log\0EXIT L 0\0EXIT Z 0\0",  # inode 超过 20 位
    b"L \xd9\xa1 2 smartfox.log\0EXIT L 0\0EXIT Z 0\0",       # 非 ASCII 数字
    b"L 1 -2 smartfox.log\0EXIT L 0\0EXIT Z 0\0",
    b"L 1 2\0EXIT L 0\0EXIT Z 0\0",                          # 缺文件名字段
    b"EXIT L 0\0L 1 2 smartfox.log\0EXIT Z 0\0",             # L 出现在 EXIT L 之后
    b"Z 1 a.tar.gz\0EXIT L 0\0EXIT Z 0\0",                   # Z 出现在 EXIT L 之前
    b"EXIT Z 0\0EXIT L 0\0",                                 # 顺序颠倒
    b"EXIT L 0\0EXIT Z 0\0EXIT Z 0\0",                       # DONE 之后还有记录
    b"L 1 2 smartfox.log\0IONICE\0EXIT L 0\0EXIT Z 0\0",     # IONICE 出现在列举之后
    b"IONICE\0IONICE\0EXIT L 0\0EXIT Z 0\0",
    b"IONICE\0MISSING find\0EXIT L 0\0EXIT Z 0\0",           # MISSING 出现在 IONICE 之后
    b"MISSING rm\0EXIT L 0\0EXIT Z 0\0",
    b"EXIT L 0 \0EXIT Z 0\0",
    b"EXIT L x\0EXIT Z 0\0",
    b"EXIT Q 0\0EXIT Z 0\0",
    b"L 1 2 " + b"a" * remote.MAX_RECORD_BYTES + b"\0EXIT L 0\0EXIT Z 0\0",
])
def test_malformed_records_are_ssh_failed(payload) -> None:
    assert error_of(payload) is ErrorCode.SSH_FAILED


def test_overlong_partial_record_fails_early() -> None:
    parser = ListingParser()
    parser.feed(b"\0L 1 2 ")
    with pytest.raises(LogViewerError) as ei:
        parser.feed(b"a" * (remote.MAX_RECORD_BYTES + 1))
    assert ei.value.code is ErrorCode.SSH_FAILED


def test_fake_records_in_noise_do_not_count() -> None:
    # 登录 Shell 的输出中形似记录的文本（MISSING grep、L …）都在第一个 NUL 之前，被丢弃
    listing = parse(b"EXIT L 0\0EXIT Z 0\0")
    assert listing == Listing((), (), False, 0)
