"""断开取消与停机的单元测试（任务 8.7，需求 10.5、10.6）。

在 ASGI 层驱动 build_asgi_app 组装的应用：断开用自己写的 receive/send 模拟（httpx 的 ASGITransport 不能在
响应之前断开），lifespan 与令牌沿用 test_app_http.py。替身远端的 run_query 阻塞到被取消，并记录取消
（真实实现在取消时终止远端进程组，见 tests/integration/test_server_shutdown.py）。
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
import uvicorn

from log_viewer_mcp.server.app import (
    CALL_TASK_NAME,
    DISCONNECT_WATCH_TASK_NAME,
    LogViewerMCP,
    LogViewerServer,
    _DisconnectWatchingReceive,
    build_asgi_app,
    create_mcp,
)
from log_viewer_mcp.server.context import RequestContext
from tests.unit.test_app import AT, Env, accept_all
from tests.unit.test_app_http import MCP_HEADERS, OTHER_TOKEN, TOKENS, VALID_TOKEN, bearer, lifespan, rpc_call

SEARCH_ARGS = {"svr_ids": [1001], "log_type": "normal", "at_time": AT}
WAIT_S = 5


@pytest.fixture
def env(tmp_path) -> Env:
    return Env(tmp_path)


@pytest_asyncio.fixture
async def served(env) -> AsyncIterator[tuple[LogViewerMCP, object]]:
    mcp = create_mcp(env.config_with_tokens(TOKENS), env.services, env.audit, regex_checker=accept_all)
    app = build_asgi_app(mcp)
    async with lifespan(app):
        yield mcp, app


class RawRequest:
    """在 ASGI 层发一个 HTTP 请求：请求体可分块；disconnect() 模拟客户端断开。"""

    def __init__(self, app, method: str, path: str, body: bytes = b"", *, token: str = VALID_TOKEN,
                 chunks: int = 1) -> None:
        headers = {**MCP_HEADERS, **bearer(token), "host": "testserver", "content-length": str(len(body))}
        self.scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
            "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            "server": ("testserver", 80), "client": ("127.0.0.1", 50000), "state": {},
        }
        self.incoming: asyncio.Queue = asyncio.Queue()
        size = max(1, -(-len(body) // chunks))
        parts = [body[i:i + size] for i in range(0, len(body), size)] or [b""]
        for i, part in enumerate(parts):
            self.incoming.put_nowait({"type": "http.request", "body": part, "more_body": i < len(parts) - 1})
        self.sent: list[dict] = []
        self.task = asyncio.create_task(app(self.scope, self.incoming.get, self._send))

    async def _send(self, message) -> None:
        self.sent.append(message)

    def disconnect(self) -> None:
        self.incoming.put_nowait({"type": "http.disconnect"})

    async def finish(self) -> None:
        await asyncio.wait_for(self.task, WAIT_S)

    @property
    def status(self) -> int:
        return next(m["status"] for m in self.sent if m["type"] == "http.response.start")

    def json(self) -> dict:
        return json.loads(b"".join(m.get("body", b"") for m in self.sent if m["type"] == "http.response.body"))


def tool_request(app, name: str, arguments: dict, **kwargs) -> RawRequest:
    return RawRequest(app, "POST", "/mcp", json.dumps(rpc_call(name, arguments)).encode(), **kwargs)


def lingering_tasks() -> list[str]:
    names = {DISCONNECT_WATCH_TASK_NAME, CALL_TASK_NAME, f"{CALL_TASK_NAME}-disconnect"}
    return [t.get_name() for t in asyncio.all_tasks() if t.get_name() in names and not t.done()]


def error_of(response: dict) -> dict:
    result = response["result"]
    assert result["isError"] is True
    return result["structuredContent"]["error"]


def assert_cancelled_and_cleaned(env: Env, mcp: LogViewerMCP) -> None:
    (record,) = env.audit_records()
    assert record["tool"] == "search_logs" and record["user"] == "zhangsan"
    assert record["status"] == "cancelled"
    (target,) = record["targets"]
    assert target["svr_id"] == 1001 and target["status"] == "cancelled" and len(target["scripts"]) == 2
    assert os.listdir(env.config.results.dir) == []  # 本次结果目录已删除
    assert mcp.active_call_count == 0
    assert lingering_tasks() == []


# ---------------------------------------------------------------------------
# 断开（10.5）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disconnect_cancels_running_search(served, env) -> None:
    mcp, app = served
    env.remote.block = asyncio.Event()  # 永不置位：查询一直进行到被取消
    req = tool_request(app, "search_logs", SEARCH_ARGS)
    await asyncio.wait_for(env.remote.started.wait(), WAIT_S)
    assert mcp.active_call_count == 1 and not req.task.done()

    req.disconnect()
    await asyncio.wait_for(env.remote.cancelled.wait(), WAIT_S)
    await req.finish()

    # 答复照常发出（真实服务器对已断开的连接直接丢弃）。
    assert req.status == 200
    assert error_of(req.json()) == {"code": "INTERNAL_ERROR", "message": "调用方已断开，调用已取消"}
    assert_cancelled_and_cleaned(env, mcp)


@pytest.mark.asyncio
async def test_disconnect_after_response_does_not_affect_completed_call(served, env) -> None:
    mcp, app = served
    req = tool_request(app, "search_logs", SEARCH_ARGS, chunks=3)  # 请求体分块到达
    await req.finish()
    assert req.status == 200
    summary = req.json()["result"]["structuredContent"]
    assert summary["servers"][0]["status"] == "ok"
    (record,) = env.audit_records()
    assert record["status"] == "ok"
    assert os.listdir(env.config.results.dir) == [summary["result_handle"]]
    # 没有收到 http.disconnect，请求结束时监听任务也已取消。
    assert lingering_tasks() == [] and mcp.active_call_count == 0
    assert not env.remote.cancelled.is_set()


@pytest.mark.asyncio
async def test_receive_wrapper_passes_body_then_reports_disconnect() -> None:
    queue: asyncio.Queue = asyncio.Queue()
    parts = [
        {"type": "http.request", "body": b'{"a":', "more_body": True},
        {"type": "http.request", "body": b"1}", "more_body": False},
    ]
    for p in parts:
        queue.put_nowait(p)
    context = RequestContext(user="zhangsan")
    receive = _DisconnectWatchingReceive(queue.get, context)

    assert [await receive(), await receive()] == parts  # 请求体原样交给下游
    later = asyncio.create_task(receive())
    await asyncio.sleep(0.01)
    assert not later.done() and not context.disconnected.is_set()

    queue.put_nowait({"type": "http.disconnect"})
    assert await asyncio.wait_for(later, WAIT_S) == {"type": "http.disconnect"}
    assert context.disconnected.is_set()
    assert await receive() == {"type": "http.disconnect"}  # 之后的调用仍拿到 http.disconnect
    await receive.aclose()
    assert lingering_tasks() == []


@pytest.mark.asyncio
async def test_receive_wrapper_watcher_cancelled_on_close() -> None:
    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait({"type": "http.request", "body": b"", "more_body": False})
    context = RequestContext(user="zhangsan")
    receive = _DisconnectWatchingReceive(queue.get, context)
    await receive()
    assert DISCONNECT_WATCH_TASK_NAME in lingering_tasks()
    await receive.aclose()
    assert lingering_tasks() == [] and not context.disconnected.is_set()


# ---------------------------------------------------------------------------
# 停机（10.6）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutting_down_returns_503_before_auth(served, env) -> None:
    mcp, app = served
    mcp.begin_shutdown()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        for token in (VALID_TOKEN, OTHER_TOKEN, "not-a-registered-token"):
            r = await client.post("/mcp", json=rpc_call("list_servers", {}), headers={**MCP_HEADERS, **bearer(token)})
            assert r.status_code == 503 and r.headers["retry-after"] == "5"
        r = await client.get("/results/q3Zr0V2m1yX8kD5sWb7aLg/1001_normal.log", headers=bearer(VALID_TOKEN))
        assert r.status_code == 503
    assert env.db.calls == 0
    with pytest.raises(FileNotFoundError):
        env.audit_records()


@pytest.mark.asyncio
async def test_cancel_calls_cancels_in_flight_search(served, env) -> None:
    mcp, app = served
    env.remote.block = asyncio.Event()
    req = tool_request(app, "search_logs", SEARCH_ARGS)
    await asyncio.wait_for(env.remote.started.wait(), WAIT_S)

    assert await mcp.cancel_calls(WAIT_S) == 1
    assert env.remote.cancelled.is_set()  # cancel_calls 返回前远端已被取消、审计已写完
    await req.finish()
    assert req.status == 200
    assert error_of(req.json()) == {"code": "INTERNAL_ERROR", "message": "服务正在停机，调用已取消，请稍后重试"}
    assert_cancelled_and_cleaned(env, mcp)

    late = tool_request(app, "list_servers", {})
    await late.finish()
    assert late.status == 503


@pytest.mark.asyncio
async def test_sigterm_handler_cancels_calls_then_exits(served, env) -> None:
    mcp, app = served
    server = LogViewerServer(uvicorn.Config(app, lifespan="off"), mcp, shutdown_grace_s=WAIT_S)
    env.remote.block = asyncio.Event()
    req = tool_request(app, "search_logs", SEARCH_ARGS)
    await asyncio.wait_for(env.remote.started.wait(), WAIT_S)

    server.handle_exit(signal.SIGTERM, None)
    # 信号处理函数只置标志：新请求立即 503，uvicorn 等停机协程取消完调用后才退出。
    assert mcp.shutting_down and not server.should_exit
    async with asyncio.timeout(WAIT_S):
        while not server.should_exit:
            await asyncio.sleep(0.01)
    assert not server.force_exit
    assert server._captured_signals == [signal.SIGTERM]  # 退出时照常重新发出
    assert env.remote.cancelled.is_set()
    await req.finish()
    assert error_of(req.json())["message"] == "服务正在停机，调用已取消，请稍后重试"
    assert_cancelled_and_cleaned(env, mcp)


@pytest.mark.asyncio
async def test_repeated_signal_forces_exit(served, env) -> None:
    mcp, app = served
    server = LogViewerServer(uvicorn.Config(app, lifespan="off"), mcp)
    server.handle_exit(signal.SIGTERM, None)
    assert not server.should_exit
    server.handle_exit(signal.SIGTERM, None)
    assert server.should_exit and server.force_exit
    await asyncio.sleep(0.05)  # 停机协程看到 force_exit 后直接结束
    assert server._shutdown_task is not None and server._shutdown_task.done()


def test_server_without_mcp_behaves_like_uvicorn() -> None:
    server = LogViewerServer(uvicorn.Config(lambda s, r, x: None))
    server.handle_exit(signal.SIGTERM, None)  # 不在事件循环中
    assert server.should_exit and not server.force_exit
