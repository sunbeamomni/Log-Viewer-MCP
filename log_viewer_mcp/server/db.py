"""管理库 Admin_DB 的只读访问（需求 3.1、3.2、7.1–7.5、10.2；design.md「管理库（db.py）」）。

- 只发送模块中的常量语句：两条查询（SQL_LOOKUP_SERVER、SQL_LIST_SERVERS）、开启只读事务的
  SQL_START_READ_ONLY，以及结束事务的 rollback（驱动发送 ROLLBACK）。任何 SQL 文本都不由
  字符串拼接生成，svr_id 只作为服务端预处理语句（``cursor(prepared=True)``）的绑定参数。
- 每次调用新建一个连接：先开启只读事务，失败即 DB_UNAVAILABLE、不执行查询；读完 rollback 并关闭。
- 驱动是同步的，放在 ``asyncio.to_thread`` 中执行。
- 原始数据库错误只写运行日志；对外只有错误码与中文说明（10.3）。

只读事务用 ``cmd_query(SQL_START_READ_ONLY)`` 一条语句开启，而不是 ``start_transaction(readonly=True)``：
锁定版本（mysql-connector-python 26.10.0）的 start_transaction 会先发 SET TRANSACTION READ ONLY
再发 START TRANSACTION，两条语句；直接发 START TRANSACTION READ ONLY 与需求 7.3 的文字一致，
允许的语句集合也更小。

驱动访问只经过 ``connect`` 函数（默认为 mysql.connector.connect），用到的接口只有：
``connect(**kwargs)``、``conn.cmd_query(sql)``、``conn.cursor(prepared=True)``、
``cursor.execute(sql, params)``、``cursor.fetchall()``、``cursor.close()``、``conn.rollback()``、
``conn.close()``。测试可以注入记录语句与绑定参数的替身（任务 4.2）。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from log_viewer_mcp.common.errors import ErrorCode, LogViewerError
from log_viewer_mcp.common.formats import is_valid_svr_id
from log_viewer_mcp.server.config import AdminDbConfig, Config, ConfigError

__all__ = [
    "ALLOWED_SQL",
    "AdminDb",
    "SQL_LIST_SERVERS",
    "SQL_LOOKUP_SERVER",
    "SQL_START_READ_ONLY",
    "ServerLookup",
    "check_admin_db",
    "is_valid_ipv4",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 固定语句（7.1）。"?" 是预处理语句的占位符，驱动原样交给服务端 COM_STMT_PREPARE。
# ---------------------------------------------------------------------------

SQL_LOOKUP_SERVER = "SELECT svr_id, ip_inner FROM tbl_webserver WHERE svr_id = ?"
SQL_LIST_SERVERS = "SELECT svr_id, svr_name, zone, ip_inner, is_test FROM tbl_webserver ORDER BY svr_id"
SQL_START_READ_ONLY = "START TRANSACTION READ ONLY"

# 本模块主动发送的全部 SQL 文本；结束事务用 conn.rollback()（ROLLBACK）。
ALLOWED_SQL: frozenset[str] = frozenset({SQL_LOOKUP_SERVER, SQL_LIST_SERVERS, SQL_START_READ_ONLY})

# 建连、读、写的时限（秒）：线程中的同步调用无法取消，靠时限保证它们会结束。
DEFAULT_CONNECT_TIMEOUT_S = 10
DEFAULT_IO_TIMEOUT_S = 10

# 严格的点分十进制 IPv4：每段 0–255，无前导零，只用 ASCII 数字（7.5）。
_OCTET = r"(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])"
_IPV4_PATTERN = re.compile(rf"{_OCTET}(?:\.{_OCTET}){{3}}")


def is_valid_ipv4(value: Any) -> bool:
    """ip_inner 是否为合法的点分十进制 IPv4；非 str（含 NULL、bytes）一律不合法。"""
    return type(value) is str and _IPV4_PATTERN.fullmatch(value) is not None


@dataclass(frozen=True)
class ServerLookup:
    """一个 svr_id 的查询结果：error 为 None 时 ip_inner 是合法的 IPv4，否则为 None。"""

    svr_id: int
    ip_inner: str | None
    error: ErrorCode | None


ConnectFn = Callable[..., Any]


def _default_connect(**kwargs: Any) -> Any:
    import mysql.connector  # 延迟导入：只在真正连接时需要驱动

    return mysql.connector.connect(**kwargs)


class _DbFailure(Exception):
    """内部使用：数据库访问失败，详细原因已写入运行日志。"""


class AdminDb:
    """Admin_DB 的只读访问；每次调用新建连接。"""

    def __init__(
        self,
        config: AdminDbConfig,
        *,
        connect: ConnectFn | None = None,
        connect_timeout_s: int = DEFAULT_CONNECT_TIMEOUT_S,
        io_timeout_s: int = DEFAULT_IO_TIMEOUT_S,
    ) -> None:
        self._config = config
        self._connect_fn = connect if connect is not None else _default_connect
        self._connect_timeout_s = connect_timeout_s
        self._io_timeout_s = io_timeout_s

    # ---- 对外接口 -----------------------------------------------------------

    async def lookup_servers(self, svr_ids: Sequence[int]) -> dict[int, ServerLookup]:
        """按 svr_id 查 ip_inner（重复值只查一次，保持顺序）。

        单个 svr_id 的问题（不存在、ip_inner 不合法）记在对应的 ServerLookup 中；
        连接、只读事务或查询失败时整次抛出 LogViewerError(DB_UNAVAILABLE)。
        """
        # 调用方（validate.py）已校验；这里防御性地再查一次，不合法的值不进入数据库。
        # 先逐个检查原始值再去重：True == 1、1001.0 == 1001，先去重会把它们并入合法值而漏查，
        # 不可哈希的值（list、dict）也会让 dict.fromkeys 抛出 TypeError。
        raw = list(svr_ids)
        for svr_id in raw:
            if not is_valid_svr_id(svr_id):
                raise LogViewerError(ErrorCode.INVALID_ARGUMENT)
        ids = list(dict.fromkeys(raw))
        return await asyncio.to_thread(self._lookup_sync, ids)

    async def list_servers(self) -> list[dict[str, Any]]:
        """tbl_webserver 中的全部服务器（按 svr_id 排序，不过滤测试服）；失败时抛出 DB_UNAVAILABLE，不返回部分列表。"""
        return await asyncio.to_thread(self._list_sync)

    def check_sync(self) -> None:
        """启动时的连通检查：建连、开启只读事务并执行列表查询；失败时抛出 ConfigError（10.2）。"""
        try:
            self._run(self._list_rows)
        except _DbFailure as e:
            cause = e.__cause__
            detail = f"{type(cause).__name__}: {cause}" if cause is not None else "未知错误"
            detail = detail.replace(self._config.password, "***") if self._config.password else detail
            c = self._config
            raise ConfigError(f"无法访问管理库 {c.host}:{c.port}/{c.database}：{detail}") from None

    # ---- 同步实现（在线程中运行）---------------------------------------------

    def _lookup_sync(self, ids: list[int]) -> dict[int, ServerLookup]:
        if not ids:
            return {}
        try:
            return self._run(lambda conn: self._lookup_rows(conn, ids))
        except _DbFailure:
            raise LogViewerError(ErrorCode.DB_UNAVAILABLE) from None

    def _list_sync(self) -> list[dict[str, Any]]:
        try:
            rows = self._run(self._list_rows)
        except _DbFailure:
            raise LogViewerError(ErrorCode.DB_UNAVAILABLE) from None
        try:
            return [self._server_row(row) for row in rows]
        except (TypeError, ValueError) as e:
            logger.error("管理库 tbl_webserver 返回了意外的行格式：%s", type(e).__name__)
            raise LogViewerError(ErrorCode.DB_UNAVAILABLE) from None

    def _run(self, body: Callable[[Any], Any]) -> Any:
        """建连 → START TRANSACTION READ ONLY → body(conn) → rollback → close。

        任一步失败都写运行日志并抛出 _DbFailure（__cause__ 为原始异常）；只读事务没开启时不执行 body。
        """
        c = self._config
        try:
            conn = self._connect_fn(
                host=c.host,
                port=c.port,
                user=c.user,
                password=c.password,
                database=c.database,
                charset="utf8mb4",
                autocommit=False,
                allow_local_infile=False,
                connection_timeout=self._connect_timeout_s,
                read_timeout=self._io_timeout_s,
                write_timeout=self._io_timeout_s,
            )
        except Exception as e:
            self._log_failure("连接管理库失败", e)
            raise _DbFailure() from e

        in_transaction = False
        try:
            try:
                conn.cmd_query(SQL_START_READ_ONLY)
            except Exception as e:
                self._log_failure("开启只读事务失败", e)
                raise _DbFailure() from e
            in_transaction = True
            try:
                return body(conn)
            except Exception as e:
                self._log_failure("查询管理库失败", e)
                raise _DbFailure() from e
        finally:
            if in_transaction:
                try:
                    conn.rollback()
                except Exception as e:  # 数据已读完，只读事务回滚失败不影响结果
                    self._log_failure("结束只读事务失败", e)
            try:
                conn.close()
            except Exception as e:
                self._log_failure("关闭管理库连接失败", e)

    @staticmethod
    def _lookup_rows(conn: Any, ids: list[int]) -> dict[int, ServerLookup]:
        result: dict[int, ServerLookup] = {}
        cursor = conn.cursor(prepared=True)
        try:
            for svr_id in ids:
                cursor.execute(SQL_LOOKUP_SERVER, (svr_id,))
                rows = cursor.fetchall()
                result[svr_id] = _lookup_from_rows(svr_id, rows)
        finally:
            cursor.close()
        return result

    @staticmethod
    def _list_rows(conn: Any) -> list[Any]:
        cursor = conn.cursor(prepared=True)
        try:
            cursor.execute(SQL_LIST_SERVERS, ())
            return list(cursor.fetchall())
        finally:
            cursor.close()

    @staticmethod
    def _server_row(row: Sequence[Any]) -> dict[str, Any]:
        svr_id, svr_name, zone, ip_inner, is_test = row
        return {
            "svr_id": _as_int(svr_id),
            "svr_name": _as_text(svr_name),
            "zone": _as_text(zone),
            "ip_inner": _as_text(ip_inner),
            "is_test": _as_int(is_test),
        }

    def _log_failure(self, what: str, e: BaseException) -> None:
        # 运行日志：异常类型与驱动的错误说明（不含凭据；防御性地掩去密码）。
        text = str(e)
        if self._config.password:
            text = text.replace(self._config.password, "***")
        logger.error("%s：%s: %s", what, type(e).__name__, text)


def _lookup_from_rows(svr_id: int, rows: Sequence[Any]) -> ServerLookup:
    if not rows:
        return ServerLookup(svr_id, None, ErrorCode.SERVER_NOT_FOUND)  # 7.4
    ip_inner = rows[0][1]
    if not is_valid_ipv4(ip_inner):
        logger.warning("svr_id %d 的 ip_inner 不是合法的点分十进制 IPv4：%r", svr_id, ip_inner)
        return ServerLookup(svr_id, None, ErrorCode.INVALID_SERVER_ADDRESS)  # 7.5
    return ServerLookup(svr_id, ip_inner, None)


def _as_int(value: Any) -> int:
    if type(value) is int:
        return value
    if type(value) is bool:
        return int(value)
    raise TypeError("期望整数")


def _as_text(value: Any) -> str | None:
    if value is None or type(value) is str:
        return value
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    raise TypeError("期望字符串")


def check_admin_db(config: Config) -> None:
    """``load_config_or_exit(extra_checks=[check_admin_db])`` 用的启动检查（10.2）。"""
    AdminDb(config.admin_db).check_sync()
