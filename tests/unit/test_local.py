"""Local_MCP 启动配置、工具转发、错误映射与调用时限的单元测试（任务 10.1、10.2，需求 1.8、4.1、4.5、4.6、4.9、
4.10、10.5）。

超时与取消用 StubService（真实 TCP 上的最小跳板机替身）确认 Local_MCP 关闭了连接。

转发经 httpx.ASGITransport 打到进程内组装好的跳板机应用（build_asgi_app，替身管理库与远端沿用 test_app.py 的
Env），由跳板机的审计记录确认收到的原始参数与使用者。另有一个经 stdio 启动 ``python -m log_viewer_mcp.local``
的测试，确认标准输出只有 MCP 消息。
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import os
import pathlib
import subprocess
import sys
import time
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult

import log_viewer_mcp.local.main as local_main
from log_viewer_mcp.common.tool_docs import LIST_SERVERS_DESCRIPTION, SEARCH_LOGS_DESCRIPTION
from log_viewer_mcp.local.main import (
    DEFAULT_CALL_TIMEOUT_S,
    DEFAULT_RESULT_TTL_H,
    LEGACY_ENV_NAME,
    Environment,
    LocalMCP,
    LocalSettings,
    SettingsError,
    configure_logging,
    load_settings,
    main,
)
from log_viewer_mcp.server.app import build_asgi_app, build_tools, create_mcp
from tests.unit.test_app import AT, Env, accept_all
from tests.unit.test_app_http import REVOKED_TOKEN, TOKENS, VALID_TOKEN, lifespan

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVICE_URL = "http://testserver"
# 只有一套环境（旧配置）时的环境名，结果下载到 query_log/<ENV>/<handle>/。
ENV = LEGACY_ENV_NAME


def settings(tmp_path, token: str = VALID_TOKEN, url: str = SERVICE_URL, call_timeout_s: float = 10.0) -> LocalSettings:
    env = Environment(name=ENV, description="测试用跳板机", service_url=url, token=token)
    return LocalSettings(envs=(env,), result_dir=tmp_path / "query_log", call_timeout_s=call_timeout_s,
                         result_ttl_h=24.0)


def payload(result: CallToolResult) -> dict:
    (content,) = result.content
    data = json.loads(content.text)
    assert result.structuredContent == data
    return data


# ---------------------------------------------------------------------------
# 启动配置（4.9）
# ---------------------------------------------------------------------------


def test_load_settings_defaults_and_relative_result_dir(tmp_path) -> None:
    s = load_settings({"LOG_MCP_SERVICE_URL": "http://10.0.0.5:8080/", "LOG_MCP_TOKEN": "tok-123"}, cwd=tmp_path,
                      home=tmp_path)
    (env,) = s.envs
    assert env.name == "default" and s.envs_file is None and s.project_file is None
    assert env.service_url == "http://10.0.0.5:8080"
    assert env.mcp_url == "http://10.0.0.5:8080/mcp"
    assert env.results_url == "http://10.0.0.5:8080/results"
    assert env.token == "tok-123"
    assert s.result_dir == tmp_path / "query_log"
    assert s.call_timeout_s == DEFAULT_CALL_TIMEOUT_S
    assert s.result_ttl_h == DEFAULT_RESULT_TTL_H
    assert "tok-123" not in repr(s)

    s = load_settings(
        {"LOG_MCP_SERVICE_URL": "https://jump.example:8443/base", "LOG_MCP_TOKEN": "t",
         "LOG_MCP_RESULT_DIR": os.path.join("out", "logs"), "LOG_MCP_CALL_TIMEOUT_S": "30",
         "LOG_MCP_RESULT_TTL_H": "0.5"},
        cwd=tmp_path, home=tmp_path,
    )
    assert s.envs[0].mcp_url == "https://jump.example:8443/base/mcp"
    assert s.result_dir == tmp_path / "out" / "logs"
    assert s.result_dir.is_absolute()
    assert (s.call_timeout_s, s.result_ttl_h) == (30.0, 0.5)


def test_load_settings_reports_all_missing_required(tmp_path) -> None:
    with pytest.raises(SettingsError) as e:
        load_settings({"LOG_MCP_SERVICE_URL": "", "LOG_MCP_RESULT_DIR": "x"}, cwd=tmp_path, home=tmp_path)
    text = str(e.value)
    assert "LOG_MCP_SERVICE_URL" in text and "LOG_MCP_TOKEN" in text


@pytest.mark.parametrize(
    "overrides, variable",
    [
        ({"LOG_MCP_SERVICE_URL": "ftp://host"}, "LOG_MCP_SERVICE_URL"),
        ({"LOG_MCP_SERVICE_URL": "host:8080"}, "LOG_MCP_SERVICE_URL"),
        ({"LOG_MCP_SERVICE_URL": "http://user:pw@host"}, "LOG_MCP_SERVICE_URL"),
        ({"LOG_MCP_SERVICE_URL": "http://host/?q=1"}, "LOG_MCP_SERVICE_URL"),
        ({"LOG_MCP_SERVICE_URL": "http://host:99999"}, "LOG_MCP_SERVICE_URL"),
        ({"LOG_MCP_TOKEN": "has space"}, "LOG_MCP_TOKEN"),
        ({"LOG_MCP_TOKEN": "secret\n"}, "LOG_MCP_TOKEN"),
        ({"LOG_MCP_TOKEN": "令牌"}, "LOG_MCP_TOKEN"),
        ({"LOG_MCP_CALL_TIMEOUT_S": "0"}, "LOG_MCP_CALL_TIMEOUT_S"),
        ({"LOG_MCP_CALL_TIMEOUT_S": "abc"}, "LOG_MCP_CALL_TIMEOUT_S"),
        ({"LOG_MCP_RESULT_TTL_H": "nan"}, "LOG_MCP_RESULT_TTL_H"),
        ({"LOG_MCP_RESULT_TTL_H": "-1"}, "LOG_MCP_RESULT_TTL_H"),
    ],
)
def test_load_settings_rejects_invalid_values_without_echoing_them(tmp_path, overrides, variable) -> None:
    environ = {"LOG_MCP_SERVICE_URL": "http://host:8080", "LOG_MCP_TOKEN": "good-token", **overrides}
    with pytest.raises(SettingsError) as e:
        load_settings(environ, cwd=tmp_path, home=tmp_path)
    text = str(e.value)
    assert variable in text
    assert overrides[variable] not in text


def test_main_without_required_env_exits_nonzero_and_hides_token(tmp_path, capsys) -> None:
    secret = "very-secret-token-value"
    code = main({"LOG_MCP_TOKEN": secret}, home=tmp_path)
    out, err = capsys.readouterr()
    assert code != 0
    assert out == ""
    assert "LOG_MCP_SERVICE_URL" in err
    assert secret not in err


def test_module_entry_exits_nonzero_with_message_on_stderr(tmp_path) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("LOG_MCP_")}
    env.update(PYTHONPATH=str(REPO_ROOT), HOME=str(tmp_path), USERPROFILE=str(tmp_path))  # 不读研发自己的 envs.toml
    proc = subprocess.run(
        [sys.executable, "-m", "log_viewer_mcp.local"], cwd=tmp_path, env=env, capture_output=True, timeout=60,
    )
    assert proc.returncode != 0
    assert proc.stdout == b""
    stderr = proc.stderr.decode("utf-8", "replace")
    assert "LOG_MCP_SERVICE_URL" in stderr and "LOG_MCP_TOKEN" in stderr


def test_configure_logging_writes_stderr_stream_and_redacts_token() -> None:
    root = logging.getLogger()
    saved = (root.handlers[:], root.level, logging.getLogger("httpx").level, logging.getLogger("httpcore").level)
    stream = io.StringIO()
    secret = "redact-me-Token_42"
    try:
        configure_logging(secret, stream=stream)
        log = logging.getLogger("log_viewer_mcp.local.test")
        log.info("header Authorization: Bearer %s", secret)
        try:
            raise RuntimeError(f"boom {secret}")
        except RuntimeError:
            log.exception("failed")
        assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    finally:
        root.handlers[:] = saved[0]
        root.setLevel(saved[1])
        logging.getLogger("httpx").setLevel(saved[2])
        logging.getLogger("httpcore").setLevel(saved[3])
    text = stream.getvalue()
    assert "Bearer ***" in text and "boom ***" in text
    assert secret not in text


def test_local_package_runs_no_external_commands() -> None:
    """4.10：Local_MCP 的代码不启动外部命令。"""
    package_dir = pathlib.Path(local_main.__file__).parent
    for path in package_dir.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        for needle in ("subprocess", "os.system", "os.popen", "create_subprocess", "os.exec", "os.spawn"):
            assert needle not in source, (path.name, needle)


# ---------------------------------------------------------------------------
# 工具说明（1.8）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tools_match_server_with_string_log_type(tmp_path) -> None:
    local = LocalMCP(settings(tmp_path))
    tools = {t.name: t for t in await local.list_tools()}
    assert set(tools) == {"search_logs", "list_servers"}
    assert tools["search_logs"].description == SEARCH_LOGS_DESCRIPTION
    assert tools["list_servers"].description == LIST_SERVERS_DESCRIPTION

    server_tools = {t.name: t for t in build_tools(Env(tmp_path).config)}
    for name, tool in tools.items():
        assert tool.title == server_tools[name].title
        assert tool.description == server_tools[name].description
        assert tool.annotations == server_tools[name].annotations
        assert tool.outputSchema is None

    schema = tools["search_logs"].inputSchema
    assert schema["properties"]["log_type"]["type"] == "string"
    assert "enum" not in schema["properties"]["log_type"]
    # 比跳板机多一个 env（只有一套环境时可省略）；转发前去掉。
    assert set(schema["properties"]) == set(server_tools["search_logs"].parameters["properties"]) | {"env"}
    assert schema["properties"]["env"]["enum"] == [ENV]
    assert schema["required"] == ["svr_ids", "log_type"]
    assert set(tools["list_servers"].inputSchema["properties"]) == {"env"}
    assert "required" not in tools["list_servers"].inputSchema


# ---------------------------------------------------------------------------
# 转发（4.1）
# ---------------------------------------------------------------------------


class RecordingTransport(httpx.AsyncBaseTransport):
    """记录每个请求（方法、路径、Authorization 头、JSON-RPC 方法）后交给内层传输。"""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner
        self.requests: list[tuple[str, str, str | None, str | None]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        method = None
        if body:
            with contextlib.suppress(ValueError):
                method = json.loads(body).get("method")
        self.requests.append((request.method, request.url.path, request.headers.get("authorization"), method))
        return await self.inner.handle_async_request(request)


@pytest_asyncio.fixture
async def served(tmp_path) -> AsyncIterator[tuple[Env, RecordingTransport]]:
    env = Env(tmp_path)
    config = env.config_with_tokens(TOKENS)
    mcp = create_mcp(config, env.services, env.audit, regex_checker=accept_all)
    app = build_asgi_app(mcp)
    async with lifespan(app):
        yield env, RecordingTransport(httpx.ASGITransport(app=app))


def audit_lines(env: Env) -> list[dict]:
    try:
        return env.audit_records()
    except FileNotFoundError:
        return []


@pytest.mark.asyncio
async def test_list_servers_forwarded_with_bearer_token(served, tmp_path) -> None:
    env, transport = served
    local = LocalMCP(settings(tmp_path), transport=transport)
    result = await local.call_tool("list_servers", {})
    assert result.isError is False
    assert payload(result) == {
        "env": ENV,
        "server_timezone": "GMT-2",
        "log_types": {name: t.dir for name, t in env.config.log_types.items()},
        "servers": [{"svr_id": 1001, "svr_name": "S1", "zone": "eu", "ip_inner": "10.0.1.11", "is_test": 0}],
    }
    (record,) = audit_lines(env)
    assert record["user"] == "zhangsan" and record["tool"] == "list_servers"
    assert transport.requests
    for method, path, authorization, _ in transport.requests:
        assert (method, path, authorization) == ("POST", "/mcp", f"Bearer {VALID_TOKEN}")


RAW_ARGUMENTS = [
    {"svr_ids": [1001, True], "log_type": "normal"},
    {"svr_ids": [1001], "log_type": "normal", "at_time": 1789668000.0},
    {"svr_ids": ["1; rm -rf /"], "log_type": "normal"},
    {"svr_ids": [1001], "log_type": "nope"},
    {"svr_ids": [1001], "log_type": "normal", "path": "/etc/passwd"},
    {"svr_ids": [1001], "log_type": "normal", "start_time": None, "include_patterns": ["a\nb"]},
]


@pytest.mark.parametrize("arguments", RAW_ARGUMENTS)
@pytest.mark.asyncio
async def test_raw_arguments_reach_server_unchanged(served, tmp_path, arguments) -> None:
    """Local_MCP 不做类型转换、不丢参数：跳板机按原始字典校验并拒绝，审计中的参数与 Agent 发来的相同。"""
    env, transport = served
    local = LocalMCP(settings(tmp_path), transport=transport)
    result = await local.call_tool("search_logs", dict(arguments))
    assert result.isError is True
    assert payload(result)["error"]["code"] == "INVALID_ARGUMENT"
    (record,) = audit_lines(env)
    assert record["params"] == arguments
    assert type(record["params"]["svr_ids"][-1]) is type(arguments["svr_ids"][-1])
    if "at_time" in arguments:
        assert isinstance(record["params"]["at_time"], float)
    assert env.db.calls == 0 and env.connector.calls == 0


@pytest.mark.asyncio
async def test_each_call_opens_a_new_session(served, tmp_path) -> None:
    env, transport = served
    local = LocalMCP(settings(tmp_path), transport=transport)
    for _ in range(2):
        result = await local.call_tool("list_servers", {})
        assert result.isError is False
    rpc_methods = [m for _, _, _, m in transport.requests if m is not None]
    assert rpc_methods.count("initialize") == 2
    assert rpc_methods.count("tools/call") == 2
    assert "tools/list" not in rpc_methods


@pytest.mark.asyncio
async def test_search_logs_downloads_result_and_deletes_it_on_server(served, tmp_path) -> None:
    """4.2、4.3：经跳板机的 /results/ 下载到 <Local_Result_Dir>/<handle>/，摘要给出 local_path，随后 DELETE。"""
    env, transport = served
    local = LocalMCP(settings(tmp_path), transport=transport)
    result = await local.call_tool("search_logs", {"svr_ids": [1001], "log_type": "normal", "at_time": AT})
    assert result.isError is False
    data = payload(result)
    (entry,) = data["servers"]
    assert entry["svr_id"] == 1001 and entry["status"] == "ok" and entry["error"] is None
    assert "file" not in entry
    handle = data["result_handle"]
    local_path = pathlib.Path(entry["local_path"])
    assert local_path.is_absolute()
    assert data["env"] == ENV
    assert local_path == tmp_path / "query_log" / ENV / handle / "1001_normal.log"
    content = local_path.read_bytes()
    assert len(content) == entry["bytes"] > 0
    assert not local_path.with_name(local_path.name + ".part").exists()
    assert not os.path.exists(env.store.handle_dir(handle))
    requests = [(m, p) for m, p, _, _ in transport.requests if p != "/mcp"]
    assert requests == [("GET", f"/results/{handle}/1001_normal.log"), ("DELETE", f"/results/{handle}")]
    text = result.content[0].text
    assert "/results/" not in text and "testserver" not in text
    assert content.decode() not in text


@pytest.mark.asyncio
async def test_unreachable_service_returns_service_unavailable(tmp_path, caplog) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    secret = "unreachable-token-xyz"
    local = LocalMCP(settings(tmp_path, token=secret), transport=httpx.MockTransport(refuse))
    with caplog.at_level(logging.DEBUG):
        result = await local.call_tool("list_servers", {})
    assert result.isError is True
    assert payload(result)["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert secret not in caplog.text
    assert secret not in result.content[0].text


# ---------------------------------------------------------------------------
# 错误映射与调用时限（4.5、4.6、10.5）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("token", [REVOKED_TOKEN, "not-a-registered-token-Q9"])
@pytest.mark.asyncio
async def test_http_401_returns_auth_failed_without_token(served, tmp_path, caplog, token) -> None:
    """跳板机的认证中间件以 401 拒绝无效或已吊销的 token → AUTH_FAILED，说明 token 无效或已吊销。"""
    env, transport = served
    local = LocalMCP(settings(tmp_path, token=token), transport=transport)
    with caplog.at_level(logging.DEBUG):
        result = await local.call_tool("list_servers", {})
    assert result.isError is True
    error = payload(result)["error"]
    assert error["code"] == "AUTH_FAILED"
    assert "token 无效或已吊销" in error["message"]
    assert token not in result.content[0].text
    assert token not in caplog.text
    assert audit_lines(env) == []


@pytest.mark.parametrize("status", [503, 500])
@pytest.mark.asyncio
async def test_http_error_status_returns_service_unavailable(tmp_path, status) -> None:
    def reject(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="unavailable")

    local = LocalMCP(settings(tmp_path), transport=httpx.MockTransport(reject))
    result = await local.call_tool("search_logs", {"svr_ids": [1001], "log_type": "normal"})
    assert result.isError is True
    assert payload(result)["error"]["code"] == "SERVICE_UNAVAILABLE"


def test_error_mapping_unwraps_nested_exception_groups() -> None:
    request = httpx.Request("POST", SERVICE_URL + "/mcp")

    def status_error(code: int) -> httpx.HTTPStatusError:
        return httpx.HTTPStatusError("x", request=request, response=httpx.Response(code, request=request))

    nested_401 = ExceptionGroup("outer", [RuntimeError("a"), ExceptionGroup("inner", [status_error(401)])])
    assert local_main._error_for_exception(nested_401).code == "AUTH_FAILED"
    assert local_main._error_for_exception(ExceptionGroup("g", [status_error(503)])).code == "SERVICE_UNAVAILABLE"
    assert local_main._error_for_exception(httpx.ConnectError("refused")).code == "SERVICE_UNAVAILABLE"


class StubService:
    """进程内的最小 HTTP/1.1 跳板机替身，走真实的 TCP 连接。

    initialize 按 initialize_delay 延迟答复，通知答 202，tools/call 一直不答复；记录 tools/call 所在的连接
    何时被 Local_MCP 关闭（即断开能传到跳板机）。
    """

    def __init__(self, initialize_delay: float = 0.0) -> None:
        self.initialize_delay = initialize_delay
        self.call_received = asyncio.Event()
        self.call_connection_closed = asyncio.Event()
        self.url = ""
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    async def __aenter__(self) -> StubService:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        assert self._server is not None
        self._server.close()
        # 关闭仍打开的连接（Server.close_clients 要到 Python 3.13 才有，最低支持 3.11）
        for writer in list(self._writers):
            writer.close()
        await asyncio.wait_for(self._server.wait_closed(), 5)

    @staticmethod
    async def _respond(writer: asyncio.StreamWriter, status: int, body: bytes = b"") -> None:
        head = (
            f"HTTP/1.1 {status} X\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n"
        ).encode()
        writer.write(head + body)
        await writer.drain()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        waiting_call = False
        self._writers.add(writer)
        try:
            while True:
                head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
                headers = dict(
                    (k.strip().lower(), v.strip()) for k, v in (line.split(":", 1) for line in head.split("\r\n")[1:]
                                                               if ":" in line)
                )
                body = await reader.readexactly(int(headers.get("content-length", "0")))
                message = json.loads(body) if body else {}
                if message.get("method") == "initialize":
                    await asyncio.sleep(self.initialize_delay)
                    result = {"protocolVersion": message["params"]["protocolVersion"], "capabilities": {},
                              "serverInfo": {"name": "stub", "version": "0"}}
                    reply = {"jsonrpc": "2.0", "id": message["id"], "result": result}
                    await self._respond(writer, 200, json.dumps(reply).encode())
                elif "id" not in message:
                    await self._respond(writer, 202)
                else:
                    waiting_call = True
                    self.call_received.set()
                    while await reader.read(65536):  # 不答复，直到对端关闭连接
                        pass
                    return
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            if waiting_call:
                self.call_connection_closed.set()
            self._writers.discard(writer)
            writer.close()


@pytest.mark.asyncio
async def test_call_timeout_counts_from_call_start_and_closes_connection(tmp_path, caplog) -> None:
    """initialize 用掉 0.6 秒，tools/call 不答复：1 秒的 Service_Call_Timeout 从收到调用起算。"""
    secret = "timeout-token-Zz_9"
    async with StubService(initialize_delay=0.6) as service:
        local = LocalMCP(settings(tmp_path, token=secret, url=service.url, call_timeout_s=1.0))
        started = time.monotonic()
        with caplog.at_level(logging.DEBUG):
            result = await local.call_tool("list_servers", {})
        elapsed = time.monotonic() - started
        assert service.call_received.is_set()
        await asyncio.wait_for(service.call_connection_closed.wait(), 5)
    assert result.isError is True
    assert payload(result)["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert 0.9 <= elapsed < 1.5, elapsed
    assert secret not in caplog.text and secret not in result.content[0].text


@pytest.mark.asyncio
async def test_cancellation_propagates_and_closes_connection(tmp_path) -> None:
    """被 Agent 取消（低层服务端取消处理任务）时不转为错误结果，并关闭到跳板机的连接。"""
    async with StubService() as service:
        local = LocalMCP(settings(tmp_path, url=service.url))
        task = asyncio.create_task(local.call_tool("list_servers", {}))
        await asyncio.wait_for(service.call_received.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(service.call_connection_closed.wait(), 5)


# ---------------------------------------------------------------------------
# stdio（标准输出只有 MCP 消息）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stdio_lists_tools_and_reports_unreachable_service(tmp_path) -> None:
    secret = "stdio-secret-token"
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "log_viewer_mcp.local"],
        env={
            "PYTHONPATH": str(REPO_ROOT),
            "HOME": str(tmp_path),
            "USERPROFILE": str(tmp_path),
            "LOG_MCP_SERVICE_URL": "http://127.0.0.1:9",
            "LOG_MCP_TOKEN": secret,
            "LOG_MCP_CALL_TIMEOUT_S": "5",
        },
        cwd=str(tmp_path),
    )
    errlog = tmp_path / "stderr.txt"
    with open(errlog, "w", encoding="utf-8") as err:
        async with stdio_client(params, errlog=err) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = {t.name: t for t in (await session.list_tools()).tools}
                assert tools["search_logs"].description == SEARCH_LOGS_DESCRIPTION
                result = await session.call_tool("list_servers", {})
    assert result.isError is True
    assert payload(result)["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert secret not in errlog.read_text(encoding="utf-8", errors="replace")
    assert not (tmp_path / "query_log").exists()
