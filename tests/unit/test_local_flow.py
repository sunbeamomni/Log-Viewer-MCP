"""Local_MCP 整个调用流程的单元测试（任务 10.6，需求 4.2、4.3、4.4、4.5、4.6、4.8、4.9）。

``LocalMCP.call_tool("search_logs", …)`` 经同一个 httpx.MockTransport 访问跳板机替身 FakeService：
/mcp 按 stateless、JSON 响应的 Streamable HTTP 应答 initialize 与 tools/call，/results/ 应答下载与 DELETE。
覆盖下载与 DELETE、字节数不符、401（AUTH_FAILED）、503（SERVICE_UNAVAILABLE），并确认 token 不出现在返回内容
与运行日志中、过期结果目录在同一次查询开始时被清理。

单独的下载分支见 test_local_download.py，清理规则见 test_local_cleanup.py，缺少环境变量见 test_local.py。
"""

from __future__ import annotations

import json
import logging
import pathlib
import time

import httpx
import pytest

from log_viewer_mcp.local.main import LocalMCP
from tests.unit.test_local import payload, settings
from tests.unit.test_local_cleanup import TTL_H, make_dir
from tests.unit.test_local_download import CONTENTS, FAILED_ENTRY, HANDLE, ok_entry

TOKEN = "flow-test-token-Xy_83"
ARGUMENTS = {"svr_ids": [1001, 1009, 1002], "log_type": "normal", "keywords": ["CMD"], "at_time": 1789668000}
EXPIRED_HANDLE = "expiredExpiredExpired_"
FRESH_HANDLE = "freshFreshFreshFresh-1"


def summary_payload(servers: list) -> dict:
    return {"result_handle": HANDLE,
            "time_range": {"start": 1789667400, "end": 1789668600, "start_server": "2026-09-17 15:50:00",
                           "end_server": "2026-09-17 16:10:00"},
            "server_timezone": "GMT-2", "servers": servers}


class FakeService:
    """跳板机替身：校验 Bearer token，/mcp 用 JSON 答复（无会话 ID），/results/ 下载与删除。

    mcp_status 不为 200 时 /mcp 的每个请求都以该状态码拒绝；token 不符时一律 401。
    downloads 覆盖某个结果文件的应答（bytes 为正文，httpx.Response 原样返回）。
    """

    def __init__(self, servers: list, *, mcp_status: int = 200, downloads: dict | None = None) -> None:
        self.tool_result = summary_payload(servers)
        self.mcp_status = mcp_status
        self.downloads = downloads or {}
        self.requests: list[tuple[str, str, str | None]] = []  # (HTTP 方法, 路径, JSON-RPC 方法)
        self.authorizations: list[str | None] = []
        self.call_arguments: list[dict] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        message = json.loads(body) if body else {}
        self.requests.append((request.method, request.url.path, message.get("method")))
        self.authorizations.append(request.headers.get("authorization"))
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"error": "unauthorized"})
        path = request.url.path
        if path == "/mcp":
            return self._mcp(request, message)
        if path.startswith(f"/results/{HANDLE}"):
            return self._results(request, path)
        return httpx.Response(404)

    def _mcp(self, request: httpx.Request, message: dict) -> httpx.Response:
        if self.mcp_status != 200:
            return httpx.Response(self.mcp_status, text="unavailable")
        assert request.method == "POST"
        if "id" not in message:  # notifications/initialized
            return httpx.Response(202)
        if message["method"] == "initialize":
            result = {"protocolVersion": message["params"]["protocolVersion"], "capabilities": {"tools": {}},
                      "serverInfo": {"name": "log-viewer-mcp", "version": "0"}}
        elif message["method"] == "tools/call":
            self.call_arguments.append(message["params"]["arguments"])
            text = json.dumps(self.tool_result, ensure_ascii=False)
            result = {"content": [{"type": "text", "text": text}], "structuredContent": self.tool_result,
                      "isError": False}
        else:  # pragma: no cover - Local_MCP 不应发出其他请求
            raise AssertionError(message["method"])
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": result})

    def _results(self, request: httpx.Request, path: str) -> httpx.Response:
        if request.method == "DELETE" and path == f"/results/{HANDLE}":
            return httpx.Response(204)
        name = path.rsplit("/", 1)[-1]
        if request.method == "GET" and name in CONTENTS:
            override = self.downloads.get(name)
            if isinstance(override, httpx.Response):
                return override
            return httpx.Response(200, content=CONTENTS[name] if override is None else override)
        return httpx.Response(404)

    @property
    def result_requests(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p, _ in self.requests if p != "/mcp"]

    @property
    def rpc_methods(self) -> list[str | None]:
        return [rpc for _, p, rpc in self.requests if p == "/mcp"]


def make_local(tmp_path, service: FakeService, token: str = TOKEN) -> LocalMCP:
    return LocalMCP(settings(tmp_path, token=token), transport=httpx.MockTransport(service))


def tree(root: pathlib.Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*")}


def assert_no_token(token: str, result, caplog) -> None:
    text = result.content[0].text
    assert token not in text
    assert token not in json.dumps(result.structuredContent, ensure_ascii=False)
    assert token not in caplog.text


@pytest.mark.asyncio
async def test_search_logs_flow_downloads_deletes_and_cleans_expired(tmp_path, caplog) -> None:
    """4.2、4.3、4.8：查询开始时清理过期目录；下载到 <Local_Result_Dir>/<handle>/ 后 DELETE；摘要只给本地路径。"""
    now = time.time()
    result_dir = tmp_path / "query_log"
    expired = make_dir(result_dir / EXPIRED_HANDLE, TTL_H + 1, now, {"1001_normal.log": b"old\n"})
    fresh = make_dir(result_dir / FRESH_HANDLE, 1, now, {"1001_normal.log": b"recent\n"})
    service = FakeService([ok_entry(1001), FAILED_ENTRY, ok_entry(1002)])
    local = make_local(tmp_path, service)

    with caplog.at_level(logging.DEBUG):
        result = await local.call_tool("search_logs", dict(ARGUMENTS))

    assert result.isError is False
    data = payload(result)
    assert {k: v for k, v in data.items() if k != "servers"} == {
        k: v for k, v in summary_payload([]).items() if k != "servers"}
    first, failed, second = data["servers"]
    for entry, name in ((first, "1001_normal.log"), (second, "1002_normal.log")):
        assert entry["status"] == "ok" and entry["error"] is None and "file" not in entry
        path = pathlib.Path(entry["local_path"])
        assert path.is_absolute() and path == result_dir / HANDLE / name
        assert path.read_bytes() == CONTENTS[name] and entry["bytes"] == len(CONTENTS[name])
    assert failed["svr_id"] == 1009 and failed["error"]["code"] == "SSH_FAILED" and failed["local_path"] is None

    # 参数原样转发；先 initialize，再 tools/call，之后逐个下载，全部结束后 DELETE。
    assert service.call_arguments == [ARGUMENTS]
    assert service.rpc_methods == ["initialize", "notifications/initialized", "tools/call"]
    assert service.result_requests == [
        ("GET", f"/results/{HANDLE}/1001_normal.log"),
        ("GET", f"/results/{HANDLE}/1002_normal.log"),
        ("DELETE", f"/results/{HANDLE}"),
    ]
    assert service.authorizations == [f"Bearer {TOKEN}"] * len(service.requests)

    # 过期目录被删除，未过期的与本次结果保留；没有遗留 .part。
    assert not expired.exists()
    assert (fresh / "1001_normal.log").read_bytes() == b"recent\n"
    assert tree(result_dir) == {FRESH_HANDLE, f"{FRESH_HANDLE}/1001_normal.log", HANDLE,
                                f"{HANDLE}/1001_normal.log", f"{HANDLE}/1002_normal.log"}

    text = result.content[0].text
    assert "/results/" not in text and "testserver" not in text and "CMD |" not in text
    assert_no_token(TOKEN, result, caplog)


@pytest.mark.parametrize(
    "download",
    [b"short", CONTENTS["1001_normal.log"] + b"extra", httpx.Response(401), httpx.Response(503)],
    ids=["fewer-bytes", "more-bytes", "http-401", "http-503"],
)
@pytest.mark.asyncio
async def test_search_logs_flow_failed_download_marks_download_failed(tmp_path, caplog, download) -> None:
    """4.4：字节数不符或下载被拒时删除已下载部分，该 svr_id 标 DOWNLOAD_FAILED，其余照常，最后仍 DELETE。"""
    service = FakeService([ok_entry(1001), ok_entry(1002)], downloads={"1001_normal.log": download})
    local = make_local(tmp_path, service)

    with caplog.at_level(logging.DEBUG):
        result = await local.call_tool("search_logs", dict(ARGUMENTS))

    assert result.isError is False
    failed, ok = payload(result)["servers"]
    assert failed["status"] == "failed" and failed["error"]["code"] == "DOWNLOAD_FAILED"
    assert failed["local_path"] is None and failed["bytes"] == 0 and failed["lines"] == 0
    assert ok["status"] == "ok"
    assert pathlib.Path(ok["local_path"]).read_bytes() == CONTENTS["1002_normal.log"]
    assert tree(tmp_path / "query_log") == {HANDLE, f"{HANDLE}/1002_normal.log"}
    assert service.result_requests[-1] == ("DELETE", f"/results/{HANDLE}")
    assert_no_token(TOKEN, result, caplog)


@pytest.mark.parametrize(
    "token, mcp_status, code",
    [("revoked-token-Rv_1", 200, "AUTH_FAILED"), (TOKEN, 503, "SERVICE_UNAVAILABLE")],
    ids=["http-401", "http-503"],
)
@pytest.mark.asyncio
async def test_search_logs_flow_rejected_call(tmp_path, caplog, token, mcp_status, code) -> None:
    """4.5、4.6：/mcp 返回 401 → AUTH_FAILED，返回 503 → SERVICE_UNAVAILABLE；不下载、不 DELETE，不写结果目录。"""
    now = time.time()
    expired = make_dir(tmp_path / "query_log" / EXPIRED_HANDLE, TTL_H + 1, now)
    service = FakeService([ok_entry(1001)], mcp_status=mcp_status)
    local = make_local(tmp_path, service, token=token)

    with caplog.at_level(logging.DEBUG):
        result = await local.call_tool("search_logs", dict(ARGUMENTS))

    assert result.isError is True
    error = payload(result)["error"]
    assert error["code"] == code
    if code == "AUTH_FAILED":
        assert "token 无效或已吊销" in error["message"]
    assert service.requests and service.result_requests == []
    assert service.call_arguments == []
    assert not expired.exists()  # 清理在查询开始时进行，与转发结果无关
    assert tree(tmp_path / "query_log") == set()
    assert_no_token(token, result, caplog)
