"""Local_MCP 下载结果、DELETE 与路径限定的单元测试（任务 10.3，需求 4.2、4.3、4.4、4.7）。

直接把跳板机的摘要交给 ``LocalMCP._handle_result``，/results/ 由 httpx.MockTransport 应答并记录请求。
端到端（经进程内跳板机应用）的下载与 DELETE 见 test_local.py。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib

import anyio
import httpx
import pytest
from mcp.types import CallToolResult, TextContent

from log_viewer_mcp.local.main import LocalMCP
from tests.unit.test_local import payload, settings

HANDLE = "q3Zr0V2m1yX8kD5sWb7aLg"
TOKEN = "download-test-token-Q7"
CONTENTS = {
    "1001_normal.log": b"2026-09-17 16:00:01,000 | INFO  | t | CMD | x\n" * 3,
    "1002_normal.log": "2026-09-17 16:00:02,000 | INFO  | t | CMD | 中文\n".encode(),
    "1003_normal.log": b"",
}


def ok_entry(svr_id: int, file: str | None = None, size: int | None = None) -> dict:
    file = f"{svr_id}_normal.log" if file is None else file
    return {"svr_id": svr_id, "status": "ok", "error": None, "file": file,
            "bytes": len(CONTENTS.get(file, b"")) if size is None else size, "lines": 1,
            "files_read": ["smartfox.log"], "files_skipped": [], "truncated": "none", "available_range": None}


FAILED_ENTRY = {"svr_id": 1009, "status": "failed", "error": {"code": "SSH_FAILED", "message": "无法登录游戏服或连接中断"},
                "file": None, "bytes": 0, "lines": 0, "files_read": [], "files_skipped": [], "truncated": "none",
                "available_range": None}


def summary(servers: list, handle: object = HANDLE) -> CallToolResult:
    data = {"result_handle": handle, "time_range": {"start": 1, "end": 2, "start_server": "a", "end_server": "b"},
            "server_timezone": "GMT-2", "servers": servers}
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(data, ensure_ascii=False))],
                          structuredContent=data, isError=False)


class FakeResults:
    """/results/ 的替身：GET 按 CONTENTS（或 overrides）应答，DELETE 返回 delete_status；记录每个请求。"""

    def __init__(self, overrides: dict | None = None, delete_status: int = 204) -> None:
        self.overrides = overrides or {}
        self.delete_status = delete_status
        self.requests: list[tuple[str, str, str | None]] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path, request.headers.get("authorization")))
        if request.method == "DELETE":
            return httpx.Response(self.delete_status)
        name = request.url.path.rsplit("/", 1)[-1]
        override = self.overrides.get(name)
        if callable(override):
            return await override(request)
        if isinstance(override, httpx.Response):
            return override
        if override is not None:
            return httpx.Response(200, content=override)
        if name in CONTENTS:
            return httpx.Response(200, content=CONTENTS[name])
        return httpx.Response(404)

    @property
    def paths(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p, _ in self.requests]


def make_local(tmp_path, fake: FakeResults, call_timeout_s: float = 10.0) -> LocalMCP:
    return LocalMCP(settings(tmp_path, token=TOKEN, call_timeout_s=call_timeout_s),
                    transport=httpx.MockTransport(fake))


async def handle(local: LocalMCP, result: CallToolResult, name: str = "search_logs",
                 deadline: float | None = None) -> CallToolResult:
    if deadline is None:
        deadline = anyio.current_time() + local.local_settings.call_timeout_s
    return await local._handle_result(name, result, deadline)


def tree(root: pathlib.Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")}


def assert_download_failed(entry: dict) -> None:
    assert entry["status"] == "failed"
    assert entry["error"]["code"] == "DOWNLOAD_FAILED"
    assert entry["local_path"] is None and "file" not in entry
    assert entry["bytes"] == 0 and entry["lines"] == 0


@pytest.mark.asyncio
async def test_downloads_all_files_then_deletes_handle(tmp_path, caplog) -> None:
    fake = FakeResults()
    local = make_local(tmp_path, fake)
    servers = [ok_entry(1001), FAILED_ENTRY, ok_entry(1002), ok_entry(1003)]
    with caplog.at_level(logging.DEBUG):
        result = await handle(local, summary(servers))
    assert result.isError is False
    data = payload(result)
    assert data["result_handle"] == HANDLE and data["server_timezone"] == "GMT-2"
    out = data["servers"]
    assert [e["svr_id"] for e in out] == [1001, 1009, 1002, 1003]
    handle_dir = tmp_path / "query_log" / HANDLE
    for entry in (out[0], out[2], out[3]):
        assert entry["status"] == "ok" and "file" not in entry
        path = pathlib.Path(entry["local_path"])
        assert path.is_absolute() and path.parent == handle_dir
        assert path.read_bytes() == CONTENTS[path.name]
        assert entry["bytes"] == len(CONTENTS[path.name])
    # 字段顺序不变，只把 file 换成 local_path
    assert list(out[0]) == [("local_path" if k == "file" else k) for k in ok_entry(1001)]
    assert out[1] == {**{("local_path" if k == "file" else k): v for k, v in FAILED_ENTRY.items()}}
    assert tree(handle_dir) == set(CONTENTS)
    assert fake.paths == [
        ("GET", f"/results/{HANDLE}/1001_normal.log"),
        ("GET", f"/results/{HANDLE}/1002_normal.log"),
        ("GET", f"/results/{HANDLE}/1003_normal.log"),
        ("DELETE", f"/results/{HANDLE}"),
    ]
    assert all(auth == f"Bearer {TOKEN}" for _, _, auth in fake.requests)
    text = result.content[0].text
    assert "/results/" not in text and "testserver" not in text and "CMD" not in text
    assert TOKEN not in text and TOKEN not in caplog.text


@pytest.mark.parametrize("body", [b"short", CONTENTS["1001_normal.log"] + b"extra"])
@pytest.mark.asyncio
async def test_byte_count_mismatch_removes_partial_file(tmp_path, body) -> None:
    """4.4：字节数与摘要不符时删除已下载的部分并标 DOWNLOAD_FAILED，其余文件照常处理，最后仍 DELETE。"""
    fake = FakeResults({"1001_normal.log": body})
    local = make_local(tmp_path, fake)
    data = payload(await handle(local, summary([ok_entry(1001), ok_entry(1002)])))
    assert_download_failed(data["servers"][0])
    assert data["servers"][1]["status"] == "ok"
    assert tree(tmp_path / "query_log" / HANDLE) == {"1002_normal.log"}
    assert fake.paths[-1] == ("DELETE", f"/results/{HANDLE}")


@pytest.mark.parametrize(
    "response",
    [httpx.Response(404), httpx.Response(500, content=b"oops"),
     "connect-error"],
)
@pytest.mark.asyncio
async def test_download_error_marks_download_failed_and_still_deletes(tmp_path, response) -> None:
    async def failing(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("connection reset", request=request)

    fake = FakeResults({"1001_normal.log": failing if response == "connect-error" else response})
    local = make_local(tmp_path, fake)
    data = payload(await handle(local, summary([ok_entry(1001), ok_entry(1002)])))
    assert_download_failed(data["servers"][0])
    assert data["servers"][1]["status"] == "ok"
    assert tree(tmp_path / "query_log" / HANDLE) == {"1002_normal.log"}
    assert fake.paths[-1] == ("DELETE", f"/results/{HANDLE}")


@pytest.mark.asyncio
async def test_error_mid_stream_removes_part_file(tmp_path) -> None:
    async def broken_stream():
        yield b"partial"
        raise httpx.ReadError("connection reset")

    fake = FakeResults({"1001_normal.log": httpx.Response(200, content=broken_stream())})
    local = make_local(tmp_path, fake)
    data = payload(await handle(local, summary([ok_entry(1001)])))
    assert_download_failed(data["servers"][0])
    assert tree(tmp_path / "query_log" / HANDLE) == set()
    assert fake.paths[-1] == ("DELETE", f"/results/{HANDLE}")


@pytest.mark.parametrize("bad_handle", ["../../../../evil_handle_x", "q3Zr0V2m1yX8kD5sWb7aL", "q3Zr0V2m1yX8kD5sWb7aL/",
                                        "q3Zr0V2m1yX8kD5sWb7aL\n", None, 123, "C:\\Windows\\system32\\xx"])
@pytest.mark.asyncio
async def test_invalid_handle_touches_nothing(tmp_path, bad_handle) -> None:
    """4.7：handle 不合法时不发请求、不创建任何目录，成功的 svr_id 都标 DOWNLOAD_FAILED。"""
    fake = FakeResults()
    local = make_local(tmp_path, fake)
    data = payload(await handle(local, summary([ok_entry(1001), FAILED_ENTRY], handle=bad_handle)))
    assert_download_failed(data["servers"][0])
    assert data["servers"][1]["error"]["code"] == "SSH_FAILED"
    assert fake.requests == []
    assert tree(tmp_path) == set()


@pytest.mark.parametrize("bad_file", ["../1001_normal.log", "..\\1001_normal.log", "1001_normal.log/../../x.log",
                                      "/etc/1001_normal.log", "C:1001_normal.log", "1001_normal.log\n", "",
                                      "１001_normal.log", "1001_Normal.log", None])
@pytest.mark.asyncio
async def test_invalid_file_name_is_not_downloaded(tmp_path, bad_file) -> None:
    fake = FakeResults()
    local = make_local(tmp_path, fake)
    entry = {**ok_entry(1001, size=3), "file": bad_file}
    data = payload(await handle(local, summary([entry, ok_entry(1002)])))
    assert_download_failed(data["servers"][0])
    assert data["servers"][1]["status"] == "ok"
    assert fake.paths == [("GET", f"/results/{HANDLE}/1002_normal.log"), ("DELETE", f"/results/{HANDLE}")]
    assert tree(tmp_path) == {"query_log", os.path.join("query_log", HANDLE),
                              os.path.join("query_log", HANDLE, "1002_normal.log")}


@pytest.mark.parametrize("bad_bytes", [-1, 1.0, True, "3", None])
@pytest.mark.asyncio
async def test_invalid_byte_count_is_not_downloaded(tmp_path, bad_bytes) -> None:
    fake = FakeResults()
    local = make_local(tmp_path, fake)
    entry = {**ok_entry(1001), "bytes": bad_bytes}
    data = payload(await handle(local, summary([entry])))
    assert_download_failed(data["servers"][0])
    assert fake.paths == [("DELETE", f"/results/{HANDLE}")]
    assert tree(tmp_path) == set()


@pytest.mark.asyncio
async def test_handle_dir_symlink_outside_result_dir_is_refused(tmp_path) -> None:
    """realpath 不在 Local_Result_Dir 之内（handle 目录是指向外面的符号链接）时不写入。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    result_dir = tmp_path / "query_log"
    result_dir.mkdir()
    try:
        os.symlink(outside, result_dir / HANDLE, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不能创建符号链接")
    fake = FakeResults()
    local = make_local(tmp_path, fake)
    data = payload(await handle(local, summary([ok_entry(1001)])))
    assert_download_failed(data["servers"][0])
    assert list(outside.iterdir()) == []
    assert fake.paths == [("DELETE", f"/results/{HANDLE}")]


@pytest.mark.asyncio
async def test_deadline_passed_marks_downloads_failed_and_still_deletes(tmp_path) -> None:
    """下载与等待结果共用 deadline：到时未完成的文件标 DOWNLOAD_FAILED，DELETE 另限时仍会发出。"""
    started = asyncio.Event()

    async def slow(request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.sleep(30)
        return httpx.Response(200, content=CONTENTS["1002_normal.log"])

    fake = FakeResults({"1002_normal.log": slow})
    local = make_local(tmp_path, fake)
    deadline = anyio.current_time() + 0.5
    data = payload(await handle(local, summary([ok_entry(1001), ok_entry(1002), ok_entry(1003)]), deadline=deadline))
    assert started.is_set()
    assert data["servers"][0]["status"] == "ok"
    assert_download_failed(data["servers"][1])
    assert_download_failed(data["servers"][2])
    assert tree(tmp_path / "query_log" / HANDLE) == {"1001_normal.log"}
    assert fake.paths[-1] == ("DELETE", f"/results/{HANDLE}")


@pytest.mark.parametrize("delete_status", [404, 500, "error"])
@pytest.mark.asyncio
async def test_delete_failure_does_not_change_summary(tmp_path, delete_status) -> None:
    fake = FakeResults(delete_status=500 if delete_status == "error" else delete_status)
    if delete_status == "error":
        inner = fake.__call__

        async def raising(request: httpx.Request) -> httpx.Response:
            if request.method == "DELETE":
                raise httpx.ConnectError("refused", request=request)
            return await inner(request)

        local = LocalMCP(settings(tmp_path, token=TOKEN), transport=httpx.MockTransport(raising))
    else:
        local = make_local(tmp_path, fake)
    data = payload(await handle(local, summary([ok_entry(1001)])))
    assert data["servers"][0]["status"] == "ok"
    assert pathlib.Path(data["servers"][0]["local_path"]).read_bytes() == CONTENTS["1001_normal.log"]


@pytest.mark.asyncio
async def test_cancellation_removes_part_file_and_propagates(tmp_path) -> None:
    first_chunk_written = asyncio.Event()

    async def stream():
        yield b"first chunk"
        first_chunk_written.set()
        await asyncio.sleep(30)
        yield b"never"

    fake = FakeResults({"1001_normal.log": httpx.Response(200, content=stream())})
    local = make_local(tmp_path, fake)
    task = asyncio.create_task(handle(local, summary([ok_entry(1001, size=100)])))
    await asyncio.wait_for(first_chunk_written.wait(), 5)
    part = tmp_path / "query_log" / HANDLE / "1001_normal.log.part"
    assert part.exists()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert tree(tmp_path / "query_log" / HANDLE) == set()


@pytest.mark.asyncio
async def test_list_servers_and_error_results_pass_through(tmp_path) -> None:
    fake = FakeResults()
    local = make_local(tmp_path, fake)
    servers = summary([ok_entry(1001)])
    result = await handle(local, servers, name="list_servers")
    assert result.structuredContent == servers.structuredContent and result.isError is False

    error = {"error": {"code": "BUSY", "message": "查询并发已满，请稍后重试"}}
    error_result = CallToolResult(content=[TextContent(type="text", text=json.dumps(error, ensure_ascii=False))],
                                  structuredContent=error, isError=True)
    result = await handle(local, error_result)
    assert result.isError is True and payload(result) == error
    assert fake.requests == []
    assert tree(tmp_path) == set()


@pytest.mark.asyncio
async def test_malformed_summary_returns_service_unavailable(tmp_path) -> None:
    fake = FakeResults()
    local = make_local(tmp_path, fake)
    bad = CallToolResult(content=[TextContent(type="text", text="not json")], isError=False)
    result = await handle(local, bad)
    assert result.isError is True and payload(result)["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert fake.requests == [] and tree(tmp_path) == set()
