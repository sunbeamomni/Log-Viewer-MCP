"""search_logs 与 list_servers 编排的单元测试（任务 8.1）：替身管理库、SSH 建连与远端执行，真实的 Result_Store。

覆盖摘要字段、单服失败隔离、Time_Range 内没有文件时的 available_range、扫描上限、整次错误
（DB_UNAVAILABLE、RESULT_STORE_FAILED、INTERNAL_ERROR）与 list_servers。
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from log_viewer_mcp.common.errors import ErrorCode, LogViewerError, error_dict
from log_viewer_mcp.server.config import (
    AdminDbConfig,
    AuditConfig,
    Config,
    Limits,
    ListenConfig,
    LogTypeConfig,
    ResultsConfig,
    SshConfig,
)
from log_viewer_mcp.server.db import ServerLookup
from log_viewer_mcp.server.remote import (
    ArchiveEntry,
    FileOutcome,
    FileState,
    FileStatus,
    Listing,
    PlainEntry,
    QueryOutcome,
    RemoteExit,
    build_listing_script,
)
from log_viewer_mcp.server.results import ResultStore, ResultStoreError
from log_viewer_mcp.server.tools import SearchLogsRun, ToolServices, list_servers, search_logs
from log_viewer_mcp.server.validate import SearchParams

TZ = timezone(timedelta(hours=-2))
LOGS = "/usr/local/ds/SFS2X/logs"
ZIP = "/usr/local/ds/SFS2X/ziplogs"
OK_STATUS = FileStatus("F", 0, (0,))


def server_time(*args: int) -> int:
    return int(datetime(*args, tzinfo=TZ).timestamp())


NOW = server_time(2026, 9, 17, 16, 12, 30)

IP_A = "10.0.1.11"
IP_B = "10.0.1.12"
IP_DOWN = "10.0.1.99"

LOOKUPS = {
    1001: ServerLookup(1001, IP_A, None),
    1002: ServerLookup(1002, IP_B, None),
    1004: ServerLookup(1004, IP_DOWN, None),
    1005: ServerLookup(1005, None, ErrorCode.INVALID_SERVER_ADDRESS),
}

# 15 点的小时压缩包、16:00 的分钟文件与 Current（16:01 起）
LISTING = Listing(
    plain=(
        PlainEntry("smartfox.log.2026-09-17-16-00", 11, 300),
        PlainEntry("smartfox.log", 12, 400),
        PlainEntry("gc.log", 13, 50),
    ),
    archives=(ArchiveEntry("smartfox.log.2026-09-17-15.tar.gz", 200),),
    ionice=True,
)
SELECTED = ["smartfox.log.2026-09-17-15.tar.gz", "smartfox.log.2026-09-17-16-00", "smartfox.log"]


def make_config(tmp_path, **limits) -> Config:
    return Config(
        listen=ListenConfig("127.0.0.1", 8080),
        server_timezone="-02:00",
        tz=TZ,
        tokens_file="tokens.json",
        tokens=(),
        admin_db=AdminDbConfig("10.0.0.2", 3306, "logview", "admin", "db_password", "pw"),
        ssh=SshConfig("root", 22, "id_ed25519", "known_hosts", 5, b""),
        zip_dir=ZIP,
        limits=replace(Limits(), **limits),
        results=ResultsConfig(dir=str(tmp_path / "results")),
        audit=AuditConfig(dir=str(tmp_path / "audit")),
        log_types={"normal": LogTypeConfig("normal", LOGS, "")},
    )


class FakeDb:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.lookup_calls: list[tuple[int, ...]] = []

    async def lookup_servers(self, svr_ids):
        self.lookup_calls.append(tuple(svr_ids))
        if self.fail:
            raise LogViewerError(ErrorCode.DB_UNAVAILABLE)
        return {i: LOOKUPS.get(i, ServerLookup(i, None, ErrorCode.SERVER_NOT_FOUND)) for i in svr_ids}

    async def list_servers(self):
        if self.fail:
            raise LogViewerError(ErrorCode.DB_UNAVAILABLE)
        return [{"svr_id": 1001, "svr_name": "S1", "zone": "eu", "ip_inner": IP_A, "is_test": 0}]


class FakeConn:
    def __init__(self, host: str) -> None:
        self.host = host
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeConnector:
    def __init__(self) -> None:
        self.conns: list[FakeConn] = []

    async def connect(self, ip: str) -> FakeConn:
        if ip == IP_DOWN:
            raise LogViewerError(ErrorCode.SSH_FAILED)
        conn = FakeConn(ip)
        self.conns.append(conn)
        return conn


class FakeRemote:
    """① 与 ② 的替身：listings 按主机给出 Listing 或异常；② 为每个文件写一行 "<文件名> line"。"""

    def __init__(self, listings=None, query_errors=None) -> None:
        self.listings = listings or {}
        self.query_errors = query_errors or {}
        self.listing_calls: list[tuple[str, str, int]] = []
        self.queries: list[tuple[str, object]] = []

    async def run_listing(self, conn, script, *, seconds):
        self.listing_calls.append((conn.host, script, seconds))
        result = self.listings.get(conn.host, LISTING)
        if isinstance(result, BaseException):
            raise result
        return result

    async def run_query(self, conn, query, sink, *, deadline):
        self.queries.append((conn.host, query))
        error = self.query_errors.get(conn.host)
        if callable(error):
            await error()
        elif error is not None:
            raise error
        outcomes = []
        for f in query.files:
            sink.begin_file(f.index, f)
            sink.write(f.index, f.name.encode() + b" line\n")
            sink.end_file(f.index, OK_STATUS)
            outcomes.append(FileOutcome(f, FileState.READ, OK_STATUS))
        return QueryOutcome(tuple(outcomes), True, False, 1234, RemoteExit(0, None, 0))


@pytest.fixture
def env(tmp_path):
    config = make_config(tmp_path)
    store = ResultStore.from_config(config.results)
    store.prepare()
    remote = FakeRemote()
    services = ToolServices(
        config=config, db=FakeDb(), connector=FakeConnector(), store=store, clock=lambda: NOW,
        run_listing=remote.run_listing, run_query=remote.run_query,
    )
    return services, remote


def params(svr_ids, **kwargs) -> SearchParams:
    kwargs.setdefault("at_time", server_time(2026, 9, 17, 16, 1))
    return SearchParams(svr_ids=tuple(svr_ids), log_type="normal", **kwargs)


def handle_files(store: ResultStore, handle: str) -> dict[str, bytes]:
    d = store.handle_dir(handle)
    return {n: open(os.path.join(d, n), "rb").read() for n in sorted(os.listdir(d))}


# ---------------------------------------------------------------------------
# 摘要与单服失败隔离
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_shape_and_failure_isolation(env) -> None:
    services, remote = env
    result = await search_logs(services, params([1001, 1004, 9999, 1005], include_patterns=("CMD",)))
    assert not result.is_error and result.status == "ok"
    s = result.payload
    handle = s["result_handle"]
    assert handle == result.result_handle and len(handle) == 22
    window = server_time(2026, 9, 17, 16, 1)
    assert s["time_range"] == {
        "start": window - 300, "end": window + 300,
        "start_server": "2026-09-17 15:56:00", "end_server": "2026-09-17 16:06:00",
    }
    assert s["server_timezone"] == "GMT-2"
    expected_content = b"".join(n.encode() + b" line\n" for n in SELECTED)
    assert s["servers"] == [
        {"svr_id": 1001, "status": "ok", "error": None, "file": "1001_normal.log",
         "bytes": len(expected_content), "lines": 3, "files_read": SELECTED, "files_skipped": [],
         "truncated": "none", "available_range": None},
        {"svr_id": 1004, "status": "failed", "error": error_dict(ErrorCode.SSH_FAILED), "file": None,
         "bytes": 0, "lines": 0, "files_read": [], "files_skipped": [], "truncated": "none",
         "available_range": None},
        {"svr_id": 9999, "status": "failed", "error": error_dict(ErrorCode.SERVER_NOT_FOUND), "file": None,
         "bytes": 0, "lines": 0, "files_read": [], "files_skipped": [], "truncated": "none",
         "available_range": None},
        {"svr_id": 1005, "status": "failed", "error": error_dict(ErrorCode.INVALID_SERVER_ADDRESS), "file": None,
         "bytes": 0, "lines": 0, "files_read": [], "files_skipped": [], "truncated": "none",
         "available_range": None},
    ]
    assert handle_files(services.store, handle) == {"1001_normal.log": expected_content}

    # 一次管理库查询；SERVER_NOT_FOUND、INVALID_SERVER_ADDRESS 不发起 SSH；连接都已关闭
    assert services.db.lookup_calls == [(1001, 1004, 9999, 1005)]
    assert [c.host for c in services.connector.conns] == [IP_A]
    assert all(c.closed for c in services.connector.conns)
    host, query = remote.queries[0]
    assert host == IP_A and [f.name for f in query.files] == SELECTED
    assert "-e 'CMD'" in query.script and "ionice -c 3" in query.command

    # 审计信息
    listing_script = build_listing_script(LOGS, ZIP)
    targets = {t.svr_id: t for t in result.targets}
    assert targets[1001].ip_inner == IP_A and targets[1001].status == "ok"
    assert list(targets[1001].scripts) == [listing_script, query.script]
    assert (targets[1004].ip_inner, list(targets[1004].scripts), targets[1004].status) == (IP_DOWN, [], "SSH_FAILED")
    assert (targets[9999].ip_inner, targets[9999].status) == (None, "SERVER_NOT_FOUND")
    assert (targets[1005].ip_inner, targets[1005].status) == (None, "INVALID_SERVER_ADDRESS")


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [ErrorCode.LOG_DIR_UNAVAILABLE, ErrorCode.REMOTE_TOOL_UNAVAILABLE,
                                  ErrorCode.HOST_KEY_MISMATCH])
async def test_listing_error_only_affects_that_server(env, code) -> None:
    services, remote = env
    remote.listings[IP_B] = LogViewerError(code)
    result = await search_logs(services, params([1001, 1002]))
    servers = {x["svr_id"]: x for x in result.payload["servers"]}
    assert servers[1001]["status"] == "ok"
    assert servers[1002]["status"] == "failed" and servers[1002]["error"] == error_dict(code)
    assert list(handle_files(services.store, result.result_handle)) == ["1001_normal.log"]
    assert all(c.closed for c in services.connector.conns)


@pytest.mark.asyncio
async def test_without_zip_dir_reads_plain_files_only(env) -> None:
    services, remote = env
    services.config = replace(services.config, zip_dir=None)
    remote.listings[IP_A] = replace(LISTING, archives=())
    result = await search_logs(services, params([1001]))
    server = result.payload["servers"][0]
    assert server["status"] == "ok" and server["files_read"] == SELECTED[1:]
    host, script, _ = remote.listing_calls[0]
    assert script == build_listing_script(LOGS, None)
    assert "Z=('' '')" in remote.queries[0][1].script


@pytest.mark.asyncio
async def test_per_type_zip_dir_is_used(env) -> None:
    services, remote = env
    own = "/data/normal_zip"
    services.config = replace(services.config, zip_dir=None,
                              log_types={"normal": LogTypeConfig("normal", LOGS, "", zip_dir=own)})
    result = await search_logs(services, params([1001]))
    assert result.payload["servers"][0]["files_read"] == SELECTED
    assert remote.listing_calls[0][1] == build_listing_script(LOGS, own)
    assert f"'{own}/smartfox.log.2026-09-17-15.tar.gz'" in remote.queries[0][1].script


@pytest.mark.asyncio
async def test_query_error_removes_that_servers_file(env) -> None:
    services, remote = env
    remote.query_errors[IP_B] = LogViewerError(ErrorCode.TIMEOUT)
    result = await search_logs(services, params([1001, 1002]))
    servers = {x["svr_id"]: x for x in result.payload["servers"]}
    assert servers[1002]["error"] == error_dict(ErrorCode.TIMEOUT) and servers[1002]["file"] is None
    assert list(handle_files(services.store, result.result_handle)) == ["1001_normal.log"]
    t = {t.svr_id: t for t in result.targets}[1002]
    assert len(t.scripts) == 2 and t.status == "TIMEOUT"


@pytest.mark.asyncio
async def test_unexpected_exception_is_internal_error_without_details(env, caplog) -> None:
    services, remote = env
    remote.listings[IP_B] = RuntimeError("secret detail 10.9.9.9")
    result = await search_logs(services, params([1001, 1002]))
    servers = {x["svr_id"]: x for x in result.payload["servers"]}
    assert servers[1001]["status"] == "ok"
    assert servers[1002]["error"] == error_dict(ErrorCode.INTERNAL_ERROR)
    assert "secret detail" not in repr(result.payload)
    assert "secret detail" in caplog.text  # 只写运行日志


# ---------------------------------------------------------------------------
# 没有选中文件、扫描上限
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_files_in_range_gives_available_range_and_empty_file(env) -> None:
    services, remote = env
    remote.listings[IP_A] = replace(LISTING, plain=LISTING.plain[:1])  # 没有 Current
    result = await search_logs(services, params([1001], at_time=server_time(2026, 9, 17, 10, 0)))
    (s,) = result.payload["servers"]
    assert s["status"] == "ok" and s["file"] == "1001_normal.log" and (s["bytes"], s["lines"]) == (0, 0)
    assert s["files_read"] == [] and s["truncated"] == "none"
    assert s["available_range"] == {
        "earliest": server_time(2026, 9, 17, 15), "latest": server_time(2026, 9, 17, 16, 1),
        "earliest_server": "2026-09-17 15:00:00", "latest_server": "2026-09-17 16:01:00",
    }
    assert remote.queries == []  # 不执行 ②
    assert handle_files(services.store, result.result_handle) == {"1001_normal.log": b""}
    assert len({t.svr_id: t for t in result.targets}[1001].scripts) == 1


@pytest.mark.asyncio
async def test_only_current_has_unbounded_earliest(env) -> None:
    services, remote = env
    remote.listings[IP_A] = replace(LISTING, plain=(LISTING.plain[1],), archives=())
    result = await search_logs(services, params([1001], at_time=None, start_time=NOW + 3600, end_time=NOW + 7200))
    (s,) = result.payload["servers"]
    assert s["available_range"] == {"earliest": None, "latest": NOW, "earliest_server": None,
                                    "latest_server": "2026-09-17 16:12:30"}


@pytest.mark.asyncio
async def test_scan_limit_skips_later_files(env) -> None:
    services, remote = env
    services.config = replace(services.config, limits=replace(services.config.limits, max_scan_bytes=600))
    result = await search_logs(services, params([1001]))
    (s,) = result.payload["servers"]
    assert s["files_read"] == SELECTED[:2] and s["files_skipped"] == SELECTED[2:]
    assert s["truncated"] == "scan_limit"


# ---------------------------------------------------------------------------
# 整次错误
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_db_unavailable_is_whole_call_error(env) -> None:
    services, _ = env
    services.db.fail = True
    result = await search_logs(services, params([1001, 1002]))
    assert result.is_error and result.status == "DB_UNAVAILABLE"
    assert result.payload == {"error": error_dict(ErrorCode.DB_UNAVAILABLE)}
    assert result.result_handle is None and os.listdir(services.store.directory) == []
    assert services.connector.conns == []


@pytest.mark.asyncio
async def test_invalid_time_range_does_not_touch_db(env) -> None:
    services, _ = env
    result = await search_logs(services, params([1001], at_time=None, start_time=NOW + 10))
    assert result.is_error and result.status == "INVALID_ARGUMENT"
    assert services.db.lookup_calls == []


@pytest.mark.asyncio
async def test_result_store_failure_cancels_other_servers(env) -> None:
    services, remote = env
    cancelled = asyncio.Event()

    async def hang() -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def fail() -> None:
        await asyncio.sleep(0.01)
        services.store.discard(handle_holder[0])  # 与 results.py 写入失败时的行为一致
        raise ResultStoreError()

    handle_holder: list[str] = []
    original = services.store.create_handle

    def create_handle() -> str:
        handle_holder.append(original())
        return handle_holder[0]

    services.store.create_handle = create_handle
    remote.query_errors[IP_A] = hang
    remote.query_errors[IP_B] = fail
    result = await search_logs(services, params([1001, 1002]))
    assert result.is_error and result.payload == {"error": error_dict(ErrorCode.RESULT_STORE_FAILED)}
    assert cancelled.is_set()
    assert os.listdir(services.store.directory) == []
    assert all(c.closed for c in services.connector.conns)
    statuses = {t.svr_id: t.status for t in result.targets}
    assert statuses == {1001: "cancelled", 1002: "RESULT_STORE_FAILED"}


@pytest.mark.asyncio
async def test_cancelled_call_discards_results_and_keeps_audit_targets(env) -> None:
    services, remote = env
    started = asyncio.Event()

    async def hang() -> None:
        started.set()
        await asyncio.sleep(3600)

    remote.query_errors[IP_A] = hang
    run = SearchLogsRun(services, params([1001]))
    task = asyncio.create_task(run.run())
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert os.listdir(services.store.directory) == []
    (t,) = run.audit_targets
    assert t.status == "cancelled" and len(t.scripts) == 2
    assert all(c.closed for c in services.connector.conns)


# ---------------------------------------------------------------------------
# list_servers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_servers(env) -> None:
    services, _ = env
    result = await list_servers(services)
    assert not result.is_error
    assert result.payload == {"servers": [{"svr_id": 1001, "svr_name": "S1", "zone": "eu", "ip_inner": IP_A,
                                           "is_test": 0}]}
    services.db.fail = True
    result = await list_servers(services)
    assert result.is_error and result.payload == {"error": error_dict(ErrorCode.DB_UNAVAILABLE)}
