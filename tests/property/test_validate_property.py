"""Property 1：参数校验拒绝注入式输入（需求 1.1、1.2、1.3、8.1、8.2）。

经 LogViewerMCP.call_tool 调用（进程内，不经 HTTP）：

- 先生成合法的 search_logs 参数，再从变异目录中恰好施加一项违反 8.1（或 1.1、1.2、1.3）的变异：
  未定义的参数名、注入式或越界的 svr_id、svr_ids 的形状与个数、非法的 log_type、时间参数的类型与范围
  （字符串、小数、布尔值、毫秒级时间戳）、时间组合、模式的形状、个数、长度、控制字符、孤立代理项、
  反向引用与非法扩展正则、布尔参数与上下文行数。调用应返回 isError 与 INVALID_ARGUMENT，管理库替身与
  SSH 建连替身的调用次数为 0，Result_Store 中没有任何结果，并写下一条状态为 INVALID_ARGUMENT 的审计。
  每类变异单独参数化，各至少 100 例；
- 合法参数：调用不返回整次错误，摘要的 time_range 与按 1.2 独立计算的参照值（注入的时钟）相同；
- list_servers 带任何参数（即使值为 null）都返回 INVALID_ARGUMENT，且不访问管理库。

替身与配置取自 tests/unit/test_app.py（配置只定义 normal 与 login 两种 Log_Type，max_servers 为 7）。
扩展正则的合法性检查注入为替身：只把 INVALID_ERES 中的模式判为非法。每个例子在新的临时目录中
新建环境，并用 asyncio.run 运行一次调用。
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from log_viewer_mcp.common.formats import SVR_ID_MAX, SVR_ID_MIN
from log_viewer_mcp.server.app import create_mcp
from log_viewer_mcp.server.audit import AuditLog
from log_viewer_mcp.server.results import ResultStore
from log_viewer_mcp.server.tools import ToolServices
from log_viewer_mcp.server.validate import SEARCH_LOGS_PARAMS, TIMESTAMP_MAX, TIMESTAMP_MIN
from tests.unit.test_app import NOW, PASSWORD, TZ, FakeConnector, FakeDb, FakeRemote, make_config, payload

PROBE_CONFIG = make_config(Path("probe"))
LIMITS = PROBE_CONFIG.limits
LOG_TYPES = tuple(PROBE_CONFIG.log_types)
MAX_LEN = LIMITS.max_pattern_length
SPAN = LIMITS.max_time_span_s

# 注入的扩展正则检查替身只把这些模式判为非法；合法参数中的模式不取这些值。
INVALID_ERES = frozenset({"(", "a(b", "[", "[z-a]", "a{2,1}", ")("})
BACKREFERENCE = re.compile(r"\\[1-9]")

PATTERN_FIELDS = ("include_patterns", "exclude_patterns")
BOOL_FIELDS = ("regex", "ignore_case")
CONTEXT_FIELDS = ("context_before", "context_after")
TIME_FIELDS = ("at_time", "start_time", "end_time")

SETTINGS = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)


async def reject_known_invalid(pattern: str) -> bool:
    return pattern not in INVALID_ERES


# ---------------------------------------------------------------------------
# 环境：每个例子一套新的替身与临时目录
# ---------------------------------------------------------------------------


class Env:
    def __init__(self, base: Path, now: int) -> None:
        self.config = make_config(base)
        self.store = ResultStore.from_config(self.config.results)
        self.store.prepare()
        self.db = FakeDb()
        self.connector = FakeConnector()
        self.remote = FakeRemote()
        services = ToolServices(
            config=self.config, db=self.db, connector=self.connector, store=self.store, clock=lambda: now,
            run_listing=self.remote.run_listing, run_query=self.remote.run_query,
        )
        self.audit = AuditLog(self.config.audit.dir, TZ, 90, db_password=PASSWORD, clock=lambda: NOW)
        self.mcp = create_mcp(self.config, services, self.audit, regex_checker=reject_known_invalid)

    def audit_records(self) -> list[dict]:
        records = []
        for name in sorted(os.listdir(self.config.audit.dir)):
            with open(os.path.join(self.config.audit.dir, name), encoding="utf-8") as f:
                records.extend(json.loads(line) for line in f)
        return records

    def stored_results(self) -> list[str]:
        return os.listdir(self.config.results.dir)


def call(base: Path, now: int, tool: str, args: dict[str, Any]) -> tuple[Env, dict, bool]:
    async def go() -> tuple[Env, dict, bool]:
        env = Env(base, now)
        result = await env.mcp.call_tool(tool, copy.deepcopy(args))
        return env, payload(result), bool(result.isError)

    return asyncio.run(go())


# ---------------------------------------------------------------------------
# 合法参数
# ---------------------------------------------------------------------------

SAFE_CHARS = st.characters(exclude_categories=("Cc", "Cs"))
INJECTION_TEXTS = (
    "; rm -rf /",
    "$(reboot)",
    "`id`",
    "| sh",
    "&& shutdown -h now",
    "> /usr/local/ds/SFS2X/logs/smartfox.log",
    "-e",
    "--include=*",
    "-f /etc/passwd",
    "' OR '1'='1",
    "1; DROP TABLE tbl_webserver; --",
    "*",
    "../../etc/shadow",
    "登录 失败",
    "\\",
    "'\"",
)


def pattern_text(min_size: int = 1, max_size: int = MAX_LEN) -> st.SearchStrategy[str]:
    """合法模式：无 Cc 类字符、无孤立代理项、无反向引用、不在 INVALID_ERES 中（regex 为真或假都合法）。"""
    return st.text(SAFE_CHARS, min_size=min_size, max_size=max_size).filter(
        lambda p: BACKREFERENCE.search(p) is None and p not in INVALID_ERES
    )


VALID_PATTERN = st.one_of(st.sampled_from(INJECTION_TEXTS), pattern_text())
VALID_PATTERNS = st.lists(VALID_PATTERN, min_size=1, max_size=LIMITS.max_patterns)
VALID_SVR_ID = st.integers(SVR_ID_MIN, SVR_ID_MAX)
TIMESTAMP = st.integers(TIMESTAMP_MIN, TIMESTAMP_MAX)


@st.composite
def valid_case(draw: Callable) -> tuple[int, dict[str, Any]]:
    """(当前时刻, 合法参数)；未提供的可选参数可能以 null 出现（视为未提供）。"""
    now = draw(st.integers(1, TIMESTAMP_MAX))
    args: dict[str, Any] = {
        "svr_ids": draw(st.lists(VALID_SVR_ID, min_size=1, max_size=LIMITS.max_servers)),
        "log_type": draw(st.sampled_from(LOG_TYPES)),
    }
    mode = draw(st.sampled_from(("none", "at", "start", "start_end")))
    if mode == "at":
        args["at_time"] = draw(TIMESTAMP)
    elif mode == "start":
        args["start_time"] = draw(st.integers(max(TIMESTAMP_MIN, now - SPAN), now - 1))
    elif mode == "start_end":
        start = draw(st.integers(TIMESTAMP_MIN, TIMESTAMP_MAX - 1))
        args["start_time"] = start
        args["end_time"] = draw(st.integers(start + 1, min(start + SPAN, TIMESTAMP_MAX)))

    optional: dict[str, st.SearchStrategy[Any]] = {
        **{k: VALID_PATTERNS for k in PATTERN_FIELDS},
        **{k: st.booleans() for k in BOOL_FIELDS},
        **{k: st.integers(0, LIMITS.max_context_lines) for k in CONTEXT_FIELDS},
    }
    for name, strategy in optional.items():
        choice = draw(st.sampled_from(("absent", "null", "value")))
        if choice == "value":
            args[name] = draw(strategy)
        elif choice == "null":
            args[name] = None
    for name in TIME_FIELDS:
        if name not in args and draw(st.booleans()):
            args[name] = None
    return now, args


def reference_time_range(args: dict[str, Any], now: int) -> dict[str, Any]:
    """按 1.2 独立计算 Time_Range（null 视为未提供；起点按 validate.py 写定的边界取法不小于 0）。"""
    at, start, end = (args.get(k) for k in TIME_FIELDS)
    if at is not None:
        lo, hi = at - LIMITS.point_window_s, at + LIMITS.point_window_s
    elif start is not None and end is not None:
        lo, hi = start, end
    elif start is not None:
        lo, hi = start, now
    else:
        lo, hi = now - LIMITS.default_lookback_s, now
    lo = max(0, lo)

    def server_time(ts: int) -> str:  # GMT-2：UTC 减 2 小时
        return (datetime(1970, 1, 1) + timedelta(seconds=ts - 2 * 3600)).strftime("%Y-%m-%d %H:%M:%S")

    return {"start": lo, "end": hi, "start_server": server_time(lo), "end_server": server_time(hi)}


# ---------------------------------------------------------------------------
# 变异目录：每项恰好违反一条规则
# ---------------------------------------------------------------------------

Mutation = Callable[[Callable, dict[str, Any], int], tuple[dict[str, Any], int, str]]

# JSON（RFC 8259）没有 NaN 与 Infinity，生成的小数一律为有限值。
JSON_SCALARS = st.one_of(
    st.none(), st.booleans(), st.integers(), st.floats(allow_nan=False, allow_infinity=False), st.text(max_size=20),
    st.sampled_from(INJECTION_TEXTS),
)
JSON_VALUES = st.recursive(
    JSON_SCALARS,
    lambda children: st.one_of(
        st.lists(children, max_size=3), st.dictionaries(st.text(max_size=8), children, max_size=3)
    ),
    max_leaves=6,
)
INJECTION_PARAM_NAMES = (
    "path", "cmd", "command", "file", "filename", "files", "dir", "directory", "glob", "options", "args",
    "sql", "query", "shell", "exec", "script", "program", "grep_options", "svr_id", "SVR_IDS", "log_types",
    "pattern", "", " svr_ids", "svr_ids ", "svr_ids\x00",
)
UNKNOWN_PARAM_NAME = st.one_of(st.sampled_from(INJECTION_PARAM_NAMES), st.text(max_size=20)).filter(
    lambda n: n not in SEARCH_LOGS_PARAMS
)


def _replace_or_set_list(draw: Callable, args: dict[str, Any], name: str, bad: Any) -> None:
    """把 args[name]（列表）中的一个元素换成 bad；没有该列表时设为 [bad]。"""
    current = args.get(name)
    if isinstance(current, list) and current:
        current[draw(st.integers(0, len(current) - 1))] = bad
    else:
        args[name] = [bad]


def m_unknown_param(draw, args, now):
    args[draw(UNKNOWN_PARAM_NAME)] = draw(JSON_VALUES)
    return args, now, "unknown_param"


BAD_SVR_ID = st.one_of(
    st.sampled_from(
        ("1; rm -rf /", "1 OR 1=1", "1001", "0x3e9", " 1001", "1001\n", True, False, 1.0, 1001.0, 0, -1,
         SVR_ID_MAX + 1, 2**63, None, [1001], {"svr_id": 1001})
    ),
    st.integers(max_value=SVR_ID_MIN - 1),
    st.integers(min_value=SVR_ID_MAX + 1),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=20),
)


def m_bad_svr_id(draw, args, now):
    _replace_or_set_list(draw, args, "svr_ids", draw(BAD_SVR_ID))
    return args, now, "svr_id"


def m_svr_ids_shape(draw, args, now):
    kind = draw(st.sampled_from(("missing", "null", "not_list", "empty", "too_many")))
    if kind == "missing":
        del args["svr_ids"]
    elif kind == "null":
        args["svr_ids"] = None
    elif kind == "not_list":
        args["svr_ids"] = draw(
            st.one_of(st.sampled_from(("1001", 1001, True, "1; rm -rf /", "[1001]", {"0": 1001})), st.text(), st.integers())
        )
    elif kind == "empty":
        args["svr_ids"] = []
    else:
        n = LIMITS.max_servers
        args["svr_ids"] = draw(st.lists(VALID_SVR_ID, min_size=n + 1, max_size=n + 5))
    return args, now, "svr_ids_shape"


BAD_LOG_TYPE = st.one_of(
    st.sampled_from(
        ("analysis", "pulsar", "NORMAL", "Normal", "normal ", "normal\n", "../normal", "/usr/local/ds/SFS2X/logs",
         "normal; rm -rf /", "normal' OR '1'='1", "*", "", 1, True, ["normal"], {"normal": 1})
    ),
    st.text(max_size=20),
).filter(lambda v: not (isinstance(v, str) and v in LOG_TYPES))


def m_log_type(draw, args, now):
    kind = draw(st.sampled_from(("missing", "null", "bad")))
    if kind == "missing":
        del args["log_type"]
    elif kind == "null":
        args["log_type"] = None
    else:
        args["log_type"] = draw(BAD_LOG_TYPE)
    return args, now, "log_type"


BAD_TIME_VALUE = st.one_of(
    st.integers(1_000_000_000, TIMESTAMP_MAX).map(lambda t: t * 1000),  # 毫秒级时间戳
    st.integers(min_value=TIMESTAMP_MAX + 1),
    st.integers(max_value=TIMESTAMP_MIN - 1),
    TIMESTAMP.map(str),
    TIMESTAMP.map(float),
    TIMESTAMP.map(lambda t: t + 0.5),
    st.floats(allow_nan=False, allow_infinity=False),
    st.sampled_from(
        (True, False, "1789668000", "2026-09-17 16:00:00", "2026-09-17T16:00:00-02:00", "now", "-1h",
         "1789668000; rm -rf /", [1789668000], {"ts": 1789668000})
    ),
)


def m_time_value(draw, args, now):
    present = [k for k in TIME_FIELDS if args.get(k) is not None]
    name = draw(st.sampled_from(present)) if present else "at_time"
    args[name] = draw(BAD_TIME_VALUE)
    return args, now, "time_value"


def m_time_combination(draw, args, now):
    for k in TIME_FIELDS:
        args.pop(k, None)
    kinds = ["at_with_range", "end_only", "end_not_after_start", "span_exceeded", "start_not_before_now"]
    if now > SPAN:
        kinds.append("start_only_span_exceeded")
    kind = draw(st.sampled_from(kinds))
    if kind == "at_with_range":
        args["at_time"] = draw(TIMESTAMP)
        for k in draw(st.sampled_from((("start_time",), ("end_time",), ("start_time", "end_time")))):
            args[k] = draw(TIMESTAMP)
    elif kind == "end_only":
        args["end_time"] = draw(TIMESTAMP)
    elif kind == "end_not_after_start":
        start = draw(TIMESTAMP)
        args["start_time"] = start
        args["end_time"] = draw(st.integers(TIMESTAMP_MIN, start))
    elif kind == "span_exceeded":
        start = draw(st.integers(TIMESTAMP_MIN, TIMESTAMP_MAX - SPAN - 1))
        args["start_time"] = start
        args["end_time"] = draw(st.integers(start + SPAN + 1, TIMESTAMP_MAX))
    elif kind == "start_not_before_now":
        args["start_time"] = draw(st.integers(now, TIMESTAMP_MAX))
    else:
        args["start_time"] = draw(st.integers(TIMESTAMP_MIN, now - SPAN - 1))
    return args, now, "time_combination"


def _insert(draw, base: str, ch: str) -> str:
    i = draw(st.integers(0, len(base)))
    return base[:i] + ch + base[i:]


def m_pattern_list(draw, args, now):
    name = draw(st.sampled_from(PATTERN_FIELDS))
    kind = draw(st.sampled_from(("not_list", "empty", "too_many")))
    if kind == "not_list":
        args[name] = draw(st.one_of(st.sampled_from(("CMD", 1, True, {"p": "CMD"}, "; rm -rf /")), st.text()))
    elif kind == "empty":
        args[name] = []
    else:
        n = LIMITS.max_patterns
        args[name] = draw(st.lists(VALID_PATTERN, min_size=n + 1, max_size=n + 3))
    return args, now, "pattern_list"


def m_bad_pattern(draw, args, now):
    name = draw(st.sampled_from(PATTERN_FIELDS))
    kind = draw(st.sampled_from(("empty", "too_long", "control", "surrogate", "not_string")))
    if kind == "empty":
        bad: Any = ""
    elif kind == "too_long":
        bad = draw(st.text(SAFE_CHARS, min_size=MAX_LEN + 1, max_size=MAX_LEN + 20))
    elif kind == "control":  # NUL、CR、LF、TAB、ESC、DEL、C1 等，插在任意位置
        bad = _insert(draw, draw(pattern_text(min_size=0, max_size=MAX_LEN - 1)), draw(st.characters(categories=("Cc",))))
    elif kind == "surrogate":  # 孤立代理项无法编码为 UTF-8
        bad = _insert(draw, draw(pattern_text(min_size=0, max_size=MAX_LEN - 1)), draw(st.characters(categories=("Cs",))))
    else:
        bad = draw(st.sampled_from((1, 0, True, 1.0, ["CMD"], {"p": "CMD"})))
    _replace_or_set_list(draw, args, name, bad)
    return args, now, "pattern"


def m_backreference(draw, args, now):
    half = (MAX_LEN - 2) // 2
    prefix = draw(pattern_text(min_size=0, max_size=half))
    suffix = draw(pattern_text(min_size=0, max_size=half))
    bad = prefix + "\\" + draw(st.sampled_from("123456789")) + suffix
    args["regex"] = True
    _replace_or_set_list(draw, args, draw(st.sampled_from(PATTERN_FIELDS)), bad)
    return args, now, "backreference"


def m_invalid_ere(draw, args, now):
    args["regex"] = True
    _replace_or_set_list(draw, args, draw(st.sampled_from(PATTERN_FIELDS)), draw(st.sampled_from(sorted(INVALID_ERES))))
    return args, now, "invalid_ere"


def m_bool(draw, args, now):
    args[draw(st.sampled_from(BOOL_FIELDS))] = draw(
        st.one_of(
            st.sampled_from(("true", "false", 1, 0, 1.0, 0.0, "yes", [], {}, [True], "--regexp")),
            st.integers(),
            st.text(max_size=10),
        )
    )
    return args, now, "bool"


def m_context(draw, args, now):
    args[draw(st.sampled_from(CONTEXT_FIELDS))] = draw(
        st.one_of(
            st.integers(max_value=-1),
            st.integers(min_value=LIMITS.max_context_lines + 1),
            st.sampled_from((True, False, 1.0, 2.0, "2", "-A 2", "2; rm -rf /", [2], {"n": 2})),
            st.floats(allow_nan=False, allow_infinity=False),
        )
    )
    return args, now, "context"


MUTATIONS: tuple[Mutation, ...] = (
    m_unknown_param,
    m_bad_svr_id,
    m_svr_ids_shape,
    m_log_type,
    m_time_value,
    m_time_combination,
    m_pattern_list,
    m_bad_pattern,
    m_backreference,
    m_invalid_ere,
    m_bool,
    m_context,
)


# ---------------------------------------------------------------------------
# 属性
# ---------------------------------------------------------------------------


# Feature: log-viewer-mcp, Property 1: 参数校验拒绝注入式输入
@pytest.mark.parametrize("mutation", MUTATIONS, ids=[m.__name__ for m in MUTATIONS])
@SETTINGS
@given(case=valid_case(), data=st.data())
def test_single_mutation_rejected_before_db_and_ssh(tmp_path_factory, mutation, case, data) -> None:
    """每类变异各至少 100 例。**Validates: Requirements 1.1, 1.2, 1.3, 8.1, 8.2**"""
    now, valid = case
    args, now, kind = mutation(data.draw, copy.deepcopy(valid), now)

    env, body, is_error = call(tmp_path_factory.mktemp("p1-invalid"), now, "search_logs", args)

    assert is_error is True
    assert body["error"]["code"] == "INVALID_ARGUMENT", body
    if kind == "time_value":
        assert "秒级 Unix 时间戳" in body["error"]["message"]
    assert env.db.calls == 0 and env.connector.calls == 0
    assert env.stored_results() == []
    (record,) = env.audit_records()
    assert record["tool"] == "search_logs" and record["status"] == "INVALID_ARGUMENT"
    assert record["targets"] == [] and record["result_handle"] is None
    assert record["params"] == args


# Feature: log-viewer-mcp, Property 1: 参数校验拒绝注入式输入
@SETTINGS
@given(case=valid_case())
def test_valid_arguments_pass_and_time_range_follows_1_2(tmp_path_factory, case) -> None:
    """**Validates: Requirements 1.1, 1.2, 1.3, 8.1, 8.2**"""
    now, args = case

    env, body, is_error = call(tmp_path_factory.mktemp("p1-valid"), now, "search_logs", args)

    assert is_error is False, body
    assert body["time_range"] == reference_time_range(args, now)
    assert body["server_timezone"] == "GMT-2"
    assert [s["svr_id"] for s in body["servers"]] == list(dict.fromkeys(args["svr_ids"]))
    assert env.db.calls == 1
    (record,) = env.audit_records()
    assert record["status"] == "ok"


# Feature: log-viewer-mcp, Property 1: 参数校验拒绝注入式输入
@SETTINGS
@given(
    args=st.dictionaries(
        st.one_of(st.sampled_from(INJECTION_PARAM_NAMES + SEARCH_LOGS_PARAMS), st.text(max_size=20)),
        JSON_VALUES,
        min_size=1,
        max_size=3,
    )
)
def test_list_servers_rejects_any_argument(tmp_path_factory, args) -> None:
    """**Validates: Requirements 8.1, 8.2**"""
    env, body, is_error = call(tmp_path_factory.mktemp("p1-list"), NOW, "list_servers", args)

    assert is_error is True
    assert body["error"]["code"] == "INVALID_ARGUMENT"
    assert env.db.calls == 0 and env.connector.calls == 0
    (record,) = env.audit_records()
    assert record["tool"] == "list_servers" and record["status"] == "INVALID_ARGUMENT"
