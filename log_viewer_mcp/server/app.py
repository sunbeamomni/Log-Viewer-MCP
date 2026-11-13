"""Log_MCP_Server 的服务入口（design.md「服务入口（app.py）」与「参数校验（validate.py）」）。

本模块（任务 8.4）提供 FastMCP 子类 ``LogViewerMCP`` 与工厂 ``create_mcp``：

- 注册 search_logs 与 list_servers 两个工具：说明与参数 schema 取自 common/tool_docs.py（Local_MCP 与
  SKILL.md 用同一份文字），log_type 的 enum 与各上限取自配置。注册只用于 tools/list。
- 覆盖 ``call_tool``：FastMCP 的 pydantic 参数模型会丢掉未定义的参数并做类型转换，与 8.1 冲突，所以
  原始参数字典先交给 ``validate_arguments_async``，通过后直接调用 tools.py 的编排（不经 FastMCP 的
  参数模型与工具函数）。低层服务端以 ``validate_input=False`` 注册 call_tool，原始字典原样到达这里；
  返回 CallToolResult 时低层服务端原样转发。
- 每次调用结束（含被拒绝的调用、未知工具、BUSY、整次错误与被取消的调用）写一条审计：使用者与 token
  取自 context.py 的 contextvars（认证中间件在任务 8.6 设置），params 为原始参数字典（掩码由 audit.py
  处理），另有 duration_ms、result_handle 与 targets。审计写入失败时删除本次结果并返回 AUDIT_FAILED（6.5）。
  被取消时以状态 cancelled 写审计（屏蔽取消，写完再继续抛出 CancelledError）。
- 截止时刻为调用开始时的事件循环时间 + Query_Timeout，交给 search_logs（9.3）。
- 返回：JSON 摘要同时放在文本内容（ensure_ascii=False）与 structuredContent 中；整次失败时 isError 为真，
  内容为 ``{"error": {"code": …, "message": …}}``。对外只有错误码与写定的中文说明，未预期的异常只写
  运行日志（10.3）。

任务 8.6 加入 HTTP 层与启动流程：

- ``BearerAuthMiddleware``：纯 ASGI 认证中间件（不用 BaseHTTPMiddleware，以免影响流式响应与断开检测），
  包住整个 Starlette 应用，/mcp 与 /results/ 都要求有效 token（6.1）；认证通过后设置 context.py 的
  contextvars，SDK 在请求中新建的工具任务会复制它们。token 集合由 tokens.py 的 TokenSet 持有，tokens.json 变化后
  在下一个请求认证之前重新读取（6.6、6.7，任务 14.5）。
- ``build_asgi_app``：用 custom_route 挂载 /results/，生成 streamable_http_app()，在其 lifespan 中（会话管理器
  之内）启动 Result_Store 与审计的后台清理，最外层套认证中间件。
- ``main``：启动检查、Result_Store 与审计目录初始化，再由 uvicorn 监听（10.1、10.2）。

任务 8.7 加入断开取消与 SIGTERM 停机：

- 断开（10.5）：中间件包住 receive，请求体原样交给下游；请求体读完（more_body 为假）后启动监听任务继续调用
  原 receive，收到 http.disconnect（或 receive 出错）即置位 RequestContext.disconnected，下游之后调用 receive
  仍会拿到同一条 http.disconnect；请求结束时取消监听任务。stateless 模式下 SDK 在会话管理器的任务组中运行工具，
  取消 HTTP 请求任务并不会取消工具，所以有调用上下文时 call_tool 把工作放进子任务，在子任务与断开事件之间等待；
  断开即取消子任务并等它结束（SearchLogsRun 删除本次结果，run_query 的 finally 终止远端进程组），以 cancelled
  写审计，然后**返回**整次错误结果（INTERNAL_ERROR，说明为"调用方已断开，调用已取消"），由 SDK 照常答复。
  不抛出 CancelledError：工具任务自己抛出未被请求的取消会让 SDK 的任务组整体取消，HTTP 端得不到答复并记一条
  错误日志；答复写给已断开的连接时 uvicorn 直接丢弃。没有调用上下文（测试中在进程内直接调用）时与之前相同：
  不建子任务，外部取消照常以 cancelled 写审计后抛出 CancelledError。
- 停机（10.6）：``LogViewerServer.handle_exit`` 先置停机标志（只设一个布尔值，可在信号处理函数中执行），
  此后中间件对新的 HTTP 请求在认证之前返回 503（Local_MCP 映射为 SERVICE_UNAVAILABLE；不论 token 是否有效，
  停机期间都不做认证、不访问管理库）；再经 call_soon_threadsafe 启动停机协程：``LogViewerMCP.cancel_calls``
  取消登记中的全部调用（各自终止远端进程组、以 cancelled 写审计，并答复"服务正在停机，调用已取消"），
  最多等 SHUTDOWN_GRACE_S 秒，然后交给 uvicorn 原有的 handle_exit 退出（停止监听、最多等
  GRACEFUL_SHUTDOWN_TIMEOUT_S 秒让连接结束、lifespan 关闭，其中取消后台清理任务）。停机等待期间再次收到信号时不再等待，置 force_exit 立即退出（与 uvicorn 第二次
  Ctrl+C 相同）。标志置位后才开始的调用一登记即被取消。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import re
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from types import FrameType
from typing import Any

import anyio
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.tools import Tool
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from log_viewer_mcp.common.errors import ErrorCode, LogViewerError
from log_viewer_mcp.common.tool_docs import (
    LIST_SERVERS,
    LIST_SERVERS_DESCRIPTION,
    LIST_SERVERS_TITLE,
    SEARCH_LOGS,
    SEARCH_LOGS_DESCRIPTION,
    SEARCH_LOGS_TITLE,
    list_servers_input_schema,
    search_logs_input_schema,
)
from log_viewer_mcp.server.audit import AuditLog, AuditTarget, AuditWriteError
from log_viewer_mcp.server.config import Config, ConfigError, TokenRecord, load_config_or_exit
from log_viewer_mcp.server.context import RequestContext, current_request, current_token, current_user
from log_viewer_mcp.server.db import check_admin_db
from log_viewer_mcp.server.remote import check_ssh_credentials
from log_viewer_mcp.server.results import ResultStore, mount_result_routes
from log_viewer_mcp.server.tokens import TokenSet
from log_viewer_mcp.server.tools import SearchLogsRun, ToolResult, ToolServices, list_servers
from log_viewer_mcp.server.validate import (
    AsyncRegexChecker,
    grep_ere_check_works,
    grep_ere_is_valid_async,
    validate_arguments_async,
)

__all__ = [
    "AUDIT_STATUS_CANCELLED",
    "CALL_TASK_NAME",
    "DISCONNECT_WATCH_TASK_NAME",
    "SERVER_NAME",
    "SHUTDOWN_GRACE_S",
    "BearerAuthMiddleware",
    "LogViewerMCP",
    "LogViewerServer",
    "build_asgi_app",
    "build_tools",
    "check_grep_ere",
    "create_mcp",
    "main",
    "parse_args",
]

logger = logging.getLogger(__name__)

SERVER_NAME = "log-viewer-mcp"
AUDIT_STATUS_CANCELLED = "cancelled"

# 停机时等待进行中的调用结束（远端 kill 限时 remote.KILL_TIMEOUT_S）的时长上限（秒）。
SHUTDOWN_GRACE_S = 10.0
# 之后 uvicorn 等待连接结束的时长上限（秒），超过即取消请求任务。
GRACEFUL_SHUTDOWN_TIMEOUT_S = 5

# 任务名（便于在运行日志与测试中识别）。
CALL_TASK_NAME = "log-viewer-mcp-call"
DISCONNECT_WATCH_TASK_NAME = "log-viewer-mcp-disconnect-watch"

# 调用被取消的原因（决定答复中的说明；审计状态都是 cancelled）。
CANCEL_DISCONNECTED = "disconnected"
CANCEL_SHUTDOWN = "shutdown"
_CANCEL_MESSAGES = {
    CANCEL_DISCONNECTED: "调用方已断开，调用已取消",
    CANCEL_SHUTDOWN: "服务正在停机，调用已取消，请稍后重试",
    None: "调用已取消",
}

_READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=True)


async def _not_called() -> None:  # pragma: no cover - call_tool 不经 FastMCP 的工具函数
    raise RuntimeError("工具由 LogViewerMCP.call_tool 直接执行")


def _tool(name: str, title: str, description: str, parameters: dict[str, Any]) -> Tool:
    tool = Tool.from_function(
        _not_called, name=name, title=title, description=description, annotations=_READ_ONLY, structured_output=False,
    )
    return tool.model_copy(update={"parameters": parameters})


def build_tools(config: Config) -> list[Tool]:
    """两个工具的注册信息：schema 中 log_type 的 enum 与各上限取自配置。"""
    limits = config.limits
    search_schema = search_logs_input_schema(
        log_types=tuple(config.log_types),
        max_servers=limits.max_servers,
        max_patterns=limits.max_patterns,
        max_pattern_length=limits.max_pattern_length,
        max_context_lines=limits.max_context_lines,
    )
    return [
        _tool(SEARCH_LOGS, SEARCH_LOGS_TITLE, SEARCH_LOGS_DESCRIPTION, search_schema),
        _tool(LIST_SERVERS, LIST_SERVERS_TITLE, LIST_SERVERS_DESCRIPTION, list_servers_input_schema()),
    ]


def _to_call_tool_result(result: ToolResult) -> CallToolResult:
    text = json.dumps(result.payload, ensure_ascii=False)
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structuredContent=result.payload,
        isError=result.is_error,
    )


@dataclass(eq=False)
class _CallState:
    """一次经 HTTP 到达的调用：工作子任务、SearchLogsRun（审计 targets）、取消原因与是否已写完审计。"""

    task: asyncio.Task[ToolResult] | None = None
    run: SearchLogsRun | None = None
    cancel_reason: str | None = None
    finished: asyncio.Event = field(default_factory=asyncio.Event)


class LogViewerMCP(FastMCP):
    """跳板机上的 MCP 服务：Streamable HTTP、stateless、JSON 响应，call_tool 先校验原始参数再执行。

    有调用上下文（经认证中间件到达）的调用登记在 ``_active_calls`` 中，停机时由 ``cancel_calls`` 取消。
    """

    def __init__(
        self,
        config: Config,
        services: ToolServices,
        audit: AuditLog,
        *,
        regex_checker: AsyncRegexChecker = grep_ere_is_valid_async,
    ) -> None:
        super().__init__(
            name=SERVER_NAME,
            tools=build_tools(config),
            host=config.listen.address,  # 默认 127.0.0.1 会只接受本机 Host 头（以锁定版本为准）
            port=config.listen.port,
            stateless_http=True,
            json_response=True,
        )
        self.config = config
        self.services = services
        self.audit = audit
        self._regex_checker = regex_checker
        self._shutting_down = False
        self._active_calls: set[_CallState] = set()

    # -- 停机 ---------------------------------------------------------------

    @property
    def shutting_down(self) -> bool:
        return self._shutting_down

    def begin_shutdown(self) -> None:
        """置停机标志：此后中间件对新请求返回 503，新登记的调用立即取消。只设布尔值，可在信号处理函数中调用。"""
        self._shutting_down = True

    @property
    def active_call_count(self) -> int:
        return len(self._active_calls)

    async def cancel_calls(self, timeout: float = SHUTDOWN_GRACE_S) -> int:
        """停机：置停机标志，取消登记中的全部调用，最多等 timeout 秒让它们终止远端进程并写完审计。

        返回取消的调用数。
        """
        self.begin_shutdown()
        states = list(self._active_calls)
        for state in states:
            task = state.task
            if task is not None and not task.done():
                if state.cancel_reason is None:
                    state.cancel_reason = CANCEL_SHUTDOWN
                task.cancel()
        if states:
            waiters = [asyncio.create_task(s.finished.wait()) for s in states]
            _, pending = await asyncio.wait(waiters, timeout=timeout)
            for w in pending:
                w.cancel()
            if pending:
                logger.warning("停机：%d 个调用在 %.0f 秒内没有结束", len(pending), timeout)
        return len(states)

    # -- 工具调用 -------------------------------------------------------------

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> CallToolResult:  # type: ignore[override]
        """执行一次工具调用并写审计；arguments 是客户端发来的原始参数字典。

        有调用上下文时在子任务中执行，调用方断开或停机时取消子任务、以 cancelled 写审计并返回整次错误结果；
        没有调用上下文时直接执行。两种情况下外部取消都以 cancelled 写审计后继续抛出 CancelledError。
        """
        loop = asyncio.get_running_loop()
        started = time.monotonic()
        deadline = loop.time() + self.config.limits.query_timeout_s
        user = current_user.get()
        token = current_token.get()
        request = current_request.get()
        state = _CallState()

        def duration_ms() -> int:
            return int((time.monotonic() - started) * 1000)

        try:
            try:
                if request is None:
                    result: ToolResult | None = await self._execute(name, arguments, deadline, state)
                else:
                    result = await self._execute_watched(name, arguments, deadline, state, request)
            except asyncio.CancelledError:
                # SearchLogsRun.run 已删除本次结果；以 cancelled 写审计后继续抛出。
                await self._write_cancelled_audit(name, arguments, user, token, state.run, duration_ms())
                raise

            if result is None:
                # 调用方断开或停机：工作子任务已结束（远端进程已终止、本次结果已删除）。
                reason = state.cancel_reason
                logger.info("工具调用已取消（%s）", reason or "cancelled")
                await self._write_cancelled_audit(name, arguments, user, token, state.run, duration_ms())
                error = LogViewerError(ErrorCode.INTERNAL_ERROR, _CANCEL_MESSAGES.get(reason, _CANCEL_MESSAGES[None]))
                return _to_call_tool_result(ToolResult.failure(error))

            return await self._audit_and_return(name, arguments, user, token, result, duration_ms())
        finally:
            self._active_calls.discard(state)
            state.finished.set()

    async def _execute(self, name: str, arguments: dict[str, Any], deadline: float, state: _CallState) -> ToolResult:
        """校验并执行；工具错误转为整次错误结果，取消原样抛出。"""
        try:
            params = await validate_arguments_async(name, arguments, self.config, regex_checker=self._regex_checker)
            if name == SEARCH_LOGS:
                assert params is not None
                state.run = SearchLogsRun(self.services, params, deadline=deadline)
                return await state.run.run()
            return await list_servers(self.services)
        except LogViewerError as e:
            logger.info("工具调用被拒绝：%s", e.code.value)
            return ToolResult.failure(e)
        except Exception:
            logger.exception("工具调用出现未预期的异常")
            run = state.run
            handle = run.result_handle if run is not None else None
            if handle is not None:
                self.services.store.discard(handle)
            targets = run.audit_targets if run is not None else ()
            return ToolResult.failure(LogViewerError(ErrorCode.INTERNAL_ERROR), result_handle=handle, targets=targets)

    async def _execute_watched(
        self,
        name: str,
        arguments: dict[str, Any],
        deadline: float,
        state: _CallState,
        request: RequestContext,
    ) -> ToolResult | None:
        """在子任务中执行，同时等待断开事件；返回 None 表示子任务因断开或停机被取消（已结束）。

        外部取消时同样先取消子任务并等它结束，再抛出 CancelledError。
        """
        work = asyncio.create_task(self._execute(name, arguments, deadline, state), name=CALL_TASK_NAME)
        state.task = work
        self._active_calls.add(state)
        if self._shutting_down:
            state.cancel_reason = CANCEL_SHUTDOWN
            work.cancel()
        disconnect = asyncio.create_task(request.disconnected.wait(), name=f"{CALL_TASK_NAME}-disconnect")
        try:
            await asyncio.wait((work, disconnect), return_when=asyncio.FIRST_COMPLETED)
        finally:
            disconnect.cancel()
            if not work.done():
                if state.cancel_reason is None and request.disconnected.is_set():
                    state.cancel_reason = CANCEL_DISCONNECTED
                work.cancel()
            # 等子任务结束（远端 kill 在其中完成）；外部再次取消也不中断等待。
            with anyio.CancelScope(shield=True):
                await asyncio.wait((work, disconnect))
        if work.cancelled():
            return None
        return work.result()

    async def _audit_and_return(
        self,
        name: str,
        arguments: dict[str, Any],
        user: str | None,
        token: str | None,
        result: ToolResult,
        duration: int,
    ) -> CallToolResult:
        try:
            await self.audit.write(
                user=user,
                tool=name,
                params=arguments,
                status=result.status,
                duration_ms=duration,
                result_handle=result.result_handle,
                targets=result.targets,
                token=token,
            )
        except AuditWriteError as e:
            if result.result_handle is not None:
                self.services.store.discard(result.result_handle)
            return _to_call_tool_result(ToolResult.failure(e, result_handle=result.result_handle))
        return _to_call_tool_result(result)

    async def _write_cancelled_audit(
        self,
        name: str,
        arguments: Mapping[str, Any] | None,
        user: str | None,
        token: str | None,
        run: SearchLogsRun | None,
        duration: int,
    ) -> None:
        targets: tuple[AuditTarget, ...] = run.audit_targets if run is not None else ()
        handle = run.result_handle if run is not None else None
        with anyio.CancelScope(shield=True):
            try:
                await self.audit.write(
                    user=user,
                    tool=name,
                    params=arguments,
                    status=AUDIT_STATUS_CANCELLED,
                    duration_ms=duration,
                    result_handle=handle,
                    targets=targets,
                    token=token,
                )
            except AuditWriteError:
                pass  # audit.py 已写运行日志；调用已取消，没有可返回的结果


def create_mcp(
    config: Config,
    services: ToolServices,
    audit: AuditLog,
    *,
    regex_checker: AsyncRegexChecker = grep_ere_is_valid_async,
) -> LogViewerMCP:
    """构造跳板机的 MCP 服务；任务 8.6 用它生成 Starlette 应用（streamable_http_app）。"""
    return LogViewerMCP(config, services, audit, regex_checker=regex_checker)


# ---------------------------------------------------------------------------
# 认证中间件（6.1）
# ---------------------------------------------------------------------------

# Authorization 的值：方案名（不区分大小写，须为 Bearer）、一个空格、token（可见 ASCII，不含空白）。
_AUTHORIZATION_PATTERN = re.compile(r"([A-Za-z]+) ([\x21-\x7e]+)")

_UNAUTHORIZED_HEADERS = {"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"}
_SHUTTING_DOWN_HEADERS = {"Retry-After": "5", "Cache-Control": "no-store", "Connection": "close"}


class _DisconnectWatchingReceive:
    """包住一个 HTTP 请求的 receive：请求体原样交给下游，读完后由监听任务继续 receive，断开即置位事件。"""

    def __init__(self, receive: Receive, context: RequestContext) -> None:
        self._receive = receive
        self._context = context
        self._watcher: asyncio.Task[Message] | None = None

    async def __call__(self) -> Message:
        if self._watcher is not None:
            # 请求体已读完：原 receive 由监听任务独占，下游拿到它收到的 http.disconnect。
            return await asyncio.shield(self._watcher)
        message = await self._receive()
        kind = message.get("type")
        if kind == "http.disconnect":
            self._context.disconnected.set()
        elif kind == "http.request" and not message.get("more_body", False):
            self._watcher = asyncio.create_task(self._watch(), name=DISCONNECT_WATCH_TASK_NAME)
        return message

    async def _watch(self) -> Message:
        while True:
            try:
                message = await self._receive()
            except Exception:
                self._context.disconnected.set()
                raise
            if message.get("type") == "http.disconnect":
                self._context.disconnected.set()
                return message
            # 请求体之后不应再有 http.request；忽略。

    async def aclose(self) -> None:
        """请求结束：取消监听任务并等它结束。"""
        watcher = self._watcher
        if watcher is None:
            return
        if not watcher.done():
            watcher.cancel()
        await asyncio.wait((watcher,))
        if not watcher.cancelled():
            watcher.exception()  # 取走异常，避免"从未取回"的警告


def _token_digest(token: str) -> bytes:
    """与 Token_CLI 相同的摘要：UTF-8 编码后 SHA-256 的小写十六进制（ASCII 字节，供 compare_digest）。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest().encode("ascii")


class BearerAuthMiddleware:
    """纯 ASGI 认证中间件：每个 HTTP 请求须携带 ``Authorization: Bearer <token>``，否则返回 401。

    - 只认恰好一个 Authorization 头；方案名不区分大小写，token 为不含空白的可见 ASCII。
    - 认证用的集合由 tokens.py 的 ``TokenSet`` 持有：每次认证之前 stat 一次 tokens.json，变化时重新读取（6.6、6.7）。
      传入 TokenRecord 序列时为固定集合（不读文件）。
    - token 的 SHA-256 与每个未吊销的摘要逐一做 ``hmac.compare_digest``，比完全部才得出结果（不提前退出）。
    - 认证失败只写一条运行日志（方法、路径与来源地址），不记录 Authorization 的值。
    - 认证通过后设置 current_user、current_token 与 current_request，并包住 receive 监听断开（10.5），
      再交给下游应用；请求结束后取消监听任务并复原 contextvars。
    - is_shutting_down() 为真时（停机，10.6），任何 HTTP 请求在认证之前返回 503。
    - lifespan 作用域原样透传；websocket 作用域直接关闭（应用没有 websocket 端点）。
    """

    def __init__(
        self,
        app: ASGIApp,
        tokens: TokenSet | Iterable[TokenRecord],
        *,
        is_shutting_down: Callable[[], bool] | None = None,
    ) -> None:
        self.app = app
        self.token_set = tokens if isinstance(tokens, TokenSet) else TokenSet(None, tokens)
        self._is_shutting_down = is_shutting_down

    def authenticate(self, headers: Iterable[tuple[bytes, bytes]]) -> tuple[str, str] | None:
        """返回 (使用者, token)；缺少、格式不符、无效或已吊销时返回 None。

        先检查 tokens.json 是否变化（每次一个 os.stat），再按当前集合认证。
        """
        entries = self.token_set.refresh()
        values = [value for name, value in headers if name.lower() == b"authorization"]
        if len(values) != 1:
            return None
        m = _AUTHORIZATION_PATTERN.fullmatch(values[0].decode("latin-1"))
        if m is None or m.group(1).lower() != "bearer":
            return None
        token = m.group(2)
        digest = _token_digest(token)
        user: str | None = None
        for expected, name in entries:
            # 先比较再判断 user，保证每个摘要都参与比较。
            if hmac.compare_digest(digest, expected) and user is None:
                user = name
        if user is None:
            return None
        return user, token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope_type = scope["type"]
        if scope_type == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope_type != "http":
            if scope_type == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return

        if self._is_shutting_down is not None and self._is_shutting_down():
            response = PlainTextResponse("Service Unavailable", status_code=503, headers=_SHUTTING_DOWN_HEADERS)
            await response(scope, receive, send)
            return

        identity = self.authenticate(scope.get("headers") or ())
        if identity is None:
            client = scope.get("client")
            logger.warning(
                "认证失败（缺少、无效或已吊销的 token）：%s %.200r 来自 %s",
                scope.get("method"), scope.get("path"), client[0] if client else "?",
            )
            response = PlainTextResponse("Unauthorized", status_code=401, headers=_UNAUTHORIZED_HEADERS)
            await response(scope, receive, send)
            return

        user, token = identity
        context = RequestContext(user=user)
        watching_receive = _DisconnectWatchingReceive(receive, context)
        user_reset = current_user.set(user)
        token_reset = current_token.set(token)
        request_reset = current_request.set(context)
        try:
            await self.app(scope, watching_receive, send)
        finally:
            try:
                await watching_receive.aclose()
            finally:
                current_request.reset(request_reset)
                current_token.reset(token_reset)
                current_user.reset(user_reset)


# ---------------------------------------------------------------------------
# ASGI 应用（10.1）
# ---------------------------------------------------------------------------

BackgroundJob = Callable[[], Awaitable[None]]


def _add_background_jobs(app: Starlette, jobs: Iterable[BackgroundJob]) -> None:
    """在 app 原有的 lifespan（FastMCP 会话管理器的 run()）之内启动后台任务，关闭时先取消它们。"""
    inner = app.router.lifespan_context
    job_list = tuple(jobs)

    @contextlib.asynccontextmanager
    async def lifespan(a: Any) -> AsyncIterator[Any]:
        async with inner(a) as state:
            tasks = [asyncio.create_task(job(), name=f"log-viewer-mcp-{i}") for i, job in enumerate(job_list)]
            try:
                yield state
            finally:
                for task in tasks:
                    task.cancel()
                for result in await asyncio.gather(*tasks, return_exceptions=True):
                    if isinstance(result, Exception):
                        logger.error("后台任务异常结束", exc_info=result)

    app.router.lifespan_context = lifespan


def build_asgi_app(mcp: LogViewerMCP) -> BearerAuthMiddleware:
    """组装对外的 ASGI 应用：/mcp（Streamable HTTP）与 /results/，外层为认证中间件。

    lifespan 中运行会话管理器，以及 Result_Store（5.3）与审计（6.4）的后台清理。每个 LogViewerMCP 只能调用一次
    （custom_route 会重复挂载，会话管理器也只能运行一次）。
    """
    mount_result_routes(mcp, mcp.services.store)  # 须在 streamable_http_app() 之前
    starlette_app = mcp.streamable_http_app()
    _add_background_jobs(starlette_app, (mcp.services.store.run_cleanup, mcp.audit.run_cleanup))
    # 构造时重新读取一次 tokens.json（不存在时写 warning），之后每个请求认证前检查是否变化（6.6）。
    token_set = TokenSet(mcp.config.tokens_file, mcp.config.tokens)
    return BearerAuthMiddleware(starlette_app, token_set, is_shutting_down=lambda: mcp.shutting_down)


# ---------------------------------------------------------------------------
# 启动（10.1、10.2）
# ---------------------------------------------------------------------------


class LogViewerServer(uvicorn.Server):
    """uvicorn 服务；覆盖 handle_exit 实现停机（10.6）。

    第一次收到 SIGTERM/SIGINT：置 mcp 的停机标志（此后新请求返回 503），经 call_soon_threadsafe 启动停机协程，
    由它取消进行中的调用并最多等 shutdown_grace_s 秒，然后调用 uvicorn 原有的 handle_exit（should_exit，信号在
    退出时照常重新发出）。停机协程完成之前再次收到信号：置 force_exit 并交给 uvicorn，立即退出。
    没有 mcp 或不在事件循环中时与 uvicorn 原有行为相同。
    """

    def __init__(
        self,
        config: uvicorn.Config,
        mcp: LogViewerMCP | None = None,
        *,
        shutdown_grace_s: float = SHUTDOWN_GRACE_S,
    ) -> None:
        super().__init__(config)
        self.mcp = mcp
        self.shutdown_grace_s = shutdown_grace_s
        self._shutdown_requested = False
        self._shutdown_task: asyncio.Task[None] | None = None

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        # 信号处理函数：只设标志与安排回调，不记日志、不等待。
        mcp = self.mcp
        if mcp is None or self.should_exit:
            super().handle_exit(sig, frame)
            return
        if self._shutdown_requested:
            self.force_exit = True
            super().handle_exit(sig, frame)
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            super().handle_exit(sig, frame)
            return
        self._shutdown_requested = True
        mcp.begin_shutdown()
        loop.call_soon_threadsafe(self._start_graceful_shutdown, sig, frame)

    def _start_graceful_shutdown(self, sig: int, frame: FrameType | None) -> None:
        self._shutdown_task = asyncio.ensure_future(self._graceful_shutdown(sig, frame))

    async def _graceful_shutdown(self, sig: int, frame: FrameType | None) -> None:
        assert self.mcp is not None
        if self.force_exit:
            return
        logger.info("收到信号 %d：停止接受新调用，取消进行中的调用", sig)
        try:
            count = await self.mcp.cancel_calls(self.shutdown_grace_s)
            logger.info("停机：已取消 %d 个进行中的调用", count)
        except Exception:
            logger.exception("停机时取消进行中的调用出错")
        finally:
            if not self.should_exit:
                uvicorn.Server.handle_exit(self, sig, frame)


def check_grep_ere(config: Config) -> None:
    """启动检查：regex 模式的合法性由本机 grep 判断（8.1），本机 grep 不能区分合法与非法的扩展正则时拒绝启动。"""
    if not grep_ere_check_works():
        raise ConfigError("本机 grep 无法判断扩展正则是否合法（需要 GNU grep 且可执行），拒绝启动")


STARTUP_CHECKS: tuple[Callable[[Config], None], ...] = (check_ssh_credentials, check_grep_ere, check_admin_db)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="log-viewer-mcp-server", description="线上日志查询 MCP 服务")
    parser.add_argument("-c", "--config", required=True, help="TOML 配置文件路径")
    return parser.parse_args(argv)


def configure_logging() -> None:
    """运行日志写标准错误（10.1）；uvicorn 不另行配置日志，其访问日志也经根 logger 写到标准错误。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )


def _startup_failed(message: object) -> int:
    print(f"log-viewer-mcp 启动失败：{message}", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    """`log-viewer-mcp-server -c config.toml`（或 `python -m log_viewer_mcp.server -c config.toml`）的入口。

    配置与凭据检查、SSH 凭据导入、本机 grep 自检、Admin_DB 连通检查，任一不满足即在标准错误中说明并以 2 退出；
    然后初始化 Result_Store（清空旧结果）与审计目录，组装应用并由 uvicorn 在 Listen_Address、Listen_Port 上监听。
    """
    args = parse_args(argv)
    configure_logging()
    config = load_config_or_exit(args.config, extra_checks=STARTUP_CHECKS)
    try:
        store = ResultStore.from_config(config.results)
        store.prepare()
        audit = AuditLog.from_config(config)
        audit.prepare()
        services = ToolServices.from_config(config, store=store)
    except ConfigError as e:
        return _startup_failed(e)

    mcp = create_mcp(config, services, audit)
    app = build_asgi_app(mcp)
    server = LogViewerServer(
        uvicorn.Config(
            app,
            host=config.listen.address,
            port=config.listen.port,
            lifespan="on",
            log_config=None,
            proxy_headers=False,
            server_header=False,
            date_header=False,
            timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_TIMEOUT_S,
        ),
        mcp,
    )
    logger.info("log-viewer-mcp 启动：监听 %s:%d，有效 token %d 个", config.listen.address, config.listen.port,
                app.token_set.active_count)
    server.run()
    return 0 if server.started else 1
