"""server/db.py 的单元测试：在驱动 API 层用记录调用的替身代替 mysql.connector。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from log_viewer_mcp.common.errors import ErrorCode, LogViewerError
from log_viewer_mcp.server.config import AdminDbConfig, ConfigError
from log_viewer_mcp.server.db import (
    ALLOWED_SQL,
    SQL_LIST_SERVERS,
    SQL_LOOKUP_SERVER,
    SQL_START_READ_ONLY,
    AdminDb,
    ServerLookup,
    is_valid_ipv4,
)

PASSWORD = "s3cret-pw"
DB_CONFIG = AdminDbConfig("10.0.0.2", 3306, "logview", "admin", "/etc/x/db_password", PASSWORD)

TABLE = [
    (1001, "S1", "eu", "10.0.1.11", 0),
    (1002, "S2", "eu", "10.0.1.256", 0),
    (1003, "S3", "eu", None, 0),
    (2001, "T1", "test", "10.0.1.12", 1),
]


class DriverError(Exception):
    pass


class FakeCursor:
    def __init__(self, conn: FakeConnection) -> None:
        self.conn = conn
        self._rows: list[Any] = []

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.conn.log.append(("execute", sql, tuple(params)))
        if self.conn.fail_on == "execute":
            raise DriverError("raw driver text 10.0.0.2 " + PASSWORD)
        if sql == SQL_LOOKUP_SERVER:
            self._rows = [(r[0], r[3]) for r in self.conn.table if r[0] == params[0]]
        elif sql == SQL_LIST_SERVERS:
            self._rows = sorted(self.conn.table)
        else:
            raise DriverError("unexpected sql")

    def fetchall(self) -> list[Any]:
        rows, self._rows = self._rows, []
        return rows

    def close(self) -> None:
        self.conn.log.append(("cursor.close",))


class FakeConnection:
    def __init__(self, table: list[Any], fail_on: str | None, log: list) -> None:
        self.table = table
        self.fail_on = fail_on
        self.log = log

    def cmd_query(self, sql: str) -> None:
        self.log.append(("cmd_query", sql))
        if self.fail_on == "start":
            raise DriverError("cannot start read only")

    def cursor(self, prepared: bool = False) -> FakeCursor:
        self.log.append(("cursor", prepared))
        return FakeCursor(self)

    def rollback(self) -> None:
        self.log.append(("rollback",))

    def close(self) -> None:
        self.log.append(("close",))


class FakeDriver:
    def __init__(self, table: list[Any] = TABLE, fail_on: str | None = None) -> None:
        self.table = table
        self.fail_on = fail_on
        self.log: list[tuple] = []
        self.connect_kwargs: list[dict] = []

    def connect(self, **kwargs: Any) -> FakeConnection:
        self.connect_kwargs.append(kwargs)
        self.log.append(("connect",))
        if self.fail_on == "connect":
            raise DriverError("Access denied for user using " + PASSWORD)
        return FakeConnection(self.table, self.fail_on, self.log)


def make_db(driver: FakeDriver) -> AdminDb:
    return AdminDb(DB_CONFIG, connect=driver.connect)


def test_lookup_returns_ip_not_found_and_invalid_address() -> None:
    driver = FakeDriver()
    result = asyncio.run(make_db(driver).lookup_servers([1001, 1002, 1003, 9999, 1001]))
    assert result == {
        1001: ServerLookup(1001, "10.0.1.11", None),
        1002: ServerLookup(1002, None, ErrorCode.INVALID_SERVER_ADDRESS),
        1003: ServerLookup(1003, None, ErrorCode.INVALID_SERVER_ADDRESS),
        9999: ServerLookup(9999, None, ErrorCode.SERVER_NOT_FOUND),
    }
    # 一个连接、一个只读事务、预处理游标，svr_id 只作绑定参数，读完回滚并关闭。
    assert driver.log == [
        ("connect",),
        ("cmd_query", SQL_START_READ_ONLY),
        ("cursor", True),
        ("execute", SQL_LOOKUP_SERVER, (1001,)),
        ("execute", SQL_LOOKUP_SERVER, (1002,)),
        ("execute", SQL_LOOKUP_SERVER, (1003,)),
        ("execute", SQL_LOOKUP_SERVER, (9999,)),
        ("cursor.close",),
        ("rollback",),
        ("close",),
    ]
    kwargs = driver.connect_kwargs[0]
    assert kwargs["host"] == "10.0.0.2" and kwargs["database"] == "admin"
    assert kwargs["allow_local_infile"] is False


def test_list_servers_returns_all_rows_sorted_including_test_servers() -> None:
    driver = FakeDriver()
    servers = asyncio.run(make_db(driver).list_servers())
    assert [s["svr_id"] for s in servers] == [1001, 1002, 1003, 2001]
    assert servers[3] == {"svr_id": 2001, "svr_name": "T1", "zone": "test", "ip_inner": "10.0.1.12", "is_test": 1}
    assert servers[2]["ip_inner"] is None
    assert ("execute", SQL_LIST_SERVERS, ()) in driver.log
    assert driver.log[1] == ("cmd_query", SQL_START_READ_ONLY)
    assert driver.log[-2:] == [("rollback",), ("close",)]


@pytest.mark.parametrize("fail_on", ["connect", "start", "execute"])
def test_db_failures_map_to_db_unavailable_without_raw_text(fail_on: str, caplog: pytest.LogCaptureFixture) -> None:
    for call in ("lookup", "list"):
        driver = FakeDriver(fail_on=fail_on)
        db = make_db(driver)
        with pytest.raises(LogViewerError) as exc:
            if call == "lookup":
                asyncio.run(db.lookup_servers([1001]))
            else:
                asyncio.run(db.list_servers())
        assert exc.value.code is ErrorCode.DB_UNAVAILABLE
        assert exc.value.__cause__ is None
        assert "raw driver text" not in str(exc.value) and PASSWORD not in str(exc.value)
        if fail_on != "connect":
            assert driver.log[-1] == ("close",)
    assert PASSWORD not in caplog.text


def test_start_transaction_failure_runs_no_query() -> None:
    driver = FakeDriver(fail_on="start")
    with pytest.raises(LogViewerError):
        asyncio.run(make_db(driver).lookup_servers([1001]))
    assert driver.log == [("connect",), ("cmd_query", SQL_START_READ_ONLY), ("close",)]


def test_query_failure_still_rolls_back_and_closes() -> None:
    driver = FakeDriver(fail_on="execute")
    with pytest.raises(LogViewerError):
        asyncio.run(make_db(driver).lookup_servers([1001]))
    assert driver.log[-3:] == [("cursor.close",), ("rollback",), ("close",)]


@pytest.mark.parametrize("bad", [0, -1, 4294967296, True, 1.0, "1001", "1; rm -rf /", "1 OR 1=1", None])
def test_lookup_rejects_invalid_svr_id_before_connecting(bad: Any) -> None:
    driver = FakeDriver()
    with pytest.raises(LogViewerError) as exc:
        asyncio.run(make_db(driver).lookup_servers([1001, bad]))
    assert exc.value.code is ErrorCode.INVALID_ARGUMENT
    assert driver.log == []


def test_lookup_with_no_ids_does_not_connect() -> None:
    driver = FakeDriver()
    assert asyncio.run(make_db(driver).lookup_servers([])) == {}
    assert driver.log == []


def test_check_sync_raises_config_error_with_masked_password() -> None:
    driver = FakeDriver(fail_on="connect")
    with pytest.raises(ConfigError) as exc:
        make_db(driver).check_sync()
    assert "10.0.0.2:3306/admin" in str(exc.value)
    assert PASSWORD not in str(exc.value)


def test_check_sync_passes_and_uses_only_allowed_sql() -> None:
    driver = FakeDriver()
    make_db(driver).check_sync()
    sql = [e[1] for e in driver.log if e[0] in ("cmd_query", "execute")]
    assert sql and set(sql) <= ALLOWED_SQL


@pytest.mark.parametrize("ip", ["10.0.1.11", "0.0.0.0", "255.255.255.255", "172.30.77.11", "1.2.3.4"])
def test_valid_ipv4(ip: str) -> None:
    assert is_valid_ipv4(ip)


@pytest.mark.parametrize(
    "ip",
    [
        "10.0.1.256",
        "172.030.77.11",
        "01.2.3.4",
        "",
        "172.30.77.11; rm -rf /",
        "172.30.77.11\n",
        " 172.30.77.11",
        "gs",
        "::1",
        "1.2.3",
        "1.2.3.4.5",
        "١٧٢.30.77.11",
        None,
        b"10.0.1.11",
        167772427,
    ],
)
def test_invalid_ipv4(ip: Any) -> None:
    assert not is_valid_ipv4(ip)
