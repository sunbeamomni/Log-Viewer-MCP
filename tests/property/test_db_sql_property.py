"""Property 4：发往 Admin_DB 的 SQL 只有固定语句（需求 7.1、7.2、7.6）。

在驱动 API 层（db.py 用到的 connect / cmd_query / cursor(prepared=True) / execute / fetchall /
close / rollback）用替身记录每条语句与绑定参数，把任意原始请求参数（含注入式的 svr_id、
log_type 与模式）依次交给 validate.py 与 AdminDb，检查：

- 发出的 SQL 文本只属于 db.ALLOWED_SQL（7.1 的两条查询加 START TRANSACTION READ ONLY）；
- 查询只在预处理游标上、START TRANSACTION READ ONLY 之后、rollback 之前执行；
- svr_id 只作为 SQL_LOOKUP_SERVER 的唯一绑定参数出现，且是合法的整数；
- 不合法的 svr_id（含注入式字符串）在任何驱动调用之前就被拒绝。

另有一个对真实 MySQL 的变体（只在 compose 的测试运行容器中运行，未设置 LVM_IT=1 时跳过）：
包装 mysql.connector 的真实连接记录同样的调用。驱动建连时自带的会话语句不经过这些接口，
不在记录之内（design.md「Property 4」把它们计入允许集合）。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from typing import Any

import pytest
from hypothesis import event, given, settings
from hypothesis import strategies as st

from log_viewer_mcp.common.errors import ErrorCode, LogViewerError
from log_viewer_mcp.common.formats import SVR_ID_MAX, SVR_ID_MIN, is_valid_svr_id
from log_viewer_mcp.server.config import AdminDbConfig, Limits
from log_viewer_mcp.server.db import (
    ALLOWED_SQL,
    SQL_LIST_SERVERS,
    SQL_LOOKUP_SERVER,
    SQL_START_READ_ONLY,
    AdminDb,
)
from log_viewer_mcp.server.validate import validate_list_servers, validate_search_logs

LIMITS = Limits()
LOG_TYPES = ("normal", "login", "analysis", "pulsar")
DB_CONFIG = AdminDbConfig("10.0.0.2", 3306, "logview", "admin", "/etc/x/db_password", "pw")

# ---------------------------------------------------------------------------
# 驱动替身：记录语句与绑定参数
# ---------------------------------------------------------------------------


class DriverError(Exception):
    pass


class FakeCursor:
    def __init__(self, log: list[tuple], prepared: bool, fail_on: str | None) -> None:
        self._log = log
        self._prepared = prepared
        self._fail_on = fail_on
        self._rows: list[Any] = []

    def execute(self, sql: Any, params: Any = ()) -> None:
        self._log.append(("execute", sql, params, self._prepared))
        if self._fail_on == "execute":
            raise DriverError("query failed")
        if sql == SQL_LOOKUP_SERVER:
            svr_id = params[0]
            if svr_id % 2 == 0:  # 偶数有记录，其中 3 的倍数的 ip_inner 不合法
                ip = "10.0.1.256" if svr_id % 3 == 0 else "10.0.1.11"
                self._rows = [(svr_id, ip)]
            else:
                self._rows = []
        elif sql == SQL_LIST_SERVERS:
            self._rows = [(1001, "S1", "eu", "10.0.1.11", 0), (2001, "T1", "test", None, 1)]
        else:
            self._rows = []

    def fetchall(self) -> list[Any]:
        rows, self._rows = self._rows, []
        return rows

    def close(self) -> None:
        self._log.append(("cursor.close",))


class FakeConnection:
    def __init__(self, log: list[tuple], fail_on: str | None) -> None:
        self._log = log
        self._fail_on = fail_on

    def cmd_query(self, sql: Any) -> None:
        self._log.append(("cmd_query", sql))
        if self._fail_on == "start":
            raise DriverError("cannot start read only transaction")

    def cursor(self, prepared: bool = False, **kwargs: Any) -> FakeCursor:
        self._log.append(("cursor", prepared, kwargs))
        return FakeCursor(self._log, prepared, self._fail_on)

    def rollback(self) -> None:
        self._log.append(("rollback",))

    def close(self) -> None:
        self._log.append(("close",))


class FakeDriver:
    def __init__(self, fail_on: str | None = None) -> None:
        self.fail_on = fail_on
        self.log: list[tuple] = []

    def connect(self, **kwargs: Any) -> FakeConnection:
        self.log.append(("connect",))
        if self.fail_on == "connect":
            raise DriverError("cannot connect")
        return FakeConnection(self.log, self.fail_on)


# ---------------------------------------------------------------------------
# 检查记录
# ---------------------------------------------------------------------------


def check_driver_log(log: list[tuple]) -> list[int]:
    """检查一次调用的驱动记录满足 Property 4，返回按顺序作为绑定参数查询的 svr_id。"""
    sent_sql = [e[1] for e in log if e[0] in ("cmd_query", "execute")]
    assert set(sent_sql) <= ALLOWED_SQL, sent_sql

    started = finished = False
    looked_up: list[int] = []
    for event in log:
        kind = event[0]
        if kind == "cmd_query":
            # cmd_query 只用来开启只读事务，每个连接一次。
            assert event[1] == SQL_START_READ_ONLY and not started, event
            started = True
        elif kind == "cursor":
            assert event[1] is True and event[2] == {}, event
            assert started and not finished, "游标在只读事务之外创建"
        elif kind == "execute":
            _, sql, params, prepared = event
            assert prepared is True, "查询没有用预处理游标"
            assert started and not finished, "查询不在 START TRANSACTION READ ONLY 之后"
            if sql == SQL_LOOKUP_SERVER:
                # svr_id 只作为唯一的绑定参数，且是合法的整数。
                assert type(params) is tuple and len(params) == 1, params
                assert is_valid_svr_id(params[0]), params
                looked_up.append(params[0])
            else:
                assert sql == SQL_LIST_SERVERS and params == (), event
        elif kind == "rollback":
            assert started, "rollback 前没有开启事务"
            finished = True
        else:
            assert kind in ("connect", "cursor.close", "close"), event
    return looked_up


# ---------------------------------------------------------------------------
# 生成器：边界值与注入式取值
# ---------------------------------------------------------------------------

INJECTION_TEXT = st.sampled_from(
    [
        "1001",
        "1001; DROP TABLE tbl_webserver",
        "1 OR 1=1",
        "1' OR '1'='1",
        "1001 UNION SELECT svr_id, ip_inner FROM tbl_webserver",
        "1001; UPDATE tbl_webserver SET ip_inner='1.2.3.4'",
        "1001 -- ",
        "1001/*",
        "?",
        "0x3E9",
        "1e3",
        " 1001",
        "1001\n",
        "١٠٠١",
        "",
        "NULL",
        "%",
        "\\",
        "1001`",
        '1001"',
        "'; SELECT SLEEP(10); --",
        "\x00",
    ]
)

VALID_SVR_ID = st.one_of(
    st.sampled_from([SVR_ID_MIN, SVR_ID_MIN + 1, 1001, SVR_ID_MAX - 1, SVR_ID_MAX]),
    st.integers(SVR_ID_MIN, SVR_ID_MAX),
)

INVALID_SVR_ID = st.one_of(
    st.sampled_from([0, -1, SVR_ID_MAX + 1, 2**63, -(2**63), True, False, None, 1001.0, float("nan")]),
    st.integers(max_value=SVR_ID_MIN - 1),
    st.integers(min_value=SVR_ID_MAX + 1),
    INJECTION_TEXT,
    st.text(max_size=20),
    st.lists(VALID_SVR_ID, max_size=2),
    st.dictionaries(st.text(max_size=3), VALID_SVR_ID, max_size=1),
)

SVR_ID_ELEMENT = st.one_of(VALID_SVR_ID, VALID_SVR_ID, INVALID_SVR_ID)

SVR_IDS = st.one_of(
    st.lists(VALID_SVR_ID, min_size=1, max_size=LIMITS.max_servers),
    st.lists(st.sampled_from([SVR_ID_MIN, SVR_ID_MAX, 1001]), min_size=1, max_size=LIMITS.max_servers),
    st.lists(SVR_ID_ELEMENT, max_size=LIMITS.max_servers + 2),
    INVALID_SVR_ID,
    VALID_SVR_ID,
)

PATTERN = st.one_of(INJECTION_TEXT, st.text(min_size=1, max_size=40), st.text(max_size=LIMITS.max_pattern_length + 2))
PATTERNS = st.one_of(st.lists(PATTERN, min_size=1, max_size=3), st.lists(PATTERN, max_size=LIMITS.max_patterns + 1), INJECTION_TEXT)

# 能通过校验的模式：注入式文本中不含控制字符的非空项，或普通文本。
VALID_PATTERN = st.one_of(
    INJECTION_TEXT.filter(lambda s: s != "" and s.isprintable()),
    st.text(st.characters(codec="utf-8", exclude_categories=("Cc", "Cs")), min_size=1, max_size=40),
)

# 能通过校验的请求：模式与 log_type 之外的值都合法，让大部分例子真正到达驱动。
VALID_SEARCH_ARGUMENTS = st.fixed_dictionaries(
    {
        "svr_ids": st.lists(VALID_SVR_ID, min_size=1, max_size=LIMITS.max_servers),
        "log_type": st.sampled_from(LOG_TYPES),
    },
    optional={
        "include_patterns": st.lists(VALID_PATTERN, min_size=1, max_size=LIMITS.max_patterns),
        "exclude_patterns": st.lists(VALID_PATTERN, min_size=1, max_size=LIMITS.max_patterns),
        "ignore_case": st.booleans(),
        "context_before": st.integers(0, LIMITS.max_context_lines),
        "context_after": st.integers(0, LIMITS.max_context_lines),
    },
)

ANY_SEARCH_ARGUMENTS = st.builds(
    lambda base, extra: {**base, **extra},
    st.fixed_dictionaries(
        {
            "svr_ids": SVR_IDS,
            "log_type": st.one_of(st.sampled_from(LOG_TYPES), INJECTION_TEXT),
        },
        optional={
            "include_patterns": st.one_of(st.none(), PATTERNS),
            "exclude_patterns": st.one_of(st.none(), PATTERNS),
            "regex": st.one_of(st.none(), st.booleans()),
            "ignore_case": st.one_of(st.none(), st.booleans(), INJECTION_TEXT),
            "context_before": st.one_of(st.none(), st.integers(-1, LIMITS.max_context_lines + 1)),
            "context_after": st.one_of(st.none(), st.integers(-1, LIMITS.max_context_lines + 1)),
        },
    ),
    st.one_of(
        st.just({}),
        st.just({}),
        st.just({}),
        st.dictionaries(st.sampled_from(["sql", "svr_id", "query", "ip_inner", "where"]), INJECTION_TEXT, max_size=2),
    ),
)

SEARCH_ARGUMENTS = st.one_of(VALID_SEARCH_ARGUMENTS, ANY_SEARCH_ARGUMENTS)

LIST_ARGUMENTS = st.one_of(
    st.none(),
    st.just({}),
    st.dictionaries(st.sampled_from(["svr_id", "svr_ids", "sql", "where", "order_by"]), st.one_of(st.none(), INJECTION_TEXT, VALID_SVR_ID), min_size=1, max_size=2),
    INJECTION_TEXT,
)

FAIL_ON = st.sampled_from([None, None, None, "start", "execute"])


# ---------------------------------------------------------------------------
# 执行一次请求
# ---------------------------------------------------------------------------


def run_search(arguments: Any, db: AdminDb) -> tuple[tuple[int, ...] | None, Any]:
    """validate → lookup_servers；返回 (校验后的 svr_ids 或 None, 结果或异常)。"""
    try:
        params = validate_search_logs(arguments, limits=LIMITS, log_types=LOG_TYPES)
    except LogViewerError as e:
        assert e.code is ErrorCode.INVALID_ARGUMENT
        return None, e
    try:
        return params.svr_ids, asyncio.run(db.lookup_servers(params.svr_ids))
    except LogViewerError as e:
        return params.svr_ids, e


def run_list(arguments: Any, db: AdminDb) -> tuple[bool, Any]:
    """validate_list_servers → list_servers；返回 (是否通过校验, 结果或异常)。"""
    try:
        validate_list_servers(arguments)
    except LogViewerError as e:
        assert e.code is ErrorCode.INVALID_ARGUMENT
        return False, e
    try:
        return True, asyncio.run(db.list_servers())
    except LogViewerError as e:
        return True, e


# ---------------------------------------------------------------------------
# 属性测试（驱动替身）
# ---------------------------------------------------------------------------


# Feature: log-viewer-mcp, Property 4: SQL 只有固定语句
@settings(deadline=None)
@given(arguments=SEARCH_ARGUMENTS, fail_on=FAIL_ON)
def test_search_logs_sends_only_fixed_sql(arguments: dict[str, Any], fail_on: str | None) -> None:
    """**Validates: Requirements 7.1, 7.2, 7.6**"""
    driver = FakeDriver(fail_on)
    svr_ids, outcome = run_search(arguments, AdminDb(DB_CONFIG, connect=driver.connect))
    event("通过校验" if svr_ids is not None else "校验拒绝")

    if svr_ids is None:  # 校验不通过：不访问数据库
        assert driver.log == []
        return

    looked_up = check_driver_log(driver.log)
    if fail_on is None:
        assert looked_up == list(svr_ids)  # 每个去重后的 svr_id 恰好作为绑定参数查询一次
        assert set(outcome) == set(svr_ids)
    else:
        assert isinstance(outcome, LogViewerError) and outcome.code is ErrorCode.DB_UNAVAILABLE
        if fail_on == "start":  # 只读事务没开启时不执行任何查询
            assert looked_up == [] and not any(e[0] in ("cursor", "execute") for e in driver.log)


# Feature: log-viewer-mcp, Property 4: SQL 只有固定语句
@settings(deadline=None)
@given(svr_ids=st.lists(SVR_ID_ELEMENT, max_size=LIMITS.max_servers + 2))
def test_lookup_servers_rejects_invalid_svr_ids_before_driver(svr_ids: list[Any]) -> None:
    """绕过 validate.py 直接调用 AdminDb：不合法的 svr_id（含注入式字符串）不进入驱动。

    **Validates: Requirements 7.1, 7.2, 7.6**
    """
    driver = FakeDriver()
    db = AdminDb(DB_CONFIG, connect=driver.connect)
    if all(is_valid_svr_id(v) for v in svr_ids):
        result = asyncio.run(db.lookup_servers(svr_ids))
        assert check_driver_log(driver.log) == list(dict.fromkeys(svr_ids))
        assert set(result) == set(svr_ids)
    else:
        with pytest.raises(LogViewerError) as exc:
            asyncio.run(db.lookup_servers(svr_ids))
        assert exc.value.code is ErrorCode.INVALID_ARGUMENT
        assert driver.log == []


# Feature: log-viewer-mcp, Property 4: SQL 只有固定语句
@settings(deadline=None)
@given(arguments=LIST_ARGUMENTS, fail_on=FAIL_ON)
def test_list_servers_sends_only_fixed_sql(arguments: Any, fail_on: str | None) -> None:
    """**Validates: Requirements 7.1, 7.2, 7.6**"""
    driver = FakeDriver(fail_on)
    accepted, outcome = run_list(arguments, AdminDb(DB_CONFIG, connect=driver.connect))

    if not accepted:
        assert driver.log == []
        return

    assert check_driver_log(driver.log) == []
    executed = [e[1] for e in driver.log if e[0] == "execute"]
    if fail_on is None:
        assert executed == [SQL_LIST_SERVERS]
        assert isinstance(outcome, list)
    else:
        assert isinstance(outcome, LogViewerError) and outcome.code is ErrorCode.DB_UNAVAILABLE
        assert executed == ([] if fail_on == "start" else [SQL_LIST_SERVERS])


# ---------------------------------------------------------------------------
# 变体：包装真实的 mysql.connector 连接（只在集成测试容器中运行）
# ---------------------------------------------------------------------------

_IT = os.environ.get("LVM_IT") == "1"


class RecordingCursor:
    def __init__(self, cursor: Any, log: list[tuple], prepared: bool) -> None:
        self._cursor = cursor
        self._log = log
        self._prepared = prepared

    def execute(self, sql: Any, params: Any = ()) -> Any:
        self._log.append(("execute", sql, params, self._prepared))
        return self._cursor.execute(sql, params)

    def fetchall(self) -> Any:
        return self._cursor.fetchall()

    def close(self) -> Any:
        self._log.append(("cursor.close",))
        return self._cursor.close()


class RecordingConnection:
    """只暴露 db.py 用到的接口；用到其他接口即 AttributeError，测试失败。"""

    def __init__(self, conn: Any, log: list[tuple]) -> None:
        self._conn = conn
        self._log = log

    def cmd_query(self, sql: Any) -> Any:
        self._log.append(("cmd_query", sql))
        return self._conn.cmd_query(sql)

    def cursor(self, prepared: bool = False, **kwargs: Any) -> RecordingCursor:
        self._log.append(("cursor", prepared, kwargs))
        return RecordingCursor(self._conn.cursor(prepared=prepared, **kwargs), self._log, prepared)

    def rollback(self) -> Any:
        self._log.append(("rollback",))
        return self._conn.rollback()

    def close(self) -> Any:
        self._log.append(("close",))
        return self._conn.close()


def _real_db(log: list[tuple]) -> AdminDb:
    import mysql.connector

    env = os.environ
    config = AdminDbConfig(
        host=env["LVM_IT_DB_HOST"],
        port=int(env["LVM_IT_DB_PORT"]),
        user=env["LVM_IT_DB_USER"],
        database=env["LVM_IT_DB_NAME"],
        password_file="/unused",
        password=env["LVM_IT_DB_PASSWORD"],
    )

    def connect(**kwargs: Any) -> RecordingConnection:
        log.append(("connect",))
        return RecordingConnection(mysql.connector.connect(**kwargs), log)

    return AdminDb(config, connect=connect)


def _run_real(run: Callable[[Any, AdminDb], tuple[Any, Any]], arguments: Any) -> tuple[Any, Any, list[tuple]]:
    log: list[tuple] = []
    accepted, outcome = run(arguments, _real_db(log))
    return accepted, outcome, log


# Feature: log-viewer-mcp, Property 4: SQL 只有固定语句
@pytest.mark.skipif(not _IT, reason="只在集成测试容器中运行（未设置 LVM_IT=1）")
@settings(deadline=None)
@given(arguments=SEARCH_ARGUMENTS)
def test_real_mysql_search_logs_sends_only_fixed_sql(arguments: dict[str, Any]) -> None:
    """**Validates: Requirements 7.1, 7.2, 7.6**"""
    svr_ids, outcome, log = _run_real(run_search, arguments)
    if svr_ids is None:
        assert log == []
        return
    assert not isinstance(outcome, LogViewerError), outcome
    assert check_driver_log(log) == list(svr_ids)
    assert set(outcome) == set(svr_ids)


# Feature: log-viewer-mcp, Property 4: SQL 只有固定语句
@pytest.mark.skipif(not _IT, reason="只在集成测试容器中运行（未设置 LVM_IT=1）")
@settings(deadline=None)
@given(arguments=LIST_ARGUMENTS)
def test_real_mysql_list_servers_sends_only_fixed_sql(arguments: Any) -> None:
    """**Validates: Requirements 7.1, 7.2, 7.6**"""
    accepted, outcome, log = _run_real(run_list, arguments)
    if not accepted:
        assert log == []
        return
    assert isinstance(outcome, list), outcome
    assert check_driver_log(log) == []
    assert [e[1] for e in log if e[0] == "execute"] == [SQL_LIST_SERVERS]
