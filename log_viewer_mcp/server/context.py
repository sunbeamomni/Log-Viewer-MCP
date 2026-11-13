"""每个请求的调用上下文（design.md「服务入口（app.py）」）。

认证中间件（app.py 的 BearerAuthMiddleware）在把请求交给 MCP 应用之前设置这些 contextvars；call_tool
从中取出使用者与 token 原文，写入审计（token 只用于掩码，6.4）。在请求之外（例如单元测试直接调用
call_tool）取值为 None。

asyncio（anyio）新建任务时复制创建者的 contextvars，所以 stateless 模式下 SDK 在请求处理中启动的
工具任务也能读到中间件设置的值（tests/unit/test_app_http.py 经真实 HTTP 请求验证）。
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field

__all__ = ["RequestContext", "current_request", "current_token", "current_user"]

# 本请求的使用者名称（tokens.json 中的 user）。
current_user: ContextVar[str | None] = ContextVar("log_viewer_mcp_current_user", default=None)

# 本请求的 Access_Token 原文；只用于在审计与运行日志中掩码，不得写入任何输出。
current_token: ContextVar[str | None] = ContextVar("log_viewer_mcp_current_token", default=None)


@dataclass
class RequestContext:
    """一个已认证 HTTP 请求的调用上下文。

    disconnected：客户端断开时置位（由任务 8.7 在中间件中监听 http.disconnect 并置位，call_tool 据此取消
    工具任务）。
    """

    user: str
    disconnected: asyncio.Event = field(default_factory=asyncio.Event)


# 本请求的调用上下文；请求之外为 None。
current_request: ContextVar[RequestContext | None] = ContextVar("log_viewer_mcp_current_request", default=None)
