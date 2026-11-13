"""并发上限（需求 9.2；design.md「服务入口（app.py）」的并发规则）。

一个进程只用一个 ``ConcurrencyLimiter``（由 ToolServices 持有，各次调用共用）：

- 全局：asyncio.Semaphore(Max_Concurrent_Queries) 非阻塞获取，``locked()`` 为真即抛出 BUSY（整次调用），
  不排队。名额按 search_logs 调用计，从取得名额起到调用结束（含被取消）为止。
  list_servers 只读一次管理库、不登录游戏服，不占全局名额。
- 单服：按 ip_inner 计数，达到 Max_Concurrent_Per_Server 即抛出 BUSY（只影响该 svr_id），不排队。
  名额从建连之前占用到连接关闭为止；同一次调用中 ip_inner 相同的多个 svr_id 各占一个名额。

两种名额都在 finally 中归还，调用正常结束、出错或被取消时都会归还。
全部操作都在事件循环线程中执行，检查与占用之间没有 await，不会有竞争。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Iterator

from log_viewer_mcp.common.errors import ErrorCode, LogViewerError
from log_viewer_mcp.server.config import Limits

__all__ = ["ConcurrencyLimiter"]


class ConcurrencyLimiter:
    """全局与单服的并发名额；满了抛出 LogViewerError(BUSY)，不排队。"""

    def __init__(self, max_concurrent: int, max_per_server: int) -> None:
        if max_concurrent < 1 or max_per_server < 1:
            raise ValueError("并发上限必须为正整数")
        self.max_concurrent = max_concurrent
        self.max_per_server = max_per_server
        self._calls = asyncio.Semaphore(max_concurrent)
        self._active_calls = 0
        self._per_server: dict[str, int] = {}

    @classmethod
    def from_limits(cls, limits: Limits) -> ConcurrencyLimiter:
        return cls(limits.max_concurrent, limits.max_concurrent_per_server)

    @property
    def active_calls(self) -> int:
        """占用全局名额的调用数。"""
        return self._active_calls

    def active_on(self, ip_inner: str) -> int:
        """该游戏服上占用的名额数。"""
        return self._per_server.get(ip_inner, 0)

    @contextlib.asynccontextmanager
    async def call_slot(self) -> AsyncIterator[None]:
        """占用一个全局名额；已满时抛出 BUSY。"""
        if self._calls.locked():
            raise LogViewerError(ErrorCode.BUSY)
        await self._calls.acquire()  # 未满时立即返回，不让出事件循环
        self._active_calls += 1
        try:
            yield
        finally:
            self._active_calls -= 1
            self._calls.release()

    @contextlib.contextmanager
    def server_slot(self, ip_inner: str) -> Iterator[None]:
        """在 ip_inner 上占用一个名额；已满时抛出 BUSY。"""
        count = self._per_server.get(ip_inner, 0)
        if count >= self.max_per_server:
            raise LogViewerError(ErrorCode.BUSY)
        self._per_server[ip_inner] = count + 1
        try:
            yield
        finally:
            remaining = self._per_server[ip_inner] - 1
            if remaining:
                self._per_server[ip_inner] = remaining
            else:
                del self._per_server[ip_inner]
