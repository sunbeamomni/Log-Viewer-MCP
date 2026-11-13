"""并发上限与超时的单元测试（任务 8.2，需求 9.2、9.3）。

ConcurrencyLimiter 本身，以及 search_logs 中的全局 BUSY、单服 BUSY、名额归还与截止时刻。
替身沿用 test_tools.py（管理库、SSH 建连与远端执行），Result_Store 是真实的。
"""

from __future__ import annotations

import asyncio
import os

import pytest

from log_viewer_mcp.common.errors import ErrorCode, LogViewerError, error_dict
from log_viewer_mcp.server.config import Limits
from log_viewer_mcp.server.db import ServerLookup
from log_viewer_mcp.server.limits import ConcurrencyLimiter
from log_viewer_mcp.server.remote import FileOutcome, FileState, QueryOutcome
from log_viewer_mcp.server.results import ResultStore
from log_viewer_mcp.server.tools import SearchLogsRun, ToolServices, list_servers, search_logs
from tests.unit.test_tools import (
    IP_A,
    IP_B,
    LOOKUPS,
    NOW,
    SELECTED,
    FakeConnector,
    FakeDb,
    FakeRemote,
    handle_files,
    make_config,
    params,
)

# 1003 与 1001 在同一台游戏服上（ip_inner 相同）
LOOKUPS_SHARED = {**LOOKUPS, 1003: ServerLookup(1003, IP_A, None)}


class SharedIpDb(FakeDb):
    async def lookup_servers(self, svr_ids):
        self.lookup_calls.append(tuple(svr_ids))
        if self.fail:
            raise LogViewerError(ErrorCode.DB_UNAVAILABLE)
        return {i: LOOKUPS_SHARED.get(i, ServerLookup(i, None, ErrorCode.SERVER_NOT_FOUND)) for i in svr_ids}


def make_services(tmp_path, **limits):
    config = make_config(tmp_path, **limits)
    store = ResultStore.from_config(config.results)
    store.prepare()
    remote = FakeRemote()
    services = ToolServices(
        config=config, db=SharedIpDb(), connector=FakeConnector(), store=store, clock=lambda: NOW,
        run_listing=remote.run_listing, run_query=remote.run_query,
    )
    return services, remote


async def hang_forever() -> None:
    await asyncio.sleep(3600)


# ---------------------------------------------------------------------------
# ConcurrencyLimiter
# ---------------------------------------------------------------------------


def test_limiter_from_limits_and_validation() -> None:
    limiter = ConcurrencyLimiter.from_limits(Limits())
    assert (limiter.max_concurrent, limiter.max_per_server) == (8, 2)
    with pytest.raises(ValueError):
        ConcurrencyLimiter(0, 2)
    with pytest.raises(ValueError):
        ConcurrencyLimiter(8, 0)


@pytest.mark.asyncio
async def test_call_slot_is_non_blocking_and_released_on_exit_error_and_cancel() -> None:
    limiter = ConcurrencyLimiter(2, 2)
    async with limiter.call_slot(), limiter.call_slot():
        assert limiter.active_calls == 2
        with pytest.raises(LogViewerError) as ei:
            async with limiter.call_slot():
                pass
        assert ei.value.code is ErrorCode.BUSY
    assert limiter.active_calls == 0

    with pytest.raises(RuntimeError):
        async with limiter.call_slot():
            raise RuntimeError("boom")
    assert limiter.active_calls == 0

    entered = asyncio.Event()

    async def holder() -> None:
        async with limiter.call_slot():
            entered.set()
            await hang_forever()

    task = asyncio.create_task(holder())
    await entered.wait()
    assert limiter.active_calls == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert limiter.active_calls == 0
    async with limiter.call_slot(), limiter.call_slot():  # 名额都已归还
        pass


def test_server_slot_counts_per_ip() -> None:
    limiter = ConcurrencyLimiter(8, 2)
    with limiter.server_slot(IP_A), limiter.server_slot(IP_A):
        assert limiter.active_on(IP_A) == 2
        with pytest.raises(LogViewerError) as ei, limiter.server_slot(IP_A):
            pass
        assert ei.value.code is ErrorCode.BUSY
        with limiter.server_slot(IP_B):  # 其他游戏服不受影响
            assert limiter.active_on(IP_B) == 1
    assert limiter.active_on(IP_A) == 0 and limiter.active_on(IP_B) == 0

    with pytest.raises(RuntimeError), limiter.server_slot(IP_A):
        raise RuntimeError("boom")
    assert limiter.active_on(IP_A) == 0


def test_services_build_limiter_from_config(tmp_path) -> None:
    services, _ = make_services(tmp_path, max_concurrent=3, max_concurrent_per_server=1)
    assert (services.limiter.max_concurrent, services.limiter.max_per_server) == (3, 1)


# ---------------------------------------------------------------------------
# search_logs：全局 BUSY
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_global_busy_is_whole_call_error_without_db_access(tmp_path) -> None:
    services, remote = make_services(tmp_path, max_concurrent=1)
    started = asyncio.Event()

    async def hang() -> None:
        started.set()
        await hang_forever()

    remote.query_errors[IP_A] = hang
    first = asyncio.create_task(search_logs(services, params([1001])))
    await asyncio.wait_for(started.wait(), 5)

    result = await search_logs(services, params([1002]))
    assert result.is_error and result.status == "BUSY"
    assert result.payload == {"error": error_dict(ErrorCode.BUSY)}
    assert result.result_handle is None and result.targets == ()
    assert services.db.lookup_calls == [(1001,)]  # 第二次调用没有访问管理库

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert services.limiter.active_calls == 0 and services.limiter.active_on(IP_A) == 0

    remote.query_errors.clear()
    result = await search_logs(services, params([1002]))  # 名额已归还
    assert not result.is_error


@pytest.mark.asyncio
async def test_invalid_time_range_is_reported_before_busy(tmp_path) -> None:
    services, _ = make_services(tmp_path, max_concurrent=1)
    async with services.limiter.call_slot():
        result = await search_logs(services, params([1001], at_time=None, start_time=NOW + 10))
    assert result.status == "INVALID_ARGUMENT"


@pytest.mark.asyncio
async def test_list_servers_does_not_use_global_slot(tmp_path) -> None:
    services, _ = make_services(tmp_path, max_concurrent=1)
    async with services.limiter.call_slot():
        result = await list_servers(services)
    assert not result.is_error


# ---------------------------------------------------------------------------
# search_logs：单服 BUSY
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_server_busy_only_affects_that_svr_id(tmp_path) -> None:
    services, _ = make_services(tmp_path, max_concurrent_per_server=1)
    connect = services.connector.connect

    async def slow_connect(ip):  # 建连会让出事件循环，同一次调用中的各 svr_id 同时进行
        await asyncio.sleep(0.01)
        return await connect(ip)

    services.connector.connect = slow_connect
    result = await search_logs(services, params([1001, 1003, 1002]))
    assert not result.is_error
    servers = {s["svr_id"]: s for s in result.payload["servers"]}
    assert servers[1001]["status"] == "ok" and servers[1001]["files_read"] == SELECTED
    assert servers[1002]["status"] == "ok"
    assert servers[1003]["status"] == "failed" and servers[1003]["error"] == error_dict(ErrorCode.BUSY)
    assert servers[1003]["file"] is None
    assert [c.host for c in services.connector.conns] == [IP_A, IP_B]  # 1003 没有发起 SSH
    assert sorted(handle_files(services.store, result.result_handle)) == ["1001_normal.log", "1002_normal.log"]
    t = {t.svr_id: t for t in result.targets}[1003]
    assert (t.ip_inner, t.scripts, t.status) == (IP_A, (), "BUSY")
    assert services.limiter.active_on(IP_A) == 0 and services.limiter.active_on(IP_B) == 0


@pytest.mark.asyncio
async def test_same_ip_svr_ids_both_proceed_within_limit(tmp_path) -> None:
    services, _ = make_services(tmp_path, max_concurrent_per_server=2)
    result = await search_logs(services, params([1001, 1003]))
    assert [s["status"] for s in result.payload["servers"]] == ["ok", "ok"]


@pytest.mark.asyncio
async def test_per_server_slots_are_shared_across_calls(tmp_path) -> None:
    services, _ = make_services(tmp_path)  # 每台 2 个
    with services.limiter.server_slot(IP_A), services.limiter.server_slot(IP_A):
        result = await search_logs(services, params([1001, 1002]))
    servers = {s["svr_id"]: s for s in result.payload["servers"]}
    assert servers[1001]["error"] == error_dict(ErrorCode.BUSY)
    assert servers[1002]["status"] == "ok"


@pytest.mark.asyncio
async def test_slots_released_after_server_failures(tmp_path) -> None:
    services, remote = make_services(tmp_path, max_concurrent=1, max_concurrent_per_server=1)
    remote.listings[IP_B] = LogViewerError(ErrorCode.LOG_DIR_UNAVAILABLE)
    remote.query_errors[IP_A] = RuntimeError("boom")
    result = await search_logs(services, params([1001, 1002, 1004]))
    assert [s["status"] for s in result.payload["servers"]] == ["failed", "failed", "failed"]
    limiter = services.limiter
    assert limiter.active_calls == 0 and limiter._per_server == {}

    remote.listings.clear()
    remote.query_errors.clear()
    result = await search_logs(services, params([1001, 1002]))
    assert [s["status"] for s in result.payload["servers"]] == ["ok", "ok"]


# ---------------------------------------------------------------------------
# 截止时刻（9.3）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deadline_timeout_before_reading_and_truncation_after(tmp_path) -> None:
    """IP_A 已开始读取：保留完整行并标 timeout；IP_B 建连挂起：该 svr_id 返回 TIMEOUT。"""
    services, remote = make_services(tmp_path)

    async def timed_query(conn, query, sink, *, deadline):
        first = query.files[0]
        sink.begin_file(first.index, first)
        sink.write(first.index, b"complete line\npartial")
        try:
            async with asyncio.timeout_at(deadline):
                await hang_forever()
        except TimeoutError:
            pass
        sink.interrupt_file(first.index)  # 截到最后一个完整行（与 run_query 超时时的行为一致）
        files = tuple(FileOutcome(f, FileState.TRUNCATED if f is first else FileState.NOT_REACHED, None)
                      for f in query.files)
        return QueryOutcome(files, False, True, 1234, None, True)

    services.run_query = timed_query
    connect = services.connector.connect

    async def connect_hanging_on_b(ip):
        if ip == IP_B:
            await hang_forever()
        return await connect(ip)

    services.connector.connect = connect_hanging_on_b
    loop = asyncio.get_running_loop()
    started = loop.time()
    result = await search_logs(services, params([1001, 1002]), deadline=started + 0.3)
    assert loop.time() - started < 2  # 截止时刻后随即返回

    assert not result.is_error
    servers = {s["svr_id"]: s for s in result.payload["servers"]}
    a = servers[1001]
    assert a["status"] == "ok" and a["truncated"] == "timeout"
    assert a["files_read"] == SELECTED[:1] and a["files_skipped"] == SELECTED[1:]
    assert (a["bytes"], a["lines"]) == (len(b"complete line\n"), 1)
    assert handle_files(services.store, result.result_handle) == {"1001_normal.log": b"complete line\n"}
    assert servers[1002]["status"] == "failed" and servers[1002]["error"] == error_dict(ErrorCode.TIMEOUT)
    assert {t.svr_id: t.status for t in result.targets} == {1001: "ok", 1002: "TIMEOUT"}
    assert all(c.closed for c in services.connector.conns)
    assert services.limiter.active_calls == 0 and services.limiter._per_server == {}


@pytest.mark.asyncio
async def test_listing_hang_returns_timeout(tmp_path) -> None:
    services, remote = make_services(tmp_path)

    async def hanging_listing(conn, script, *, seconds):
        await hang_forever()

    services.run_listing = hanging_listing
    loop = asyncio.get_running_loop()
    result = await search_logs(services, params([1001]), deadline=loop.time() + 0.2)
    (s,) = result.payload["servers"]
    assert s["error"] == error_dict(ErrorCode.TIMEOUT) and s["file"] is None
    assert handle_files(services.store, result.result_handle) == {}
    assert all(c.closed for c in services.connector.conns)


@pytest.mark.asyncio
async def test_db_lookup_past_deadline_is_whole_call_timeout(tmp_path) -> None:
    services, _ = make_services(tmp_path)

    async def hanging_lookup(svr_ids):
        await hang_forever()

    services.db.lookup_servers = hanging_lookup
    loop = asyncio.get_running_loop()
    started = loop.time()
    result = await search_logs(services, params([1001]), deadline=started + 0.2)
    assert loop.time() - started < 2
    assert result.is_error and result.payload == {"error": error_dict(ErrorCode.TIMEOUT)}
    assert result.result_handle is None and os.listdir(services.store.directory) == []
    assert services.connector.conns == []
    assert services.limiter.active_calls == 0


@pytest.mark.asyncio
async def test_default_deadline_is_start_plus_query_timeout(tmp_path) -> None:
    services, _ = make_services(tmp_path, query_timeout_s=40)
    run = SearchLogsRun(services, params([1001]))
    loop = asyncio.get_running_loop()
    before = loop.time()
    await run.run()
    assert before + 40 <= run.deadline <= loop.time() + 40
