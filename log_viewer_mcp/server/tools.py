"""search_logs 与 list_servers 的编排（需求 1.5–1.7、3.1、3.2、7.4、7.5、10.3；design.md「工具接口」）。

本模块接收已通过校验的参数（validate.py 的 SearchParams），不做参数校验，也不写审计：
call_tool（任务 8.4）先校验原始参数，再调用这里的函数，最后用返回的审计信息写审计。

search_logs 的流程：

1. Time_Range（validate.compute_time_range，以当前时刻为参数）；不符返回 INVALID_ARGUMENT。
2. 全局并发名额（limits.ConcurrencyLimiter，9.2）：已满整次返回 BUSY，不排队、不访问管理库；
   名额一直占到本次调用结束（含被取消）。list_servers 不占名额。
3. 管理库：``lookup_servers`` 在一个只读事务中查出全部 svr_id 的 ip_inner；失败整次返回 DB_UNAVAILABLE，
   到达截止时刻仍未返回则整次返回 TIMEOUT（还没有任何 svr_id 开始读取）。
4. 新建 Result_Handle（失败整次返回 RESULT_STORE_FAILED）。
5. 各 svr_id 在 asyncio.TaskGroup 中并发执行：占单服名额 → ① 列目录与探测 → select_files → ② 查询并写结果文件。
   单服错误只影响该 svr_id（1.7）：SERVER_NOT_FOUND、INVALID_SERVER_ADDRESS（都不发起 SSH，7.4、7.5）、
   BUSY（该 ip_inner 的单服名额已满，不发起 SSH，9.2）、
   SSH_FAILED、HOST_KEY_MISMATCH、LOG_DIR_UNAVAILABLE、REMOTE_TOOL_UNAVAILABLE、TIMEOUT 与
   未预期的异常（INTERNAL_ERROR）记为该服 failed，删除该服的结果文件（摘要中 file 为 null）。
   写结果文件失败（RESULT_STORE_FAILED）是整次错误：results.py 已删除整个 handle 目录，
   TaskGroup 随即取消其余 svr_id（它们的远端进程由 run_query 终止）。
6. 按「工具接口」汇总摘要。

单服名额按 ip_inner 计（同一次调用中 ip_inner 相同的 svr_id 各占一个，按 svr_ids 顺序先到先得），
从建连之前占到连接关闭为止。

截止时刻 deadline 是事件循环时间（``loop.time()``）。调用方（任务 8.4 的 call_tool）传入调用开始时刻 +
Query_Timeout；不传时取 run() 开始时刻 + Query_Timeout（9.3）。管理库查询、建连与 ① 都在
``asyncio.timeout_at(deadline)`` 中执行：开始读取之前到达截止时刻的 svr_id 返回 TIMEOUT；② 由 run_query
按同一截止时刻收尾，已开始读取的保留完整行并标 truncated 为 timeout，尚未开始读取的返回 TIMEOUT。
timeout 的秒数按剩余时长计算（remote.remote_seconds）。到达截止时刻后只剩收尾（kill 限时
remote.KILL_TIMEOUT_S，关闭连接限时 remote.CLOSE_TIMEOUT_S），所以整次调用最迟在截止时刻后约 10 秒内返回。
管理库查询在线程中执行、无法取消，超时后由驱动自身的时限结束（db.py）。

摘要字段的取法：

- servers 按去重后的 svr_ids 顺序排列；status 为 ok 或 failed，failed 时 error 为 {code, message}
  （message 是 errors.py 中写定的中文说明），其余字段取空值（file 为 null，bytes、lines 为 0，
  列表为空，truncated 为 none，available_range 为 null）。
- files_read：② 中读完的文件与超时时读到一半的文件；files_skipped：② 中读取或解压失败、因超时未读的文件，
  再接上因扫描上限未选取的文件（按时间先后）。
- truncated：② 因截止时刻停止读取时为 timeout；否则因 Max_Scan_Bytes 少选了文件时为 scan_limit；
  否则为 none。两者都发生时取 timeout（已选的文件也没读完）。
- available_range：只在 Time_Range 内没有该类型文件时给出 {earliest, latest, earliest_server, latest_server}。
  该类型只有 Current_Log_File 时最早时间不限，earliest 与 earliest_server 为 null；
  该类型一个文件都没有时 available_range 也为 null。
- 没有选中任何文件时不执行 ②，结果文件为空文件（1.5）。

审计信息（ToolResult.targets，任务 8.4 写入审计）：每个查过管理库的 svr_id 一项，含 ip_inner（查不到时为 null）、
实际交给游戏服的脚本（① 与 ②，按执行顺序；没有建立连接时为空）与状态：ok、该服的错误码，或 cancelled
（整次调用被取消或因其他 svr_id 的 RESULT_STORE_FAILED 而中止）。ToolResult.status 为 ok（返回了摘要）
或整次错误的错误码。调用被取消（断开或停机，任务 8.7）时 ``SearchLogsRun.run`` 删除本次结果后抛出
CancelledError，审计信息仍可从 ``SearchLogsRun.audit_targets`` 取得。

对外只有错误码与写定的中文说明；原始异常只写运行日志（10.3）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from log_viewer_mcp.common.errors import ErrorCode, LogViewerError, error_dict
from log_viewer_mcp.server import remote
from log_viewer_mcp.server.audit import AuditTarget
from log_viewer_mcp.server.config import Config
from log_viewer_mcp.server.db import AdminDb, ServerLookup
from log_viewer_mcp.server.limits import ConcurrencyLimiter
from log_viewer_mcp.server.remote import (
    Listing,
    QueryOutcome,
    QueryScript,
    ResultSink,
    SshConnector,
    build_listing_script,
    build_query_script,
    remote_seconds,
)
from log_viewer_mcp.server.results import ResultStore, ResultStoreError
from log_viewer_mcp.server.select import AvailableRange, Selection, select_files
from log_viewer_mcp.server.validate import SearchParams, TimeRange, compute_time_range, format_server_time

__all__ = [
    "STATUS_FAILED",
    "STATUS_OK",
    "TARGET_CANCELLED",
    "TRUNCATED_NONE",
    "TRUNCATED_SCAN_LIMIT",
    "TRUNCATED_TIMEOUT",
    "SearchLogsRun",
    "ToolResult",
    "ToolServices",
    "list_servers",
    "search_logs",
]

logger = logging.getLogger(__name__)

STATUS_OK = "ok"
STATUS_FAILED = "failed"
TARGET_CANCELLED = "cancelled"

TRUNCATED_NONE = "none"
TRUNCATED_TIMEOUT = "timeout"
TRUNCATED_SCAN_LIMIT = "scan_limit"


# ---------------------------------------------------------------------------
# 依赖注入
# ---------------------------------------------------------------------------


class AdminDbLike(Protocol):
    async def lookup_servers(self, svr_ids: Sequence[int]) -> Mapping[int, ServerLookup]: ...

    async def list_servers(self) -> list[dict[str, Any]]: ...


class ConnectionLike(Protocol):
    host: str

    async def close(self) -> None: ...


class ConnectorLike(Protocol):
    async def connect(self, ip_inner: str) -> Any: ...


ListingFn = Callable[..., Awaitable[Listing]]  # (conn, script, *, seconds) → Listing
QueryFn = Callable[..., Awaitable[QueryOutcome]]  # (conn, query, sink, *, deadline) → QueryOutcome


@dataclass
class ToolServices:
    """编排用到的外部依赖；测试可替换其中任意一项。

    - db：Admin_DB（lookup_servers、list_servers）。
    - connector：SSH 建连（connect(ip_inner) → 连接，连接须有 host 与 async close()）。
    - store：Result_Store（create_handle、open_writer、discard）；启动检查（prepare）由服务入口负责。
    - clock：Unix 时间（秒），用于 Time_Range 与 Current_Log_File 的终点。
    - run_listing、run_query：远端执行 ① 与 ②（默认为 remote.py 中的实现）。
    - limiter：全局与单服的并发名额（未传入时按 config.limits 新建）；各次调用共用同一个 ToolServices。
    """

    config: Config
    db: AdminDbLike
    connector: ConnectorLike
    store: ResultStore
    clock: Callable[[], float] = time.time
    run_listing: ListingFn = remote.run_listing
    run_query: QueryFn = remote.run_query
    limiter: ConcurrencyLimiter = None  # type: ignore[assignment]  # __post_init__ 中按配置新建

    def __post_init__(self) -> None:
        if self.limiter is None:
            self.limiter = ConcurrencyLimiter.from_limits(self.config.limits)

    @classmethod
    def from_config(cls, config: Config, *, store: ResultStore | None = None, **kwargs: Any) -> ToolServices:
        """按配置构造：AdminDb、SshConnector（导入私钥与 known_hosts）与 ResultStore（未传入时新建）。"""
        return cls(
            config=config,
            db=AdminDb(config.admin_db),
            connector=SshConnector(config.ssh),
            store=store if store is not None else ResultStore.from_config(config.results),
            **kwargs,
        )


# ---------------------------------------------------------------------------
# 结果
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolResult:
    """一次工具调用的结果。

    - payload：交给 Agent 的 JSON 对象：摘要（search_logs）、{"servers": [...]}（list_servers），
      或整次失败时的 {"error": {"code", "message"}}。
    - is_error：整次失败（工具结果的 isError）。
    - status：审计记录的状态，ok 或整次错误的错误码。
    - result_handle：本次新建的 Result_Handle（没有新建时为 None）；整次失败时其目录已删除。
    - targets：审计记录的 targets。
    """

    payload: dict[str, Any]
    is_error: bool = False
    status: str = STATUS_OK
    result_handle: str | None = None
    targets: tuple[AuditTarget, ...] = ()

    @classmethod
    def failure(
        cls,
        error: LogViewerError,
        *,
        result_handle: str | None = None,
        targets: tuple[AuditTarget, ...] = (),
    ) -> ToolResult:
        return cls(
            payload={"error": error.to_dict()},
            is_error=True,
            status=error.code.value,
            result_handle=result_handle,
            targets=targets,
        )


def _failed_server(svr_id: int, code: ErrorCode) -> dict[str, Any]:
    return {
        "svr_id": svr_id,
        "status": STATUS_FAILED,
        "error": error_dict(code),
        "file": None,
        "bytes": 0,
        "lines": 0,
        "files_read": [],
        "files_skipped": [],
        "truncated": TRUNCATED_NONE,
        "available_range": None,
    }


def _available_range(available: AvailableRange | None, config: Config) -> dict[str, Any] | None:
    if available is None:
        return None
    earliest = available.earliest
    return {
        "earliest": earliest,
        "latest": available.latest,
        "earliest_server": format_server_time(earliest, config.tz) if earliest is not None else None,
        "latest_server": format_server_time(available.latest, config.tz),
    }


def _truncated(selection: Selection, outcome: QueryOutcome | None) -> str:
    if outcome is not None and outcome.timed_out:
        return TRUNCATED_TIMEOUT
    if selection.scan_limited:
        return TRUNCATED_SCAN_LIMIT
    return TRUNCATED_NONE


def _is_store_failure(e: BaseException) -> bool:
    return isinstance(e, LogViewerError) and e.code is ErrorCode.RESULT_STORE_FAILED


# ---------------------------------------------------------------------------
# search_logs
# ---------------------------------------------------------------------------


@dataclass
class _ServerRun:
    """一个 svr_id 的进行状态；任务被取消时已记录的审计信息仍然保留。"""

    svr_id: int
    lookup: ServerLookup | None
    scripts: list[str] = field(default_factory=list)
    status: str | None = None  # None：还没有结束
    summary: dict[str, Any] | None = None

    @property
    def ip_inner(self) -> str | None:
        return self.lookup.ip_inner if self.lookup is not None else None

    def audit_target(self) -> AuditTarget:
        return AuditTarget(
            svr_id=self.svr_id,
            ip_inner=self.ip_inner,
            scripts=tuple(self.scripts),
            status=self.status if self.status is not None else TARGET_CANCELLED,
        )


class SearchLogsRun:
    """一次 search_logs 调用；``run()`` 只能调用一次。

    deadline 为截止时刻（事件循环时间）；None 时取 run() 开始时刻 + Query_Timeout。
    """

    def __init__(self, services: ToolServices, params: SearchParams, *, deadline: float | None = None) -> None:
        if params.log_type not in services.config.log_types:
            raise ValueError("log_type 未配置（参数应已通过校验）")
        self._services = services
        self.params = params
        self.deadline = deadline
        self.result_handle: str | None = None
        self.time_range: TimeRange | None = None
        self._now = 0
        self._runs: list[_ServerRun] = []
        self._started = False

    @property
    def audit_targets(self) -> tuple[AuditTarget, ...]:
        """审计记录的 targets：查过管理库的各 svr_id（调用被取消时，未结束的状态为 cancelled）。"""
        return tuple(r.audit_target() for r in self._runs)

    async def run(self) -> ToolResult:
        if self._started:
            raise RuntimeError("SearchLogsRun.run 只能调用一次")
        self._started = True
        loop = asyncio.get_running_loop()
        if self.deadline is None:
            self.deadline = loop.time() + self._services.config.limits.query_timeout_s
        try:
            return await self._run()
        except LogViewerError as e:
            # 整次错误：INVALID_ARGUMENT（Time_Range）、BUSY（全局名额）、DB_UNAVAILABLE、
            # TIMEOUT（管理库查询到达截止时刻）、RESULT_STORE_FAILED
            logger.warning("search_logs 整次失败：%s", e.code.value)
            self._discard()
            return ToolResult.failure(e, result_handle=self.result_handle, targets=self.audit_targets)
        except asyncio.CancelledError:
            self._discard()
            raise
        except Exception:
            logger.exception("search_logs 出现未预期的异常")
            self._discard()
            return ToolResult.failure(
                LogViewerError(ErrorCode.INTERNAL_ERROR), result_handle=self.result_handle, targets=self.audit_targets,
            )

    def _discard(self) -> None:
        if self.result_handle is not None:
            self._services.store.discard(self.result_handle)

    async def _run(self) -> ToolResult:
        services = self._services
        config = services.config
        self._now = int(services.clock())
        self.time_range = compute_time_range(self.params, self._now, config.limits)

        async with services.limiter.call_slot():  # 已满时抛出 BUSY（整次），不访问管理库
            lookups = await self._lookup_servers()
            self._runs = [_ServerRun(svr_id, lookups.get(svr_id)) for svr_id in self.params.svr_ids]

            self.result_handle = services.store.create_handle()
            try:
                async with asyncio.TaskGroup() as tg:
                    for run in self._runs:
                        tg.create_task(self._server_task(run))
            except BaseExceptionGroup as eg:
                _, rest = eg.split(_is_store_failure)
                if rest is not None:
                    raise rest
                raise ResultStoreError() from None

        summary = {
            "result_handle": self.result_handle,
            "time_range": self.time_range.as_summary(config.tz),
            "server_timezone": config.timezone_label,
            "servers": [r.summary for r in self._runs],
        }
        return ToolResult(
            payload=summary, status=STATUS_OK, result_handle=self.result_handle, targets=self.audit_targets,
        )

    async def _server_task(self, run: _ServerRun) -> None:
        """单个 svr_id：单服错误记在 run 中；RESULT_STORE_FAILED 与取消向上传递。"""
        try:
            run.summary = await self._query_server(run)
        except LogViewerError as e:
            run.status = e.code.value
            if _is_store_failure(e):
                raise
            logger.warning("svr_id %d 查询失败：%s", run.svr_id, e.code.value)
            run.summary = _failed_server(run.svr_id, e.code)
        except Exception:
            logger.exception("svr_id %d 查询时出现未预期的异常", run.svr_id)
            run.status = ErrorCode.INTERNAL_ERROR.value
            run.summary = _failed_server(run.svr_id, ErrorCode.INTERNAL_ERROR)
        else:
            run.status = STATUS_OK

    async def _lookup_servers(self) -> Mapping[int, ServerLookup]:
        """管理库查询，受截止时刻限制：到达截止时刻时整次返回 TIMEOUT（此时还没有 svr_id 开始读取）。"""
        assert self.deadline is not None
        cm = asyncio.timeout_at(self.deadline)
        try:
            async with cm:
                return await self._services.db.lookup_servers(self.params.svr_ids)
        except TimeoutError:
            if cm.expired():
                logger.warning("search_logs 的管理库查询到达截止时刻")
                raise LogViewerError(ErrorCode.TIMEOUT) from None
            raise

    def _seconds(self, loop: asyncio.AbstractEventLoop) -> int:
        assert self.deadline is not None
        return remote_seconds(self.deadline - loop.time())

    async def _query_server(self, run: _ServerRun) -> dict[str, Any]:
        lookup = run.lookup
        if lookup is None:
            logger.error("管理库查询结果中缺少 svr_id %d", run.svr_id)
            raise LogViewerError(ErrorCode.INTERNAL_ERROR)
        if lookup.error is not None:
            raise LogViewerError(lookup.error)  # SERVER_NOT_FOUND、INVALID_SERVER_ADDRESS：不发起 SSH
        assert lookup.ip_inner is not None and self.deadline is not None
        if asyncio.get_running_loop().time() >= self.deadline:
            raise LogViewerError(ErrorCode.TIMEOUT)
        with self._services.limiter.server_slot(lookup.ip_inner):  # 已满时抛出 BUSY（该 svr_id），不发起 SSH
            return await self._search_server(run, lookup.ip_inner)

    async def _search_server(self, run: _ServerRun, ip_inner: str) -> dict[str, Any]:
        """已占单服名额：建连、①、选取、② 与写结果；连接在返回或出错前关闭。"""
        assert self.deadline is not None and self.time_range is not None
        services = self._services
        config = services.config
        params = self.params
        log_type = config.log_types[params.log_type]
        loop = asyncio.get_running_loop()
        deadline = self.deadline

        conn: Any = None
        try:
            zip_dir = config.zip_dir_for(log_type)
            listing_script = build_listing_script(log_type.dir, zip_dir)
            cm = asyncio.timeout_at(deadline)
            try:
                async with cm:
                    conn = await services.connector.connect(ip_inner)
                    run.scripts.append(listing_script)
                    listing = await services.run_listing(conn, listing_script, seconds=self._seconds(loop))
            except TimeoutError:
                if cm.expired():
                    raise LogViewerError(ErrorCode.TIMEOUT) from None  # 开始读取之前到达截止时刻
                raise

            selection = select_files(
                listing.plain,
                listing.archives,
                start=self.time_range.start,
                end=self.time_range.end,
                now=self._now,
                max_scan_bytes=config.limits.max_scan_bytes,
                suffix=log_type.suffix,
                tz=config.tz,
            )

            assert self.result_handle is not None
            writer = services.store.open_writer(self.result_handle, run.svr_id, params.log_type)
            outcome: QueryOutcome | None = None
            try:
                if selection.files:
                    query: QueryScript = build_query_script(
                        selection.files,
                        plain_dir=log_type.dir,
                        zip_dir=zip_dir,
                        include_patterns=params.include_patterns,
                        exclude_patterns=params.exclude_patterns,
                        regex=params.regex,
                        ignore_case=params.ignore_case,
                        context_before=params.context_before,
                        context_after=params.context_after,
                        seconds=self._seconds(loop),
                        ionice=listing.ionice,
                    )
                    run.scripts.append(query.script)
                    sink: ResultSink = writer
                    outcome = await services.run_query(conn, query, sink, deadline=deadline)
                info = writer.close()
            except BaseException:
                writer.abort()
                raise
        finally:
            if conn is not None:
                await conn.close()

        files_read = list(outcome.files_read) if outcome is not None else []
        files_skipped = list(outcome.files_skipped) if outcome is not None else []
        files_skipped += selection.skipped
        return {
            "svr_id": run.svr_id,
            "status": STATUS_OK,
            "error": None,
            "file": info.file_name,
            "bytes": info.bytes,
            "lines": info.lines,
            "files_read": files_read,
            "files_skipped": files_skipped,
            "truncated": _truncated(selection, outcome),
            "available_range": _available_range(selection.available_range, config),
        }


async def search_logs(services: ToolServices, params: SearchParams, *, deadline: float | None = None) -> ToolResult:
    """执行一次 search_logs（参数已通过校验），返回摘要与审计信息。"""
    return await SearchLogsRun(services, params, deadline=deadline).run()


# ---------------------------------------------------------------------------
# list_servers
# ---------------------------------------------------------------------------


async def list_servers(services: ToolServices) -> ToolResult:
    """tbl_webserver 中的全部服务器（3.1）；读取失败返回 DB_UNAVAILABLE，不返回部分列表（3.2）。

    只读一次管理库、不登录游戏服，不占并发名额（limits.py）。
    """
    try:
        servers = await services.db.list_servers()
    except LogViewerError as e:
        logger.warning("list_servers 失败：%s", e.code.value)
        return ToolResult.failure(e)
    except Exception:
        logger.exception("list_servers 出现未预期的异常")
        return ToolResult.failure(LogViewerError(ErrorCode.INTERNAL_ERROR))
    return ToolResult(payload={"servers": list(servers)})
