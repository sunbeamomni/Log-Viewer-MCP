"""server/validate.py 参数校验与 Time_Range 的单元测试（任务 2.1–2.4；需求 1.1–1.3、1.6、8.1、8.2）。"""

import os
import shutil
import time
from datetime import timedelta, timezone
from types import SimpleNamespace

import pytest

from log_viewer_mcp.common.errors import ErrorCode, LogViewerError
from log_viewer_mcp.server import validate
from log_viewer_mcp.server.config import Limits
from log_viewer_mcp.server.validate import (
    GREP_SEARCH_PATH,
    SearchParams,
    TimeRange,
    compute_time_range,
    format_server_time,
    grep_ere_check_works,
    grep_ere_is_valid,
    grep_ere_is_valid_async,
    validate_arguments_async,
    validate_list_servers,
    validate_search_logs,
    validate_search_logs_async,
)

LIMITS = Limits()
LOG_TYPES = ("normal", "login", "analysis", "pulsar")


def search(**kwargs):
    return validate_search_logs(kwargs, limits=LIMITS, log_types=LOG_TYPES)


def assert_invalid(arguments):
    with pytest.raises(LogViewerError) as exc:
        validate_search_logs(arguments, limits=LIMITS, log_types=LOG_TYPES)
    assert exc.value.code is ErrorCode.INVALID_ARGUMENT
    return exc.value


BASE = {"svr_ids": [1001], "log_type": "normal"}


def test_minimal_arguments_use_defaults():
    assert search(**BASE) == SearchParams(svr_ids=(1001,), log_type="normal")


def test_full_arguments_are_returned():
    p = search(
        svr_ids=[1001, 1002],
        log_type="login",
        start_time=1789668000,
        end_time=1789671600,
        include_patterns=["CMD", "uid=123"],
        exclude_patterns=["heartbeat"],
        regex=False,
        ignore_case=True,
        context_before=0,
        context_after=100,
    )
    assert p.svr_ids == (1001, 1002)
    assert p.start_time == 1789668000 and p.end_time == 1789671600 and p.at_time is None
    assert p.include_patterns == ("CMD", "uid=123")
    assert p.exclude_patterns == ("heartbeat",)
    assert p.ignore_case is True and p.context_after == 100


def test_null_optional_values_are_treated_as_absent():
    args = dict(BASE, at_time=None, include_patterns=None, regex=None, context_after=None)
    assert validate_search_logs(args, limits=LIMITS, log_types=LOG_TYPES) == search(**BASE)


def test_svr_ids_deduplicated_preserving_order():
    assert search(svr_ids=[3, 1, 3, 2, 1], log_type="normal").svr_ids == (3, 1, 2)


def test_svr_ids_count_is_checked_before_dedup():
    assert_invalid(dict(BASE, svr_ids=[1] * (LIMITS.max_servers + 1)))
    assert search(svr_ids=[1] * LIMITS.max_servers, log_type="normal").svr_ids == (1,)


@pytest.mark.parametrize(
    "svr_ids",
    [
        [],
        1001,
        "1001",
        ["1001"],
        ["1; rm -rf /"],
        ["1 OR 1=1"],
        [True],
        [1.0],
        [0],
        [-1],
        [4294967296],
        [None],
    ],
)
def test_invalid_svr_ids_rejected(svr_ids):
    assert_invalid(dict(BASE, svr_ids=svr_ids))


def test_svr_id_bounds_accepted():
    assert search(svr_ids=[1, 4294967295], log_type="normal").svr_ids == (1, 4294967295)


@pytest.mark.parametrize("missing", ["svr_ids", "log_type"])
def test_required_parameters(missing):
    args = dict(BASE)
    args[missing] = None
    assert_invalid(args)
    del args[missing]
    assert_invalid(args)


@pytest.mark.parametrize("extra", ["path", "cmd", "file", "sql", "SVR_IDS", "log_dir"])
def test_undefined_parameter_names_rejected(extra):
    assert_invalid(dict(BASE, **{extra: "x"}))


@pytest.mark.parametrize("arguments", [[], "svr_ids", 1, [("svr_ids", [1])]])
def test_arguments_must_be_an_object(arguments):
    assert_invalid(arguments)


@pytest.mark.parametrize("log_type", ["unknown", "Normal", "normal\n", "../logs", 1, True, ["normal"]])
def test_invalid_log_type_lists_available_values(log_type):
    err = assert_invalid(dict(BASE, log_type=log_type))
    for name in LOG_TYPES:
        assert name in err.message


@pytest.mark.parametrize("name", ["include_patterns", "exclude_patterns"])
@pytest.mark.parametrize(
    "value",
    [
        [],
        "CMD",
        [1],
        [None],
        [""],
        ["x" * 257],
        ["a\nb"],
        ["a\rb"],
        ["a\x00b"],
        ["a\tb"],
        ["a\x7fb"],
        ["a\x85b"],
        ["\ud800"],
        ["p"] * 11,
    ],
)
def test_invalid_patterns_rejected(name, value):
    assert_invalid(dict(BASE, **{name: value}))


def test_pattern_limits_and_special_characters_accepted():
    patterns = ["x" * 256, "中" * 256, "-e", "--", "; rm -rf / | `id` $(id) 'q' \"d\" \\ * ? [a] {b} ~ ! # <>"]
    p = search(**BASE, include_patterns=patterns, exclude_patterns=["p"] * 10)
    assert p.include_patterns == tuple(patterns)
    assert len(p.exclude_patterns) == 10


@pytest.mark.parametrize("name", ["regex", "ignore_case"])
@pytest.mark.parametrize("value", [0, 1, "true", "false", [True]])
def test_flags_must_be_booleans(name, value):
    assert_invalid(dict(BASE, **{name: value}))


@pytest.mark.parametrize("name", ["context_before", "context_after"])
@pytest.mark.parametrize("value", [-1, 101, 1.0, "2", True])
def test_invalid_context_rejected(name, value):
    assert_invalid(dict(BASE, **{name: value}))


@pytest.mark.parametrize("name", ["at_time", "start_time", "end_time"])
@pytest.mark.parametrize("value", ["1789668000", 1789668000.0, True, [1789668000]])
def test_time_params_must_be_integers(name, value):
    assert_invalid(dict(BASE, **{name: value}))


# --- 时间参数与 Time_Range（任务 2.2；需求 1.2、1.3、1.6） ---

NOW = 1789671600  # 2026-09-17 17:00:00 GMT-2
GMT_MINUS_2 = timezone(timedelta(hours=-2))


def time_range(now=NOW, **kwargs):
    return compute_time_range(search(**BASE, **kwargs), now, LIMITS)


@pytest.mark.parametrize("value", [-1, 4102444800, 1789668000000])
def test_time_params_out_of_range_suggest_seconds(value):
    err = assert_invalid(dict(BASE, start_time=value))
    assert "秒级 Unix 时间戳" in err.message


def test_time_param_bounds_accepted():
    assert search(**BASE, start_time=0, end_time=3600).start_time == 0
    assert search(**BASE, at_time=4102444799).at_time == 4102444799


def test_time_range_branches():
    assert time_range(at_time=NOW) == TimeRange(NOW - 300, NOW + 300)
    assert time_range(start_time=NOW - 600) == TimeRange(NOW - 600, NOW)
    assert time_range(start_time=NOW - 7200, end_time=NOW - 3600) == TimeRange(NOW - 7200, NOW - 3600)
    assert time_range() == TimeRange(NOW - 3600, NOW)


def test_time_range_full_span_accepted_and_clamped_at_zero():
    assert time_range(start_time=NOW - 86400) == TimeRange(NOW - 86400, NOW)
    assert time_range(at_time=60) == TimeRange(0, 360)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"at_time": NOW, "start_time": NOW - 60},
        {"at_time": NOW, "end_time": NOW},
        {"end_time": NOW},
        {"start_time": NOW, "end_time": NOW},
        {"start_time": NOW, "end_time": NOW - 1},
        {"start_time": NOW - 86401, "end_time": NOW},
    ],
)
def test_invalid_time_combinations_rejected_by_validation(kwargs):
    assert_invalid(dict(BASE, **kwargs))


@pytest.mark.parametrize("start_time", [NOW, NOW + 1, NOW - 86401])
def test_start_only_checked_against_now(start_time):
    params = search(**BASE, start_time=start_time)
    with pytest.raises(LogViewerError) as exc:
        compute_time_range(params, NOW, LIMITS)
    assert exc.value.code is ErrorCode.INVALID_ARGUMENT


def test_format_server_time_and_summary():
    assert format_server_time(1791328385, GMT_MINUS_2) == "2026-10-06 21:13:05"
    assert format_server_time(0, GMT_MINUS_2) == "1969-12-31 22:00:00"
    assert TimeRange(1789668000, NOW).as_summary(GMT_MINUS_2) == {
        "start": 1789668000,
        "end": NOW,
        "start_server": "2026-09-17 16:00:00",
        "end_server": "2026-09-17 17:00:00",
    }


def test_list_servers_accepts_no_arguments():
    validate_list_servers(None)
    validate_list_servers({})


@pytest.mark.parametrize("arguments", [{"svr_ids": [1]}, {"x": None}, [], "x"])
def test_list_servers_rejects_any_argument(arguments):
    with pytest.raises(LogViewerError) as exc:
        validate_list_servers(arguments)
    assert exc.value.code is ErrorCode.INVALID_ARGUMENT


# --- regex 模式检查（任务 2.3；需求 8.1） ---


class StubChecker:
    """记录调用的检查器替身：invalid 中的模式判为非法。"""

    def __init__(self, invalid=()):
        self.invalid = set(invalid)
        self.calls = []

    def __call__(self, pattern):
        self.calls.append(pattern)
        return pattern not in self.invalid


class AsyncStubChecker(StubChecker):
    async def __call__(self, pattern):
        return StubChecker.__call__(self, pattern)


def regex_search(checker, **kwargs):
    return validate_search_logs(dict(BASE, **kwargs), limits=LIMITS, log_types=LOG_TYPES, regex_checker=checker)


async def regex_search_async(checker, **kwargs):
    return await validate_search_logs_async(
        dict(BASE, **kwargs), limits=LIMITS, log_types=LOG_TYPES, regex_checker=checker
    )


def invalid_message(call):
    with pytest.raises(LogViewerError) as exc:
        call()
    assert exc.value.code is ErrorCode.INVALID_ARGUMENT
    return exc.value.message


BACKREFERENCES = ["\\1", "(a)\\1", "a\\9b", "\\\\1", "[\\1]"]


def test_regex_false_does_not_check_patterns():
    checker = StubChecker(invalid={"("})
    p = regex_search(checker, include_patterns=["(", "\\1"], regex=False)
    assert p.include_patterns == ("(", "\\1") and checker.calls == []


def test_regex_patterns_checked_once_each_in_order():
    checker = StubChecker()
    p = regex_search(checker, include_patterns=["a|b", "x"], exclude_patterns=["x", "(y)+"], regex=True)
    assert p.regex is True
    assert checker.calls == ["a|b", "x", "(y)+"]


@pytest.mark.parametrize("name", ["include_patterns", "exclude_patterns"])
@pytest.mark.parametrize("pattern", BACKREFERENCES)
def test_backreferences_rejected_conservatively_without_running_checker(name, pattern):
    checker = StubChecker()
    message = invalid_message(lambda: regex_search(checker, regex=True, **{name: ["ok", pattern]}))
    assert "反向引用" in message and pattern not in message
    assert checker.calls == []


@pytest.mark.parametrize("pattern", ["\\0", "\\.", "\\[", "\\\\", "\\bword\\b", "a{1,9}"])
def test_other_escapes_are_left_to_grep(pattern):
    checker = StubChecker()
    regex_search(checker, include_patterns=[pattern], regex=True)
    assert checker.calls == [pattern]


def test_invalid_ere_rejected_without_echoing_pattern():
    checker = StubChecker(invalid={"secret("})
    message = invalid_message(lambda: regex_search(checker, exclude_patterns=["ok", "secret("], regex=True))
    assert "扩展正则" in message and "secret" not in message


@pytest.mark.asyncio
async def test_async_variant_checks_each_pattern_once():
    checker = AsyncStubChecker()
    p = await regex_search_async(checker, include_patterns=["a|b", "x"], exclude_patterns=["x"], regex=True)
    assert p.include_patterns == ("a|b", "x")
    assert sorted(checker.calls) == ["a|b", "x"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        ({"include_patterns": ["ok", "secret("]}, "扩展正则"),
        ({"exclude_patterns": ["(a)\\1"]}, "反向引用"),
    ],
)
async def test_async_variant_rejects(kwargs, fragment):
    checker = AsyncStubChecker(invalid={"secret("})
    with pytest.raises(LogViewerError) as exc:
        await regex_search_async(checker, regex=True, **kwargs)
    assert exc.value.code is ErrorCode.INVALID_ARGUMENT and fragment in exc.value.message


@pytest.mark.asyncio
async def test_async_variant_propagates_checker_error():
    async def unavailable(pattern):
        raise LogViewerError(ErrorCode.INVALID_ARGUMENT, "跳板机暂时无法检查扩展正则")

    with pytest.raises(LogViewerError) as exc:
        await regex_search_async(unavailable, include_patterns=["a", "b"], regex=True)
    assert exc.value.code is ErrorCode.INVALID_ARGUMENT


@pytest.mark.asyncio
async def test_validate_arguments_async_dispatch():
    config = SimpleNamespace(limits=LIMITS, log_types={"normal": None})
    checker = AsyncStubChecker()
    args = dict(BASE, include_patterns=["a+"], regex=True)
    p = await validate_arguments_async("search_logs", args, config, regex_checker=checker)
    assert p.include_patterns == ("a+",) and checker.calls == ["a+"]
    assert await validate_arguments_async("list_servers", {}, config) is None
    with pytest.raises(LogViewerError):
        await validate_arguments_async("other", {}, config)


@pytest.mark.asyncio
async def test_missing_grep_fails_closed(monkeypatch):
    monkeypatch.setattr(validate, "GREP_SEARCH_PATH", os.path.join(os.path.dirname(__file__), "no-such-dir"))
    assert "regex=false" in invalid_message(lambda: grep_ere_is_valid("a"))
    with pytest.raises(LogViewerError) as exc:
        await grep_ere_is_valid_async("a")
    assert exc.value.code is ErrorCode.INVALID_ARGUMENT
    # 默认检查器：regex=true 且有模式时拒绝；没有模式时不需要 grep
    invalid_message(lambda: validate_search_logs(dict(BASE, include_patterns=["a"], regex=True),
                                                 limits=LIMITS, log_types=LOG_TYPES))
    assert validate_search_logs(dict(BASE, regex=True), limits=LIMITS, log_types=LOG_TYPES).regex is True
    assert grep_ere_check_works() is False


# 以下用本机 GNU grep 或替身 grep 脚本，只在 POSIX 上运行（compose 的测试运行容器自带 GNU grep）。

posix_only = pytest.mark.skipif(os.name != "posix", reason="grep 检查只在 POSIX 系统上运行")
real_grep = pytest.mark.skipif(
    os.name != "posix" or shutil.which("grep", path=GREP_SEARCH_PATH) is None,
    reason="本机 /usr/bin、/bin 中没有 grep",
)

VALID_ERE = ["a|b", "^\\[ERROR\\] .*uid=[0-9]+$", "x{2,3}", "-e", "--", "中文.*结束", "(a|b)+c?", "\\.", "[[:digit:]]+",
             "\\bword\\b", "'; rm -rf / #", "$(id)`id`"]
# 注：GNU grep -E 把不配对的 ")" 当作字面字符（"a)" 合法），远端 grep 同样如此，故不列入。
INVALID_ERE = ["(", "(a|b", "[a", "a{2,1}", "\\", "[[:nope:]]"]


@real_grep
@pytest.mark.parametrize("pattern", VALID_ERE)
def test_real_grep_accepts_valid_ere(pattern):
    assert grep_ere_is_valid(pattern) is True


@real_grep
@pytest.mark.parametrize("pattern", INVALID_ERE)
def test_real_grep_rejects_invalid_ere(pattern):
    assert grep_ere_is_valid(pattern) is False


@real_grep
@pytest.mark.asyncio
async def test_real_grep_async_matches_sync():
    for pattern in VALID_ERE + INVALID_ERE:
        assert await grep_ere_is_valid_async(pattern) is grep_ere_is_valid(pattern), pattern


@real_grep
@pytest.mark.asyncio
async def test_real_grep_end_to_end():
    assert grep_ere_check_works() is True
    args = dict(BASE, include_patterns=["CMD|uid=[0-9]+"], exclude_patterns=["heart(beat"], regex=True)
    invalid_message(lambda: validate_search_logs(args, limits=LIMITS, log_types=LOG_TYPES))
    with pytest.raises(LogViewerError):
        await validate_search_logs_async(args, limits=LIMITS, log_types=LOG_TYPES)
    args["exclude_patterns"] = ["heart(beat)?"]
    p = await validate_search_logs_async(args, limits=LIMITS, log_types=LOG_TYPES)
    assert p.exclude_patterns == ("heart(beat)?",)


@pytest.fixture
def fake_grep(tmp_path, monkeypatch):
    """在临时目录放一个替身 grep 脚本，并让 validate 只在该目录中找 grep。"""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    monkeypatch.setattr(validate, "GREP_SEARCH_PATH", str(bindir))

    def install(body):
        script = bindir / "grep"
        script.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
        script.chmod(0o755)
        return tmp_path

    return install


@posix_only
@pytest.mark.asyncio
async def test_grep_argv_and_environment(fake_grep, monkeypatch):
    monkeypatch.setenv("GREP_OPTIONS", "--include=x")
    out = fake_grep('for a in "$@"; do printf "%s\\n" "$a"; done >"$0.argv"; /usr/bin/env >"$0.env"; exit 1')
    pattern = "-x'; rm -rf / \"$(id)\" 中文"
    for check in (grep_ere_is_valid, grep_ere_is_valid_async):
        result = check(pattern)
        if not isinstance(result, bool):
            result = await result
        assert result is True
        argv = (out / "bin" / "grep.argv").read_text(encoding="utf-8").splitlines()
        assert argv == ["-E", "-e", pattern, "--", "/dev/null"]
        env = (out / "bin" / "grep.env").read_text(encoding="utf-8").splitlines()
        assert "LC_ALL=C" in env and f"PATH={out / 'bin'}" in env
        assert not any(line.startswith(("GREP_OPTIONS=", "HOME=", "LANG=")) for line in env)


@posix_only
@pytest.mark.asyncio
@pytest.mark.parametrize(("body", "valid"), [("exit 0", True), ("exit 1", True), ("exit 2", False),
                                             ("exit 3", False), ("kill -9 $$", False)])
async def test_grep_exit_status(fake_grep, body, valid):
    fake_grep(body)
    assert grep_ere_is_valid("a") is valid
    assert await grep_ere_is_valid_async("a") is valid


@posix_only
@pytest.mark.asyncio
async def test_grep_timeout_is_invalid_and_process_killed(fake_grep, monkeypatch):
    monkeypatch.setattr(validate, "GREP_CHECK_TIMEOUT_S", 0.3)
    out = fake_grep('printf "%s" "$$" >"$0.pid"; exec /bin/sleep 30')
    pid_file = out / "bin" / "grep.pid"
    for check in (grep_ere_is_valid, grep_ere_is_valid_async):
        started = time.monotonic()
        result = check("a")
        if not isinstance(result, bool):
            result = await result
        assert result is False
        assert time.monotonic() - started < 5
        with pytest.raises(ProcessLookupError):  # 子进程已被杀掉并回收
            os.kill(int(pid_file.read_text()), 0)


# --- 边界值与 Time_Range 补充（任务 2.4；需求 1.2、1.3、8.1） ---

# 看起来像数字的字符串：末尾换行、前导空格、全角数字、阿拉伯-印度数字、指数与十六进制写法。
NUMERIC_LOOKING_STRINGS = ["1789668000\n", " 1789668000", "１７８９６６８０００", "١٧٨٩٦٦٨٠٠٠", "1.789668e9", "0x6aac5ca0"]


@pytest.mark.parametrize("svr_id", ["1001\n", " 1001", "１００１", "١٠٠١", "1e3", "0x3e9"])
def test_svr_ids_numeric_looking_strings_rejected(svr_id):
    assert_invalid(dict(BASE, svr_ids=[svr_id]))
    assert_invalid(dict(BASE, svr_ids=[1001, svr_id]))


@pytest.mark.parametrize("log_type", ["normal ", " normal", "normal\r\n", "ｎｏｒｍａｌ", "NORMAL", ""])
def test_log_type_must_match_exactly(log_type):
    assert_invalid(dict(BASE, log_type=log_type))


@pytest.mark.parametrize("name", ["at_time", "start_time", "end_time"])
@pytest.mark.parametrize("value", NUMERIC_LOOKING_STRINGS + [1789668000.5, 1.789668e9, 0.0])
def test_time_params_strings_and_decimals_rejected_without_echo(name, value):
    args = dict(BASE, **{name: value})
    if name == "end_time":
        args["start_time"] = 1789668000 - 3600
    err = assert_invalid(args)
    assert name in err.message and "秒级 Unix 时间戳" in err.message
    assert "1789668000" not in err.message


@pytest.mark.parametrize("name", ["at_time", "start_time", "end_time"])
@pytest.mark.parametrize("value", [-1, 4102444800, 1789668000000, 1789668000000000, -1789668000])
def test_time_params_out_of_range_rejected(name, value):
    args = dict(BASE, **{name: value})
    if name == "end_time":
        args["start_time"] = 0
    err = assert_invalid(args)
    assert name in err.message and "秒级 Unix 时间戳" in err.message
    assert str(value) not in err.message


def test_time_param_bounds_accepted_for_every_param():
    assert search(**BASE, at_time=0).at_time == 0
    assert search(**BASE, at_time=4102444799).at_time == 4102444799
    p = search(**BASE, start_time=4102444799 - 3600, end_time=4102444799)
    assert (p.start_time, p.end_time) == (4102444799 - 3600, 4102444799)
    p = search(**BASE, start_time=0, end_time=1)
    assert (p.start_time, p.end_time) == (0, 1)


def test_time_range_exact_max_span_accepted_for_start_and_end():
    assert time_range(start_time=NOW - 86400, end_time=NOW) == TimeRange(NOW - 86400, NOW)
    assert_invalid(dict(BASE, start_time=NOW - 86400, end_time=NOW + 1))


def test_time_range_start_only_minimal_span():
    assert time_range(start_time=NOW - 1) == TimeRange(NOW - 1, NOW)


def test_time_range_clamped_at_zero_for_at_time_and_lookback():
    assert time_range(at_time=0) == TimeRange(0, 300)
    assert time_range(at_time=299) == TimeRange(0, 599)
    assert time_range(at_time=300) == TimeRange(0, 600)
    assert time_range(now=100) == TimeRange(0, 100)
    assert time_range(now=3600) == TimeRange(0, 3600)


def test_time_range_future_times_are_not_errors():
    assert time_range(at_time=NOW + 86400) == TimeRange(NOW + 86100, NOW + 86700)
    assert time_range(start_time=NOW + 3600, end_time=NOW + 7200) == TimeRange(NOW + 3600, NOW + 7200)
    # at_time + Point_Window 超过时间参数上限时不裁剪
    assert time_range(at_time=4102444799) == TimeRange(4102444499, 4102445099)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"at_time": NOW, "start_time": NOW - 60, "end_time": NOW},
        {"at_time": NOW, "start_time": NOW - 60},
        {"at_time": NOW, "end_time": NOW},
        {"end_time": NOW},
        {"start_time": NOW, "end_time": NOW},
        {"start_time": NOW, "end_time": NOW - 1},
        {"start_time": NOW - 86401, "end_time": NOW},
    ],
)
def test_compute_time_range_checks_combinations_on_its_own(kwargs):
    """直接构造的 SearchParams（绕过 validate_search_logs）也按 1.2 拒绝。"""
    params = SearchParams(svr_ids=(1001,), log_type="normal", **kwargs)
    with pytest.raises(LogViewerError) as exc:
        compute_time_range(params, NOW, LIMITS)
    assert exc.value.code is ErrorCode.INVALID_ARGUMENT


@pytest.mark.parametrize("now", [float(NOW), True, str(NOW)])
def test_compute_time_range_requires_integer_now(now):
    with pytest.raises(TypeError):
        compute_time_range(search(**BASE), now, LIMITS)


def test_span_error_names_the_limit():
    err = assert_invalid(dict(BASE, start_time=NOW - 86401, end_time=NOW))
    assert "24 小时" in err.message


# 非默认的 Limits：跨度 2 小时、回溯 2 小时、时间点前后各 1 小时（满足 config 的约束）。
CUSTOM = Limits(max_time_span_h=2, default_lookback_h=2, point_window_min=60)


def custom_search(**kwargs):
    return validate_search_logs(dict(BASE, **kwargs), limits=CUSTOM, log_types=LOG_TYPES)


def custom_time_range(**kwargs):
    return compute_time_range(custom_search(**kwargs), NOW, CUSTOM)


def test_custom_limits_time_range_branches():
    assert custom_time_range() == TimeRange(NOW - 7200, NOW)
    assert custom_time_range(at_time=NOW) == TimeRange(NOW - 3600, NOW + 3600)
    assert custom_time_range(start_time=NOW - 7200) == TimeRange(NOW - 7200, NOW)
    assert custom_time_range(start_time=NOW - 7200, end_time=NOW) == TimeRange(NOW - 7200, NOW)


def test_custom_limits_span_enforced():
    with pytest.raises(LogViewerError) as exc:
        custom_search(start_time=NOW - 7201, end_time=NOW)
    assert exc.value.code is ErrorCode.INVALID_ARGUMENT and "2 小时" in exc.value.message
    with pytest.raises(LogViewerError):
        custom_time_range(start_time=NOW - 7201)
    # 同样的参数在默认 Limits 下合法：校验用的是传入的 Limits
    assert time_range(start_time=NOW - 7201, end_time=NOW) == TimeRange(NOW - 7201, NOW)


def test_custom_limits_other_bounds():
    small = Limits(max_servers=2, max_patterns=1, max_pattern_length=3, max_context_lines=5)

    def check(**kwargs):
        return validate_search_logs(dict(BASE, **kwargs), limits=small, log_types=LOG_TYPES)

    assert check(svr_ids=[1, 2], include_patterns=["abc"], context_before=5).context_before == 5
    for bad in ({"svr_ids": [1, 2, 3]}, {"include_patterns": ["a", "b"]}, {"exclude_patterns": ["abcd"]},
                {"context_after": 6}):
        with pytest.raises(LogViewerError) as exc:
            check(**bad)
        assert exc.value.code is ErrorCode.INVALID_ARGUMENT


def test_format_server_time_examples():
    assert format_server_time(1791328385, GMT_MINUS_2) == "2026-10-06 21:13:05"
    assert format_server_time(1789668000, GMT_MINUS_2) == "2026-09-17 16:00:00"
    assert format_server_time(1791328385, timezone.utc) == "2026-10-06 23:13:05"
    assert format_server_time(4102444799, GMT_MINUS_2) == "2099-12-31 21:59:59"
    # at_time + Point_Window 可超过时间参数上限，摘要仍能格式化
    assert format_server_time(4102445099, timezone.utc) == "2100-01-01 00:04:59"


@pytest.mark.parametrize("pattern", ["(", "[a", "a{2,1}", "*a"])
def test_invalid_regex_with_regex_false_is_literal(pattern):
    """非法正则只在 regex 为 true 时拒绝；regex 为 false 时按字面匹配，合法。"""
    checker = StubChecker(invalid={pattern})
    assert regex_search(checker, include_patterns=[pattern]).include_patterns == (pattern,)
    invalid_message(lambda: regex_search(checker, include_patterns=[pattern], regex=True))


@pytest.mark.asyncio
async def test_async_variant_applies_static_checks_before_grep():
    checker = AsyncStubChecker()
    for bad in ({"start_time": "1789668000\n"}, {"svr_ids": ["１００１"]}, {"end_time": NOW}):
        with pytest.raises(LogViewerError) as exc:
            await regex_search_async(checker, include_patterns=["a"], regex=True, **bad)
        assert exc.value.code is ErrorCode.INVALID_ARGUMENT
    assert checker.calls == []
