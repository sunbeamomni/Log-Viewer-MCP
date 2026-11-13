"""search_logs 与 list_servers 原始参数字典的校验（需求 1.1、8.1、8.2；design.md「参数校验（validate.py）」）。

FastMCP 的 pydantic 参数模型会丢掉未定义的参数并做类型转换，与 8.1 冲突，所以
call_tool 拿到原始参数字典后先交给本模块，通过后才继续执行；本模块不访问 Admin_DB
与游戏服。规则：

- 类型按 JSON 原值判断（``type(v) is int``，排除 true 与 1.0），不做任何类型转换；
- 可选参数为 null 时视为未提供；出现未定义的参数名即拒绝；
- svr_ids 的个数按原始列表计，再去重保序；
- 模式须能编码为 UTF-8（排除孤立代理项），长度按字符计，含 Unicode Cc 类字符即拒绝；
- 时间参数须是 0 至 4102444799 的整数（秒级 Unix 时间戳，1.3）；毫秒级的值超出范围；
- 任一项不符即抛出 ``LogViewerError(INVALID_ARGUMENT, 说明)``。说明由程序写定，
  不回显请求中的值。

Time_Range（1.2）由 ``compute_time_range(params, now, limits)`` 计算，是以当前时刻
（整数秒）为参数的纯函数。不依赖当前时刻的组合规则（at_time 与 start_time/end_time
同时提供、只提供 end_time、end_time 不晚于 start_time、start/end 跨度超过
Max_Time_Span）在 ``validate_search_logs`` 中就检查，依赖当前时刻的规则（只提供
start_time 时 start_time 不早于当前时刻或跨度超限）在 ``compute_time_range`` 中检查。
边界取法：

- at_time − Point_Window、当前时刻 − Default_Lookback 小于 0 时起点取 0（时间参数
  下限为 0，不产生负的 Unix 时间）；at_time + Point_Window 超过 4102444799 时不裁剪，
  终点只用于选文件；
- start_time 与 end_time 可以晚于当前时刻（结果为空，不算错误）。

``format_server_time`` 按 Server_Timezone 把 Unix 时间戳格式化为
``YYYY-MM-DD HH:mm:ss``，供摘要使用（1.6）。

regex 模式检查（8.1；design.md「参数校验（validate.py）」），只在 regex 为 true 时进行，
对 include_patterns 与 exclude_patterns 的全部模式（去重后）：

- 反向引用按保守规则拒绝：模式中任意位置出现「反斜杠后跟 1–9」即拒绝，不分析转义与方括号，
  所以 ``\\\\1``（转义的反斜杠加 1）与 ``[\\1]`` 也被拒绝；需要匹配这类文本时改用 regex=false；
- 扩展正则是否合法，在跳板机本机以 argv 列表运行 ``grep -E -e <模式> -- /dev/null`` 判断：
  不经 Shell，grep 只在 ``GREP_SEARCH_PATH``（/usr/bin:/bin）中查找，环境变量只有
  ``LC_ALL=C`` 与同一 PATH，标准输入输出都接 /dev/null，限时 2 秒；退出码 0 或 1 为合法，
  退出码 2、超时（进程被杀掉）或其他退出状态一律按非法处理（失败即拒绝）；
- 跳板机上找不到或无法启动 grep 时同样拒绝（INVALID_ARGUMENT，说明提示改用 regex=false），
  并写运行日志；启动时可调用 ``grep_ere_check_works()`` 自检（任务 8.6 决定是否据此拒绝启动）。

错误说明都是写定的文字，不回显模式。

同步与异步两套入口，校验规则相同，只是 grep 的运行方式不同：

- ``validate_arguments_async`` / ``validate_search_logs_async``：用
  ``asyncio.create_subprocess_exec`` 并发检查各模式，不阻塞事件循环。call_tool（任务 8.4）
  必须用这一套；
- ``validate_arguments`` / ``validate_search_logs``：用 ``subprocess.run`` 逐个检查，
  会阻塞调用线程，只用于同步场景与测试。

两套入口都接受 ``regex_checker`` 参数（模式 → 是否合法），测试可注入替身；默认分别为
``grep_ere_is_valid`` 与 ``grep_ere_is_valid_async``。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from log_viewer_mcp.common.errors import ErrorCode, LogViewerError
from log_viewer_mcp.common.formats import SVR_ID_MAX, SVR_ID_MIN, is_valid_svr_id

from .config import Config, Limits

__all__ = [
    "GREP_CHECK_TIMEOUT_S",
    "GREP_SEARCH_PATH",
    "LIST_SERVERS",
    "LIST_SERVERS_PARAMS",
    "SEARCH_LOGS",
    "SEARCH_LOGS_PARAMS",
    "TIMESTAMP_MAX",
    "TIMESTAMP_MIN",
    "AsyncRegexChecker",
    "RegexChecker",
    "SearchParams",
    "TimeRange",
    "compute_time_range",
    "format_server_time",
    "grep_ere_check_works",
    "grep_ere_is_valid",
    "grep_ere_is_valid_async",
    "validate_arguments",
    "validate_arguments_async",
    "validate_list_servers",
    "validate_search_logs",
    "validate_search_logs_async",
]

_log = logging.getLogger(__name__)

SEARCH_LOGS = "search_logs"
LIST_SERVERS = "list_servers"

# search_logs 的全部参数名（1.1）；不在其中的参数名一律拒绝（8.1）。
SEARCH_LOGS_PARAMS: tuple[str, ...] = (
    "svr_ids",
    "log_type",
    "at_time",
    "start_time",
    "end_time",
    "include_patterns",
    "exclude_patterns",
    "regex",
    "ignore_case",
    "context_before",
    "context_after",
)
LIST_SERVERS_PARAMS: tuple[str, ...] = ()

_TIME_PARAMS = ("at_time", "start_time", "end_time")

# 秒级 Unix 时间戳的取值范围（1.3）：1970-01-01T00:00:00Z 至 2099-12-31T23:59:59Z。
TIMESTAMP_MIN = 0
TIMESTAMP_MAX = 4102444799

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# Unicode Cc 类字符恰为 U+0000–U+001F 与 U+007F–U+009F（含 NUL、TAB、LF、CR）。
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


@dataclass(frozen=True)
class SearchParams:
    """通过校验的 search_logs 参数；未提供的可选参数取默认值（时间参数为 None）。"""

    svr_ids: tuple[int, ...]  # 已去重，保持原顺序
    log_type: str
    at_time: int | None = None
    start_time: int | None = None
    end_time: int | None = None
    include_patterns: tuple[str, ...] = ()
    exclude_patterns: tuple[str, ...] = ()
    regex: bool = False
    ignore_case: bool = False
    context_before: int = 0
    context_after: int = 0


def _invalid(message: str) -> LogViewerError:
    return LogViewerError(ErrorCode.INVALID_ARGUMENT, message)


def _arguments_dict(arguments: Any, allowed: tuple[str, ...]) -> dict[str, Any]:
    """检查参数字典与参数名，返回去掉 null 值后的字典（null 视为未提供）。"""
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, Mapping):
        raise _invalid("参数必须是 JSON 对象")
    for key in arguments:
        if type(key) is not str or key not in allowed:
            raise _invalid("存在未定义的参数，可用参数：" + "、".join(allowed))
    return {k: v for k, v in arguments.items() if v is not None}


def _check_svr_ids(value: Any, limits: Limits) -> tuple[int, ...]:
    hint = "svr_ids 必须是 1 至 %d 个整数" % limits.max_servers
    if type(value) is not list or not 1 <= len(value) <= limits.max_servers:
        raise _invalid(hint)
    for v in value:
        if not is_valid_svr_id(v):
            raise _invalid("%s，每个取值为 %d 至 %d 的整数" % (hint, SVR_ID_MIN, SVR_ID_MAX))
    return tuple(dict.fromkeys(value))


def _check_log_type(value: Any, log_types: Collection[str]) -> str:
    if type(value) is not str or value not in log_types:
        raise _invalid("log_type 不合法，可用取值：" + "、".join(log_types))
    return value


def _check_pattern(name: str, value: Any, limits: Limits) -> str:
    hint = "%s 中的每个模式必须是长度 1 至 %d 个字符的字符串，不能含控制字符（如换行、制表符）" % (
        name,
        limits.max_pattern_length,
    )
    if type(value) is not str or not 1 <= len(value) <= limits.max_pattern_length:
        raise _invalid(hint)
    if _CONTROL_CHARS.search(value):
        raise _invalid(hint)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise _invalid("%s 中的模式必须是合法的 UTF-8 文本" % name) from None
    return value


def _check_patterns(name: str, value: Any, limits: Limits) -> tuple[str, ...]:
    if type(value) is not list or not 1 <= len(value) <= limits.max_patterns:
        raise _invalid("%s 必须是 1 至 %d 个字符串" % (name, limits.max_patterns))
    return tuple(_check_pattern(name, v, limits) for v in value)


def _check_bool(name: str, value: Any) -> bool:
    if type(value) is not bool:
        raise _invalid("%s 必须是布尔值 true 或 false" % name)
    return value


def _check_context(name: str, value: Any, limits: Limits) -> int:
    if type(value) is not int or not 0 <= value <= limits.max_context_lines:
        raise _invalid("%s 必须是 0 至 %d 的整数" % (name, limits.max_context_lines))
    return value


def _check_time_param(name: str, value: Any) -> int:
    """时间参数须是 0 至 4102444799 的整数（1.3）；字符串、小数、布尔值与毫秒级的值都拒绝。"""
    hint = "%s 必须是秒级 Unix 时间戳，即 %d 至 %d 的整数" % (name, TIMESTAMP_MIN, TIMESTAMP_MAX)
    if type(value) is not int:
        raise _invalid(hint + "，不接受字符串或小数，请改用秒级 Unix 时间戳")
    if not TIMESTAMP_MIN <= value <= TIMESTAMP_MAX:
        raise _invalid(hint + "；毫秒级时间戳请除以 1000，改用秒级 Unix 时间戳")
    return value


@dataclass(frozen=True)
class TimeRange:
    """一次查询的时间范围 [start, end)，秒级 Unix 时间戳。"""

    start: int
    end: int

    def as_summary(self, tz: timezone) -> dict[str, int | str]:
        """摘要中的 time_range：Unix 时间戳与按 Server_Timezone 显示的服务器时间（1.6）。"""
        return {
            "start": self.start,
            "end": self.end,
            "start_server": format_server_time(self.start, tz),
            "end_server": format_server_time(self.end, tz),
        }


def format_server_time(ts: int, tz: timezone) -> str:
    """按 Server_Timezone 把秒级 Unix 时间戳格式化为 ``YYYY-MM-DD HH:mm:ss``（1.6）。

    从 UTC 纪元加偏移计算，不调用平台的 localtime/gmtime（Windows 上不支持负值等边界）。
    """
    return (_EPOCH + timedelta(seconds=ts)).astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")


def _span_error(limits: Limits) -> LogViewerError:
    return _invalid("时间跨度不能超过 %d 小时，请缩小时间范围" % limits.max_time_span_h)


def _check_time_combination(
    at_time: int | None, start_time: int | None, end_time: int | None, limits: Limits
) -> None:
    """不依赖当前时刻的组合规则（1.2）。"""
    if at_time is not None and (start_time is not None or end_time is not None):
        raise _invalid("at_time 不能与 start_time、end_time 同时提供")
    if end_time is not None and start_time is None:
        raise _invalid("提供 end_time 时必须同时提供 start_time")
    if start_time is not None and end_time is not None:
        if end_time <= start_time:
            raise _invalid("end_time 必须晚于 start_time")
        if end_time - start_time > limits.max_time_span_s:
            raise _span_error(limits)


def compute_time_range(params: SearchParams, now: int, limits: Limits) -> TimeRange:
    """按 1.2 计算 Time_Range；now 为当前时刻（整数秒）。不符即抛出 INVALID_ARGUMENT。

    - 只提供 at_time：[at_time − Point_Window, at_time + Point_Window)，起点不小于 0；
    - 只提供 start_time：[start_time, now)，start_time 须早于 now，跨度不超过 Max_Time_Span；
    - 提供 start_time 与 end_time：[start_time, end_time)；
    - 都不提供：[now − Default_Lookback, now)，起点不小于 0。
    """
    if type(now) is not int:
        raise TypeError("now 必须是整数秒")
    at_time, start_time, end_time = params.at_time, params.start_time, params.end_time
    _check_time_combination(at_time, start_time, end_time, limits)

    if at_time is not None:
        window = limits.point_window_s
        result = TimeRange(max(TIMESTAMP_MIN, at_time - window), at_time + window)
    elif start_time is not None and end_time is not None:
        result = TimeRange(start_time, end_time)
    elif start_time is not None:
        if start_time >= now:
            raise _invalid("只提供 start_time 时，start_time 必须早于当前时刻；查询未来时段请同时提供 end_time")
        result = TimeRange(start_time, now)
    else:
        result = TimeRange(max(TIMESTAMP_MIN, now - limits.default_lookback_s), now)

    if result.end <= result.start:
        raise _invalid("时间范围为空，请检查时间参数")
    if result.end - result.start > limits.max_time_span_s:
        raise _span_error(limits)
    return result


# --- regex 模式检查（8.1） ---

# 模式 → 是否为合法的扩展正则。检查本身无法进行时（找不到 grep）抛出 INVALID_ARGUMENT。
RegexChecker = Callable[[str], bool]
AsyncRegexChecker = Callable[[str], Awaitable[bool]]

GREP_CHECK_TIMEOUT_S = 2.0
# 只在系统目录中找 grep，不用服务进程的 PATH；grep 子进程的 PATH 也只有这些目录。
GREP_SEARCH_PATH = "/usr/bin:/bin"

# 保守规则：任意位置的「反斜杠后跟 1–9」都按反向引用处理，含 \\1 与 [\1]。
_BACKREFERENCE = re.compile(r"\\[1-9]")

_BACKREFERENCE_MESSAGE = (
    "regex 为 true 时模式不能含反向引用（反斜杠后跟数字 1 至 9）；如需按字面匹配这类文本，请改用 regex=false"
)
_INVALID_ERE_MESSAGE = "regex 为 true 时每个模式必须是合法的扩展正则（grep -E 语法）"
_GREP_UNAVAILABLE_MESSAGE = "跳板机暂时无法检查扩展正则，请改用 regex=false，或联系管理员"


def _grep_unavailable() -> LogViewerError:
    return _invalid(_GREP_UNAVAILABLE_MESSAGE)


def _grep_argv(pattern: str) -> list[Any]:
    """``grep -E -e <模式> -- /dev/null`` 的 argv；找不到 grep 时抛出 INVALID_ARGUMENT。

    POSIX 上模式按 UTF-8 编码为 bytes 传入，不依赖服务进程的 locale 编码。
    """
    grep = shutil.which("grep", path=GREP_SEARCH_PATH)
    if grep is None:
        _log.error("在 %s 中找不到 grep，无法检查扩展正则，regex=true 的请求一律拒绝", GREP_SEARCH_PATH)
        raise _grep_unavailable()
    arg: Any = pattern.encode("utf-8") if os.name == "posix" else pattern
    return [grep, "-E", "-e", arg, "--", os.devnull]


def _grep_env() -> dict[str, str]:
    return {"LC_ALL": "C", "PATH": GREP_SEARCH_PATH}


def _grep_verdict(returncode: int) -> bool:
    """退出码 0（匹配）或 1（无匹配）为合法；2 为语法错误，其他（含被信号终止）也按非法处理。"""
    if returncode not in (0, 1, 2):
        _log.warning("检查扩展正则时 grep 异常退出（退出状态 %d），按非法处理", returncode)
    return returncode in (0, 1)


def grep_ere_is_valid(pattern: str) -> bool:
    """同步检查：模式是否为合法的扩展正则（会阻塞调用线程至多 GREP_CHECK_TIMEOUT_S 秒）。"""
    argv = _grep_argv(pattern)
    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_grep_env(),
            timeout=GREP_CHECK_TIMEOUT_S,
            shell=False,
            check=False,
        )
    except subprocess.TimeoutExpired:  # subprocess.run 已杀掉并回收子进程
        _log.warning("检查扩展正则超时（%s 秒），按非法处理", GREP_CHECK_TIMEOUT_S)
        return False
    except OSError:
        _log.exception("无法启动 grep 检查扩展正则")
        raise _grep_unavailable() from None
    return _grep_verdict(proc.returncode)


async def grep_ere_is_valid_async(pattern: str) -> bool:
    """异步检查，规则同 ``grep_ere_is_valid``；超时或被取消时杀掉并回收 grep 子进程。"""
    argv = _grep_argv(pattern)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_grep_env(),
        )
    except OSError:
        _log.exception("无法启动 grep 检查扩展正则")
        raise _grep_unavailable() from None
    try:
        returncode = await asyncio.wait_for(proc.wait(), GREP_CHECK_TIMEOUT_S)
    except TimeoutError:
        _log.warning("检查扩展正则超时（%s 秒），按非法处理", GREP_CHECK_TIMEOUT_S)
        return False
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
    return _grep_verdict(returncode)


def grep_ere_check_works() -> bool:
    """启动自检：本机 grep 能区分合法与非法的扩展正则（"a" 合法、"(" 非法）。"""
    try:
        return grep_ere_is_valid("a") and not grep_ere_is_valid("(")
    except LogViewerError:
        return False


def _regex_patterns_to_check(params: SearchParams) -> tuple[str, ...]:
    """regex 为 true 时须交给 grep 检查的模式（去重保序）；先做反向引用检查。"""
    if not params.regex:
        return ()
    patterns = tuple(dict.fromkeys(params.include_patterns + params.exclude_patterns))
    if any(_BACKREFERENCE.search(p) for p in patterns):
        raise _invalid(_BACKREFERENCE_MESSAGE)
    return patterns


def _check_regex_patterns(params: SearchParams, checker: RegexChecker) -> None:
    for pattern in _regex_patterns_to_check(params):
        if not checker(pattern):
            raise _invalid(_INVALID_ERE_MESSAGE)


async def _check_regex_patterns_async(params: SearchParams, checker: AsyncRegexChecker) -> None:
    patterns = _regex_patterns_to_check(params)
    if not patterns:
        return
    try:
        async with asyncio.TaskGroup() as tg:  # 任一检查抛错时取消其余检查（并杀掉其 grep）
            tasks = [tg.create_task(checker(p)) for p in patterns]
    except BaseExceptionGroup as eg:
        raise eg.exceptions[0] from None
    if not all(t.result() for t in tasks):
        raise _invalid(_INVALID_ERE_MESSAGE)


def validate_search_logs(
    arguments: Any,
    *,
    limits: Limits,
    log_types: Collection[str],
    regex_checker: RegexChecker = grep_ere_is_valid,
) -> SearchParams:
    """校验 search_logs 的原始参数字典；不符即抛出 INVALID_ARGUMENT 的 LogViewerError。

    同步版本：regex 为 true 时逐个运行 grep，会阻塞调用线程；事件循环中请用
    ``validate_search_logs_async``。
    """
    params = _validate_search_logs_static(arguments, limits=limits, log_types=log_types)
    _check_regex_patterns(params, regex_checker)
    return params


async def validate_search_logs_async(
    arguments: Any,
    *,
    limits: Limits,
    log_types: Collection[str],
    regex_checker: AsyncRegexChecker = grep_ere_is_valid_async,
) -> SearchParams:
    """``validate_search_logs`` 的异步版本：regex 为 true 时并发运行 grep，不阻塞事件循环。"""
    params = _validate_search_logs_static(arguments, limits=limits, log_types=log_types)
    await _check_regex_patterns_async(params, regex_checker)
    return params


def _validate_search_logs_static(
    arguments: Any, *, limits: Limits, log_types: Collection[str]
) -> SearchParams:
    """除扩展正则合法性（需要运行 grep）之外的全部检查。"""
    args = _arguments_dict(arguments, SEARCH_LOGS_PARAMS)
    if "svr_ids" not in args:
        raise _invalid("缺少必填参数 svr_ids")
    if "log_type" not in args:
        raise _invalid("缺少必填参数 log_type，可用取值：" + "、".join(log_types))

    svr_ids = _check_svr_ids(args["svr_ids"], limits)
    log_type = _check_log_type(args["log_type"], log_types)
    def optional(name: str, check: Callable[[str, Any], Any], default: Any) -> Any:
        return check(name, args[name]) if name in args else default

    times = {k: _check_time_param(k, args[k]) for k in _TIME_PARAMS if k in args}
    _check_time_combination(times.get("at_time"), times.get("start_time"), times.get("end_time"), limits)
    include = optional("include_patterns", lambda n, v: _check_patterns(n, v, limits), ())
    exclude = optional("exclude_patterns", lambda n, v: _check_patterns(n, v, limits), ())
    regex = optional("regex", _check_bool, False)
    ignore_case = optional("ignore_case", _check_bool, False)
    before = optional("context_before", lambda n, v: _check_context(n, v, limits), 0)
    after = optional("context_after", lambda n, v: _check_context(n, v, limits), 0)

    return SearchParams(
        svr_ids=svr_ids,
        log_type=log_type,
        at_time=times.get("at_time"),
        start_time=times.get("start_time"),
        end_time=times.get("end_time"),
        include_patterns=include,
        exclude_patterns=exclude,
        regex=regex,
        ignore_case=ignore_case,
        context_before=before,
        context_after=after,
    )


def validate_list_servers(arguments: Any) -> None:
    """list_servers 不带参数：参数字典须为空或缺省，带任何参数名（即使值为 null）都拒绝。"""
    if arguments is None:
        return
    if not isinstance(arguments, Mapping):
        raise _invalid("参数必须是 JSON 对象")
    if len(arguments) > 0:
        raise _invalid("该工具不接受任何参数")


def validate_arguments(
    name: str, arguments: Any, config: Config, *, regex_checker: RegexChecker = grep_ere_is_valid
) -> SearchParams | None:
    """按工具名分派校验：search_logs 返回 SearchParams，list_servers 返回 None。

    同步版本，regex 为 true 时会阻塞调用线程；call_tool 中请用 ``validate_arguments_async``。
    """
    if name == SEARCH_LOGS:
        return validate_search_logs(
            arguments, limits=config.limits, log_types=tuple(config.log_types), regex_checker=regex_checker
        )
    if name == LIST_SERVERS:
        validate_list_servers(arguments)
        return None
    raise _invalid("未知的工具")


async def validate_arguments_async(
    name: str, arguments: Any, config: Config, *, regex_checker: AsyncRegexChecker = grep_ere_is_valid_async
) -> SearchParams | None:
    """``validate_arguments`` 的异步版本，供 call_tool（任务 8.4）在事件循环中调用。"""
    if name == SEARCH_LOGS:
        return await validate_search_logs_async(
            arguments, limits=config.limits, log_types=tuple(config.log_types), regex_checker=regex_checker
        )
    if name == LIST_SERVERS:
        validate_list_servers(arguments)
        return None
    raise _invalid("未知的工具")
