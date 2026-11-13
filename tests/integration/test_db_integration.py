"""server/db.py 对真实 MySQL 8.4 的集成测试（compose 中的 mysql 服务，测试数据见 docker/mysql/init）。"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest

from log_viewer_mcp.common.errors import ErrorCode, LogViewerError
from log_viewer_mcp.server.config import AdminDbConfig, ConfigError
from log_viewer_mcp.server.db import SQL_START_READ_ONLY, AdminDb, ServerLookup

pytestmark = pytest.mark.skipif(os.environ.get("LVM_IT") != "1", reason="只在集成测试容器中运行")


def _config(password: str | None = None) -> AdminDbConfig:
    env = os.environ
    return AdminDbConfig(
        host=env.get("LVM_IT_DB_HOST", ""),
        port=int(env.get("LVM_IT_DB_PORT", "3306")),
        user=env.get("LVM_IT_DB_USER", ""),
        database=env.get("LVM_IT_DB_NAME", ""),
        password_file="/unused",
        password=password if password is not None else env.get("LVM_IT_DB_PASSWORD", ""),
    )


def test_lookup_against_test_data() -> None:
    ids = [1001, 1002, 1003, 1004, 2001, 4294967295, 9999] + list(range(1005, 1014))
    result = asyncio.run(AdminDb(_config()).lookup_servers(ids))
    assert result[1001] == ServerLookup(1001, "172.30.77.11", None)
    assert result[1002] == ServerLookup(1002, "172.30.77.12", None)
    assert result[1003] == ServerLookup(1003, "172.30.77.13", None)
    assert result[1004] == ServerLookup(1004, "172.30.77.99", None)
    assert result[2001] == ServerLookup(2001, "172.30.77.11", None)
    assert result[4294967295] == ServerLookup(4294967295, "172.30.77.11", None)
    assert result[9999] == ServerLookup(9999, None, ErrorCode.SERVER_NOT_FOUND)
    for svr_id in range(1005, 1014):
        assert result[svr_id] == ServerLookup(svr_id, None, ErrorCode.INVALID_SERVER_ADDRESS), svr_id


def test_list_servers_returns_all_rows() -> None:
    servers = asyncio.run(AdminDb(_config()).list_servers())
    ids = [s["svr_id"] for s in servers]
    assert ids == sorted(ids) and {1001, 2001, 4294967295, 1012} <= set(ids)
    by_id = {s["svr_id"]: s for s in servers}
    assert by_id[2001] == {"svr_id": 2001, "svr_name": "T1-测试服", "zone": "test", "ip_inner": "172.30.77.11", "is_test": 1}
    assert by_id[1012]["ip_inner"] is None


def test_transaction_is_read_only() -> None:
    """START TRANSACTION READ ONLY 之后，同一连接上的写语句被服务端拒绝（错误 1792）。"""
    import mysql.connector

    write_errors: list[int] = []

    class Probe:
        def __init__(self, conn: Any) -> None:
            self._conn = conn

        def __getattr__(self, name: str) -> Any:
            return getattr(self._conn, name)

        def cmd_query(self, sql: str) -> Any:
            res = self._conn.cmd_query(sql)
            if sql == SQL_START_READ_ONLY:
                cur = self._conn.cursor()
                try:
                    cur.execute("UPDATE tbl_webserver SET svr_name = svr_name WHERE svr_id = 1001")
                except mysql.connector.Error as e:
                    write_errors.append(e.errno)
                finally:
                    cur.close()
            return res

    def connect(**kwargs: Any) -> Probe:
        return Probe(mysql.connector.connect(**kwargs))

    result = asyncio.run(AdminDb(_config(), connect=connect).lookup_servers([1001]))
    assert result[1001].ip_inner == "172.30.77.11"
    assert write_errors == [1792]  # ER_CANT_EXECUTE_IN_READ_ONLY_TRANSACTION


def test_wrong_password_maps_to_db_unavailable_and_config_error() -> None:
    db = AdminDb(_config(password="wrong-password-xyz"))
    with pytest.raises(LogViewerError) as exc:
        asyncio.run(db.lookup_servers([1001]))
    assert exc.value.code is ErrorCode.DB_UNAVAILABLE
    with pytest.raises(ConfigError) as cexc:
        db.check_sync()
    assert "wrong-password-xyz" not in str(cexc.value)


def test_check_sync_passes() -> None:
    AdminDb(_config()).check_sync()
