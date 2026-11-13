"""认证中间件、/results/ 路由与启动入口的单元测试（任务 8.6）。

经 httpx 的 ASGITransport 向组装好的 ASGI 应用（build_asgi_app）发 HTTP 请求：lifespan 在单独的任务中运行
SDK 的会话管理器，/mcp 由 SDK 的 Streamable HTTP 传输处理，再到 LogViewerMCP.call_tool。替身管理库与远端
执行沿用 test_app.py 的 Env；Result_Store 与 AuditLog 为真实实现（临时目录）。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio

from log_viewer_mcp.server import app as app_module
from log_viewer_mcp.server.app import BearerAuthMiddleware, build_asgi_app, create_mcp, main, parse_args
from log_viewer_mcp.server.config import TokenRecord
from tests.unit.test_app import AT, Env, accept_all

VALID_TOKEN = "valid-token-Abc_123-xyz"
REVOKED_TOKEN = "revoked-token-Def_456-uvw"
OTHER_TOKEN = "other-token-Ghi_789-rst"
HANDLE = "q3Zr0V2m1yX8kD5sWb7aLg"

MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def record(token: str, *, token_id: str, user: str, revoked: bool = False) -> TokenRecord:
    return TokenRecord(token_id, user, hashlib.sha256(token.encode()).hexdigest(), "2026-09-01T00:00:00-02:00", revoked)


TOKENS = (
    record(REVOKED_TOKEN, token_id="t0", user="lisi", revoked=True),
    record(VALID_TOKEN, token_id="t1", user="zhangsan"),
    record(OTHER_TOKEN, token_id="t2", user="wangwu"),
)


@pytest.fixture
def env(tmp_path) -> Env:
    return Env(tmp_path)


@contextlib.asynccontextmanager
async def lifespan(app) -> AsyncIterator[None]:
    """像 uvicorn 一样在单独的任务中经认证中间件运行 ASGI lifespan（会话管理器与后台清理）。"""
    to_app: asyncio.Queue = asyncio.Queue()
    from_app: asyncio.Queue = asyncio.Queue()
    scope = {"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}}
    task = asyncio.create_task(app(scope, to_app.get, from_app.put))
    await to_app.put({"type": "lifespan.startup"})
    assert (await from_app.get())["type"] == "lifespan.startup.complete"
    try:
        yield
    finally:
        await to_app.put({"type": "lifespan.shutdown"})
        assert (await from_app.get())["type"] == "lifespan.shutdown.complete"
        await task


@pytest_asyncio.fixture
async def client(env) -> AsyncIterator[httpx.AsyncClient]:
    config = env.config_with_tokens(TOKENS)
    mcp = create_mcp(config, env.services, env.audit, regex_checker=accept_all)
    app = build_asgi_app(mcp)
    async with lifespan(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
            yield c


def bearer(token: str, scheme: str = "Bearer") -> dict[str, str]:
    return {"Authorization": f"{scheme} {token}"}


def rpc_call(name: str, arguments: dict) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}


async def call_tool(client: httpx.AsyncClient, name: str, arguments: dict, token: str = VALID_TOKEN,
                    scheme: str = "Bearer") -> httpx.Response:
    return await client.post("/mcp", json=rpc_call(name, arguments), headers={**MCP_HEADERS, **bearer(token, scheme)})


def audit_lines(env: Env) -> list[dict]:
    try:
        return env.audit_records()
    except FileNotFoundError:
        return []


# ---------------------------------------------------------------------------
# 401
# ---------------------------------------------------------------------------

BAD_AUTH = {
    "missing": {},
    "wrong_scheme": {"Authorization": f"Basic {VALID_TOKEN}"},
    "no_token": {"Authorization": "Bearer"},
    "double_space": {"Authorization": f"Bearer  {VALID_TOKEN}"},
    "trailing_text": {"Authorization": f"Bearer {VALID_TOKEN} x"},
    "invalid": bearer("not-a-registered-token"),
    "revoked": bearer(REVOKED_TOKEN),
}

REQUESTS = {
    "mcp": ("POST", "/mcp"),
    "download": ("GET", f"/results/{HANDLE}/1001_normal.log"),
    "delete": ("DELETE", f"/results/{HANDLE}"),
    "unknown_path": ("GET", "/nothing"),
}


@pytest.mark.parametrize("auth", BAD_AUTH.values(), ids=BAD_AUTH.keys())
@pytest.mark.parametrize("request_line", REQUESTS.values(), ids=REQUESTS.keys())
@pytest.mark.asyncio
async def test_requests_without_valid_token_get_401(client, env, auth, request_line) -> None:
    method, path = request_line
    kwargs = {"json": rpc_call("list_servers", {})} if method == "POST" else {}
    response = await client.request(method, path, headers={**MCP_HEADERS, **auth}, **kwargs)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert env.db.calls == 0
    assert audit_lines(env) == []


def test_duplicate_authorization_headers_rejected() -> None:
    mw = BearerAuthMiddleware(lambda s, r, x: None, TOKENS)
    good = (b"authorization", f"Bearer {VALID_TOKEN}".encode())
    assert mw.authenticate([good]) == ("zhangsan", VALID_TOKEN)
    assert mw.authenticate([good, good]) is None
    assert mw.authenticate([(b"Authorization", f"bEaReR {OTHER_TOKEN}".encode())]) == ("wangwu", OTHER_TOKEN)
    assert mw.authenticate([(b"authorization", f"Bearer {REVOKED_TOKEN}".encode())]) is None


# ---------------------------------------------------------------------------
# 有效 token：MCP 调用与 /results/
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_logs_over_http_audits_user_and_results_downloadable(client, env) -> None:
    response = await call_tool(client, "search_logs", {"svr_ids": [1001], "log_type": "normal", "at_time": AT})
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is False
    summary = result["structuredContent"]
    handle = summary["result_handle"]
    (server,) = summary["servers"]
    assert server["status"] == "ok" and server["file"] == "1001_normal.log"

    # 中间件设置的 contextvars 到达 SDK 中运行工具的任务：审计记录中有使用者。
    (rec,) = env.audit_records()
    assert rec["user"] == "zhangsan" and rec["tool"] == "search_logs" and rec["status"] == "ok"

    download = await client.get(f"/results/{handle}/1001_normal.log", headers=bearer(VALID_TOKEN))
    assert download.status_code == 200
    assert len(download.content) == server["bytes"] and download.content.count(b"\n") == server["lines"]

    assert (await client.delete(f"/results/{handle}", headers=bearer(OTHER_TOKEN))).status_code == 204
    assert (await client.get(f"/results/{handle}/1001_normal.log", headers=bearer(VALID_TOKEN))).status_code == 404


@pytest.mark.asyncio
async def test_scheme_is_case_insensitive_and_user_follows_token(client, env) -> None:
    response = await call_tool(client, "list_servers", {}, token=OTHER_TOKEN, scheme="bearer")
    assert response.status_code == 200
    assert response.json()["result"]["structuredContent"]["servers"][0]["svr_id"] == 1001
    (rec,) = env.audit_records()
    assert rec["user"] == "wangwu" and rec["tool"] == "list_servers"


@pytest.mark.asyncio
async def test_token_never_logged(client, env, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    await client.get(f"/results/{HANDLE}/1001_normal.log", headers=bearer(REVOKED_TOKEN))
    await client.get(f"/results/{HANDLE}/1001_normal.log", headers=bearer("guess-" + VALID_TOKEN))
    await call_tool(client, "search_logs", {"svr_ids": [1001], "log_type": "normal", "at_time": AT,
                                      "include_patterns": [VALID_TOKEN]})
    await call_tool(client, "search_logs", {"svr_ids": ["x"], "log_type": "normal"})
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "认证失败" in text
    for secret in (VALID_TOKEN, REVOKED_TOKEN):
        assert secret not in text
        assert secret not in env.audit_text()


# ---------------------------------------------------------------------------
# 启动入口
# ---------------------------------------------------------------------------


def test_config_option_required(capsys) -> None:
    with pytest.raises(SystemExit) as e:
        parse_args([])
    assert e.value.code == 2
    assert parse_args(["-c", "a.toml"]).config == "a.toml"
    assert parse_args(["--config", "b.toml"]).config == "b.toml"


def test_main_refuses_to_start_with_missing_config(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "configure_logging", lambda: None)  # 不改动测试进程的根 logger
    with pytest.raises(SystemExit) as e:
        main(["-c", str(tmp_path / "missing.toml")])
    assert e.value.code == 2
    assert "启动失败" in capsys.readouterr().err
