"""服务入口的单元测试（任务 8.8，需求 5.2、6.1、6.4、6.5、10.5、10.6）。

经 build_asgi_app 组装的完整应用（认证中间件 + /mcp + /results/ + lifespan）补充 test_app_http.py 与
test_app_shutdown.py 之外的情形：

- 401：token 不在 Authorization 头中（查询串、其他头）、已吊销的 token 不能删除结果、401 答复不回显 token；
- /results/ 的 404：认证通过后，格式不符、不存在与多余路径段都返回 404，且不影响已有结果；
- AUDIT_FAILED：经 HTTP 的 search_logs、list_servers 与被拒绝的调用；本次结果已删除、下载返回 404；
- 停机期间 503：没有 token 也返回 503（不是 401），DELETE 不执行；
- 断开：list_servers 进行中断开同样被取消；请求体未读完就断开时不执行工具；
- 运行日志与审计中没有 token：覆盖 401、404、503、断开取消与 AUDIT_FAILED 各路径。

替身管理库与远端执行沿用 test_app.py 的 Env；Result_Store 与 AuditLog 为真实实现（临时目录）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio

from log_viewer_mcp.server.app import LogViewerMCP, build_asgi_app, create_mcp
from tests.unit.test_app import AT, Env, accept_all
from tests.unit.test_app_http import (
    MCP_HEADERS,
    OTHER_TOKEN,
    REVOKED_TOKEN,
    TOKENS,
    VALID_TOKEN,
    bearer,
    lifespan,
    rpc_call,
)
from tests.unit.test_app_shutdown import WAIT_S, RawRequest, error_of, lingering_tasks, tool_request

SEARCH_ARGS = {"svr_ids": [1001], "log_type": "normal", "at_time": AT}
RESULT_FILE = "1001_normal.log"
SECRETS = (VALID_TOKEN, REVOKED_TOKEN, OTHER_TOKEN)


class Served:
    """一个运行中的完整应用（lifespan 已启动）及其 httpx 客户端。"""

    def __init__(self, env: Env, mcp: LogViewerMCP, app, client: httpx.AsyncClient) -> None:
        self.env = env
        self.mcp = mcp
        self.app = app
        self.client = client
        self.handles: list[str] = []  # 本测试中新建的 Result_Handle（含随后被删除的）
        create = env.store.create_handle

        def recording_create_handle() -> str:
            handle = create()
            self.handles.append(handle)
            return handle

        env.store.create_handle = recording_create_handle  # type: ignore[method-assign]

    async def call(self, name: str, arguments: dict, token: str = VALID_TOKEN) -> httpx.Response:
        return await self.client.post(
            "/mcp", json=rpc_call(name, arguments), headers={**MCP_HEADERS, **bearer(token)},
        )

    async def search(self, **extra) -> dict:
        response = await self.call("search_logs", {**SEARCH_ARGS, **extra})
        assert response.status_code == 200
        return response.json()["result"]

    def results_entries(self) -> list[str]:
        return os.listdir(self.env.config.results.dir)

    def audit_records(self) -> list[dict]:
        try:
            return self.env.audit_records()
        except FileNotFoundError:
            return []


@contextlib.asynccontextmanager
async def serve(env: Env) -> AsyncIterator[Served]:
    mcp = create_mcp(env.config_with_tokens(TOKENS), env.services, env.audit, regex_checker=accept_all)
    app = build_asgi_app(mcp)
    async with lifespan(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
            yield Served(env, mcp, app, c)


@pytest_asyncio.fixture
async def served(tmp_path) -> AsyncIterator[Served]:
    async with serve(Env(tmp_path)) as s:
        yield s


@pytest_asyncio.fixture
async def audit_broken(tmp_path) -> AsyncIterator[Served]:
    """审计目录是一个普通文件：每次写审计都失败（6.5）。"""
    not_dir = tmp_path / "audit_is_file"
    not_dir.write_text("x")
    async with serve(Env(tmp_path, audit_dir=str(not_dir))) as s:
        yield s


def block_list_servers(env: Env) -> tuple[asyncio.Event, asyncio.Event]:
    """让替身管理库的 list_servers 阻塞到被取消；返回 (已开始, 已取消) 两个事件。"""
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def list_servers():
        env.db.calls += 1
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    env.db.list_servers = list_servers  # type: ignore[method-assign]
    return started, cancelled


def assert_no_secrets(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text


def server_log_text(caplog) -> str:
    """服务端的运行日志（含异常文本）；排除测试客户端 httpx 自己的请求日志（它记录完整 URL）。"""
    handler = caplog.handler
    return "\n".join(
        handler.format(r) for r in caplog.records if r.name != "httpx" and not r.name.startswith("httpx.")
    )


# ---------------------------------------------------------------------------
# 401（6.1）
# ---------------------------------------------------------------------------

TOKEN_ELSEWHERE = {
    "query_string": ("/mcp?access_token=" + VALID_TOKEN, {}),
    "x_api_token": ("/mcp", {"X-Api-Token": VALID_TOKEN}),
    "cookie": ("/mcp", {"Cookie": f"token={VALID_TOKEN}"}),
    "proxy_authorization": ("/mcp", {"Proxy-Authorization": f"Bearer {VALID_TOKEN}"}),
}


@pytest.mark.parametrize("case", TOKEN_ELSEWHERE.values(), ids=TOKEN_ELSEWHERE.keys())
@pytest.mark.asyncio
async def test_token_outside_authorization_header_is_401(served, case, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    path, headers = case
    response = await served.client.post(path, json=rpc_call("list_servers", {}), headers={**MCP_HEADERS, **headers})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert VALID_TOKEN not in response.text
    assert served.env.db.calls == 0 and served.audit_records() == []
    assert "认证失败" in caplog.text
    assert_no_secrets(server_log_text(caplog))  # 运行日志只记路径（不含查询串）


@pytest.mark.asyncio
async def test_revoked_token_cannot_download_or_delete_results(served) -> None:
    result = await served.search()
    handle = result["structuredContent"]["result_handle"]
    for method, path in (("GET", f"/results/{handle}/{RESULT_FILE}"), ("DELETE", f"/results/{handle}")):
        response = await served.client.request(method, path, headers=bearer(REVOKED_TOKEN))
        assert response.status_code == 401, method
        assert response.content == b"Unauthorized"
    assert served.results_entries() == [handle]  # 结果未被删除
    download = await served.client.get(f"/results/{handle}/{RESULT_FILE}", headers=bearer(VALID_TOKEN))
    assert download.status_code == 200 and download.content


# ---------------------------------------------------------------------------
# /results/ 的 404（5.2）
# ---------------------------------------------------------------------------


def not_found_requests(handle: str) -> list[tuple[str, str]]:
    missing = "B" * 22
    return [
        ("GET", f"/results/{handle}/1002_normal.log"),  # 文件不存在
        ("GET", f"/results/{handle}/1001_login.log"),
        ("GET", f"/results/{missing}/{RESULT_FILE}"),  # handle 不存在
        ("GET", f"/results/{handle[:-1]}/{RESULT_FILE}"),  # handle 格式不符
        ("GET", f"/results/{handle}x/{RESULT_FILE}"),
        ("GET", f"/results/{handle[:-1]}./{RESULT_FILE}"),
        ("GET", f"/results/{handle}/{RESULT_FILE}.bak"),  # 文件名格式不符
        ("GET", f"/results/{handle}/1001_Normal.log"),
        ("GET", f"/results/{handle}/..%2F{RESULT_FILE}"),
        ("GET", f"/results/{handle}/%2e%2e"),
        ("GET", f"/results/{handle}/{RESULT_FILE}/extra"),  # 多余路径段
        ("GET", "/results/"),
        ("GET", "/results"),
        ("DELETE", f"/results/{missing}"),
        ("DELETE", "/results/not-a-handle"),
        ("DELETE", f"/results/{handle}/{RESULT_FILE}/x"),
        ("DELETE", f"/results/{handle[:-1]}%2E"),
    ]


@pytest.mark.asyncio
async def test_results_paths_invalid_or_missing_are_404_after_auth(served, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    result = await served.search()
    handle = result["structuredContent"]["result_handle"]
    for method, path in not_found_requests(handle):
        response = await served.client.request(method, path, headers=bearer(VALID_TOKEN))
        assert response.status_code == 404, (method, path)
    # 404 不影响已有结果
    assert served.results_entries() == [handle]
    download = await served.client.get(f"/results/{handle}/{RESULT_FILE}", headers=bearer(VALID_TOKEN))
    assert download.status_code == 200
    assert len(download.content) == result["structuredContent"]["servers"][0]["bytes"]
    assert_no_secrets(server_log_text(caplog))


@pytest.mark.asyncio
async def test_deleted_handle_is_404_for_any_valid_token(served) -> None:
    handle = (await served.search())["structuredContent"]["result_handle"]
    assert (await served.client.delete(f"/results/{handle}", headers=bearer(VALID_TOKEN))).status_code == 204
    assert served.results_entries() == []
    for token in (VALID_TOKEN, OTHER_TOKEN):
        r = await served.client.get(f"/results/{handle}/{RESULT_FILE}", headers=bearer(token))
        assert r.status_code == 404
        assert (await served.client.delete(f"/results/{handle}", headers=bearer(token))).status_code == 404


# ---------------------------------------------------------------------------
# AUDIT_FAILED（6.5）
# ---------------------------------------------------------------------------

AUDIT_FAILED = {"code": "AUDIT_FAILED", "message": "写入审计记录失败，本次结果已删除"}


@pytest.mark.asyncio
async def test_search_over_http_audit_failed_discards_result(audit_broken, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    s = audit_broken
    result = await s.search(include_patterns=[VALID_TOKEN])
    assert result["isError"] is True
    assert result["structuredContent"] == {"error": AUDIT_FAILED}
    assert json.loads(result["content"][0]["text"]) == {"error": AUDIT_FAILED}
    (handle,) = s.handles  # 本次确实新建过结果，之后被删除
    assert s.results_entries() == []
    r = await s.client.get(f"/results/{handle}/{RESULT_FILE}", headers=bearer(VALID_TOKEN))
    assert r.status_code == 404
    assert s.env.connector.calls == 1  # 查询确实执行过
    assert_no_secrets(server_log_text(caplog))  # 审计失败的运行日志中也没有 token


@pytest.mark.parametrize(
    "name, arguments",
    [
        ("list_servers", {}),
        ("search_logs", {"svr_ids": ["1; rm -rf /"], "log_type": "normal"}),  # 被拒绝的调用也要写审计
        ("no_such_tool", {}),
    ],
    ids=["list_servers", "rejected", "unknown_tool"],
)
@pytest.mark.asyncio
async def test_other_calls_audit_failed(audit_broken, name, arguments) -> None:
    response = await audit_broken.call(name, arguments)
    assert response.status_code == 200
    assert error_of(response.json()) == AUDIT_FAILED
    assert audit_broken.handles == [] and audit_broken.results_entries() == []


# ---------------------------------------------------------------------------
# 停机期间 503（10.6）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_503_even_without_token_and_delete_not_executed(served, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    handle = (await served.search())["structuredContent"]["result_handle"]
    served.mcp.begin_shutdown()
    db_calls = served.env.db.calls
    audit_count = len(served.audit_records())

    requests = [
        ("POST", "/mcp", {}),  # 没有 token：仍是 503，不是 401
        ("POST", "/mcp", bearer(REVOKED_TOKEN)),
        ("GET", f"/results/{handle}/{RESULT_FILE}", {}),
        ("DELETE", f"/results/{handle}", bearer(VALID_TOKEN)),
        ("GET", "/nothing", bearer(VALID_TOKEN)),
    ]
    for method, path, headers in requests:
        kwargs = {"json": rpc_call("list_servers", {})} if method == "POST" else {}
        r = await served.client.request(method, path, headers={**MCP_HEADERS, **headers}, **kwargs)
        assert r.status_code == 503, (method, path)
        assert r.headers["retry-after"] == "5" and "www-authenticate" not in r.headers

    assert served.results_entries() == [handle]  # DELETE 没有执行
    assert served.env.db.calls == db_calls and len(served.audit_records()) == audit_count
    assert "认证失败" not in caplog.text  # 停机期间不做认证
    assert_no_secrets(server_log_text(caplog))


# ---------------------------------------------------------------------------
# 断开（10.5）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disconnect_cancels_running_list_servers(served) -> None:
    started, cancelled = block_list_servers(served.env)
    req = tool_request(served.app, "list_servers", {})
    await asyncio.wait_for(started.wait(), WAIT_S)
    assert served.mcp.active_call_count == 1

    req.disconnect()
    await asyncio.wait_for(cancelled.wait(), WAIT_S)
    await req.finish()
    assert req.status == 200
    assert error_of(req.json()) == {"code": "INTERNAL_ERROR", "message": "调用方已断开，调用已取消"}
    (record,) = served.audit_records()
    assert record["tool"] == "list_servers" and record["status"] == "cancelled"
    assert record["user"] == "zhangsan" and record["targets"] == []
    assert served.mcp.active_call_count == 0 and lingering_tasks() == []


@pytest.mark.asyncio
async def test_disconnect_before_body_complete_runs_no_tool(served) -> None:
    body = json.dumps(rpc_call("search_logs", SEARCH_ARGS)).encode()
    req = RawRequest(served.app, "POST", "/mcp", body)
    # 应用任务尚未运行：把请求体换成前半段（more_body）后紧跟断开。
    while not req.incoming.empty():
        req.incoming.get_nowait()
    req.incoming.put_nowait({"type": "http.request", "body": body[:10], "more_body": True})
    req.disconnect()
    await req.finish()

    assert served.env.db.calls == 0 and served.env.connector.calls == 0
    assert served.audit_records() == [] and served.results_entries() == []
    assert served.mcp.active_call_count == 0 and lingering_tasks() == []


@pytest.mark.asyncio
async def test_disconnect_cancel_keeps_token_out_of_logs_and_audit(served, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    served.env.remote.block = asyncio.Event()  # 永不置位
    req = tool_request(served.app, "search_logs", {**SEARCH_ARGS, "include_patterns": [VALID_TOKEN, "CMD"]})
    await asyncio.wait_for(served.env.remote.started.wait(), WAIT_S)
    req.disconnect()
    await req.finish()

    assert error_of(req.json())["message"] == "调用方已断开，调用已取消"
    assert served.env.remote.cancelled.is_set()
    (record,) = served.audit_records()
    assert record["status"] == "cancelled"
    assert record["params"]["include_patterns"] == ["***", "CMD"]  # 本请求的 token 被掩码
    assert served.results_entries() == []
    assert_no_secrets(served.env.audit_text())
    assert_no_secrets(server_log_text(caplog))
