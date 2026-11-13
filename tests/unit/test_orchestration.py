"""编排的单元测试（任务 8.3，需求 1.6、1.7、9.2、9.3、10.3）。

补充 test_tools.py（8.1）与 test_limits.py（8.2）未覆盖的部分，替身沿用 test_tools.py：

- 摘要的键集合与 design.md「工具接口」一致，且可被 json.dumps 序列化并原样还原；
- available_range：日、小时文件按 GMT-2 格式化；该类型一个文件都没有时为 null；
- 单服失败隔离：在建连、① 与 ② 各阶段注入每种单服错误码（含 HOST_KEY_MISMATCH、REMOTE_TOOL_UNAVAILABLE、
  分帧不符的 INTERNAL_ERROR），失败服已写入的部分结果被删除，其余服的结果文件完整；
- 对外错误不含原始错误文本（10.3）：注入的异常文本含内部 IP、密码与堆栈，摘要与整次错误的 JSON 中都没有，
  错误说明等于 errors.py 的 MESSAGES；
- files_skipped 由 ② 失败的文件与扫描上限未选的文件拼接；timeout 优先于 scan_limit；
- 摘要按 svr_ids 的顺序排列（与管理库返回顺序、各服完成顺序无关）；
- 管理库查询恰在截止时刻返回时，各 svr_id 在建连前返回 TIMEOUT（9.3）。

svr_ids 的去重由 validate.py 负责，这里传入的都是已去重的列表。
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace

import pytest

from log_viewer_mcp.common.errors import MESSAGES, ErrorCode, LogViewerError, error_dict
from log_viewer_mcp.server.remote import (
    ArchiveEntry,
    FileOutcome,
    FileState,
    FileStatus,
    PlainEntry,
    QueryOutcome,
    RemoteExit,
)
from log_viewer_mcp.server.results import ResultStore
from log_viewer_mcp.server.tools import ToolServices, list_servers, search_logs
from tests.unit.test_tools import (
    IP_A,
    IP_B,
    IP_DOWN,
    LISTING,
    NOW,
    OK_STATUS,
    SELECTED,
    FakeConnector,
    FakeDb,
    FakeRemote,
    handle_files,
    make_config,
    params,
    server_time,
)

# design.md「工具接口」中摘要的键
SUMMARY_KEYS = {"result_handle", "time_range", "server_timezone", "servers"}
TIME_RANGE_KEYS = {"start", "end", "start_server", "end_server"}
SERVER_KEYS = {"svr_id", "status", "error", "file", "bytes", "lines", "files_read", "files_skipped", "truncated",
               "available_range"}
ERROR_KEYS = {"code", "message"}
RANGE_KEYS = {"earliest", "latest", "earliest_server", "latest_server"}

# 注入的原始错误文本：内部地址、密码与类似堆栈的内容，都不得出现在对外的 JSON 中
RAW_TEXT = (
    "connect to 10.9.8.7:3306 failed for admin password=hunter2-secret\n"
    "Traceback (most recent call last):\n"
    '  File "/opt/log-viewer/internal.py", line 42, in do_query'
)
LEAK_MARKERS = ("10.9.8.7", "hunter2", "Traceback", "internal.py", "line 42", IP_A, IP_B, IP_DOWN)

EXPECTED_A = b"".join(n.encode() + b" line\n" for n in SELECTED)


def raw_error() -> RuntimeError:
    return RuntimeError(RAW_TEXT)


def chained(code: ErrorCode) -> LogViewerError:
    """与 db.py、remote.py 中 ``raise LogViewerError(code) from e`` 相同：原始异常挂在 __cause__ 上。"""
    error = LogViewerError(code)
    error.__cause__ = raw_error()
    return error


class ErrorConnector(FakeConnector):
    """按 ip 注入建连异常；其余同 FakeConnector。delays 给各 ip 的建连加延时。"""

    def __init__(self) -> None:
        super().__init__()
        self.errors: dict[str, BaseException] = {}
        self.delays: dict[str, float] = {}

    async def connect(self, ip: str):
        await asyncio.sleep(self.delays.get(ip, 0))
        if ip in self.errors:
            raise self.errors[ip]
        return await super().connect(ip)


def make_services(tmp_path, *, db=None, **limits):
    config = make_config(tmp_path, **limits)
    store = ResultStore.from_config(config.results)
    store.prepare()
    remote = FakeRemote()
    services = ToolServices(
        config=config, db=db or FakeDb(), connector=ErrorConnector(), store=store, clock=lambda: NOW,
        run_listing=remote.run_listing, run_query=remote.run_query,
    )
    return services, remote


def failed_summary(svr_id: int, code: ErrorCode) -> dict:
    return {"svr_id": svr_id, "status": "failed", "error": error_dict(code), "file": None, "bytes": 0, "lines": 0,
            "files_read": [], "files_skipped": [], "truncated": "none", "available_range": None}


def ok_summary_a() -> dict:
    return {"svr_id": 1001, "status": "ok", "error": None, "file": "1001_normal.log", "bytes": len(EXPECTED_A),
            "lines": 3, "files_read": SELECTED, "files_skipped": [], "truncated": "none", "available_range": None}


def public_errors(payload: dict) -> list[dict]:
    errors = [payload["error"]] if "error" in payload else []
    errors += [s["error"] for s in payload.get("servers", []) if s.get("error") is not None]
    return errors


def assert_public(payload: dict) -> None:
    """对外 JSON 可序列化、不含原始错误文本与内部地址，错误说明都是 errors.py 中写定的。"""
    text = json.dumps(payload, ensure_ascii=False)
    for marker in LEAK_MARKERS:
        assert marker not in text
    for e in public_errors(payload):
        assert set(e) == ERROR_KEYS and e["message"] == MESSAGES[ErrorCode(e["code"])]


# ---------------------------------------------------------------------------
# 摘要的结构
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_keys_match_design_and_json_round_trips(tmp_path) -> None:
    services, remote = make_services(tmp_path)
    # IP_B 只有 10:00 的分钟文件：Time_Range（15:56–16:06）内没有文件，给出 available_range
    remote.listings[IP_B] = replace(LISTING, plain=(PlainEntry("smartfox.log.2026-09-17-10-00", 21, 10),),
                                    archives=())
    result = await search_logs(services, params([1001, 1002, 1004]))
    payload = result.payload

    assert json.loads(json.dumps(payload)) == payload  # 只含 JSON 类型（没有元组、枚举等）
    assert set(payload) == SUMMARY_KEYS
    assert set(payload["time_range"]) == TIME_RANGE_KEYS
    for s in payload["servers"]:
        assert set(s) == SERVER_KEYS
        assert s["status"] in ("ok", "failed") and s["truncated"] in ("none", "timeout", "scan_limit")
    a, b, down = payload["servers"]
    assert a == ok_summary_a()
    assert set(b["available_range"]) == RANGE_KEYS
    assert b["available_range"] == {
        "earliest": server_time(2026, 9, 17, 10, 0), "latest": server_time(2026, 9, 17, 10, 1),
        "earliest_server": "2026-09-17 10:00:00", "latest_server": "2026-09-17 10:01:00",
    }
    assert down == failed_summary(1004, ErrorCode.SSH_FAILED)
    assert_public(payload)


@pytest.mark.asyncio
async def test_whole_call_error_payload_shape(tmp_path) -> None:
    services, _ = make_services(tmp_path)
    services.db.fail = True
    result = await search_logs(services, params([1001]))
    assert result.is_error
    assert json.loads(json.dumps(result.payload)) == result.payload
    assert set(result.payload) == {"error"} and set(result.payload["error"]) == ERROR_KEYS


# ---------------------------------------------------------------------------
# available_range
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_available_range_day_and_hour_files_in_server_time(tmp_path) -> None:
    services, remote = make_services(tmp_path)
    remote.listings[IP_A] = replace(
        LISTING,
        plain=(PlainEntry("smartfox.log.2026-09-17", 21, 1000), PlainEntry("gc.log", 13, 50)),
        archives=(ArchiveEntry("smartfox.log.2026-09-16-23.tar.gz", 100),),
    )
    result = await search_logs(services, params([1001], at_time=server_time(2026, 9, 15, 12, 0)))
    (s,) = result.payload["servers"]
    # 最早为 09-16 23 点小时压缩包的起点，最晚为 09-17 天文件的终点（服务器时间 GMT-2）
    assert s["available_range"] == {
        "earliest": server_time(2026, 9, 16, 23), "latest": server_time(2026, 9, 18, 0),
        "earliest_server": "2026-09-16 23:00:00", "latest_server": "2026-09-18 00:00:00",
    }
    assert s["available_range"]["earliest"] == 1789606800  # 2026-09-17T01:00:00Z
    assert remote.queries == []


@pytest.mark.asyncio
async def test_no_files_at_all_gives_null_available_range(tmp_path) -> None:
    services, remote = make_services(tmp_path)
    remote.listings[IP_A] = replace(LISTING, plain=(PlainEntry("gc.log", 13, 50),), archives=())
    result = await search_logs(services, params([1001]))
    (s,) = result.payload["servers"]
    assert s == {"svr_id": 1001, "status": "ok", "error": None, "file": "1001_normal.log", "bytes": 0, "lines": 0,
                 "files_read": [], "files_skipped": [], "truncated": "none", "available_range": None}
    assert remote.queries == []
    assert handle_files(services.store, result.result_handle) == {"1001_normal.log": b""}


# ---------------------------------------------------------------------------
# 单服失败隔离（1.7）
# ---------------------------------------------------------------------------


def query_failing_on(remote: FakeRemote, ip: str, error: BaseException):
    """② 的替身：ip 上先写入一部分结果再抛出 error（读取时连接中断、分帧不符等）；其余主机同 FakeRemote。"""

    async def run_query(conn, query, sink, *, deadline):
        if conn.host != ip:
            return await remote.run_query(conn, query, sink, deadline=deadline)
        first = query.files[0]
        sink.begin_file(first.index, first)
        sink.write(first.index, b"partial data from failed server\n")
        sink.end_file(first.index, OK_STATUS)
        second = query.files[1]
        sink.begin_file(second.index, second)
        sink.write(second.index, b"half a li")
        await asyncio.sleep(0)
        raise error

    return run_query


# (阶段, 异常工厂, 摘要中的错误码, 该服审计中的脚本数)
FAILURES = [
    ("connect", lambda: chained(ErrorCode.SSH_FAILED), ErrorCode.SSH_FAILED, 0),
    ("connect", lambda: chained(ErrorCode.HOST_KEY_MISMATCH), ErrorCode.HOST_KEY_MISMATCH, 0),
    ("connect", raw_error, ErrorCode.INTERNAL_ERROR, 0),
    ("listing", lambda: chained(ErrorCode.SSH_FAILED), ErrorCode.SSH_FAILED, 1),
    ("listing", lambda: chained(ErrorCode.REMOTE_TOOL_UNAVAILABLE), ErrorCode.REMOTE_TOOL_UNAVAILABLE, 1),
    ("listing", lambda: chained(ErrorCode.LOG_DIR_UNAVAILABLE), ErrorCode.LOG_DIR_UNAVAILABLE, 1),
    ("query", lambda: chained(ErrorCode.SSH_FAILED), ErrorCode.SSH_FAILED, 2),
    ("query", lambda: chained(ErrorCode.INTERNAL_ERROR), ErrorCode.INTERNAL_ERROR, 2),  # 分帧不符
    ("query", lambda: chained(ErrorCode.TIMEOUT), ErrorCode.TIMEOUT, 2),
    ("query", raw_error, ErrorCode.INTERNAL_ERROR, 2),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("stage", "make_error", "code", "scripts"), FAILURES,
                         ids=[f"{f[0]}-{f[2].value}" for f in FAILURES])
async def test_server_failure_is_isolated(tmp_path, caplog, stage, make_error, code, scripts) -> None:
    services, remote = make_services(tmp_path)
    error = make_error()
    if stage == "connect":
        services.connector.errors[IP_B] = error
    elif stage == "listing":
        remote.listings[IP_B] = error
    else:
        services.run_query = query_failing_on(remote, IP_B, error)

    result = await search_logs(services, params([1001, 1002]))
    assert not result.is_error and result.status == "ok"
    assert result.payload["servers"] == [ok_summary_a(), failed_summary(1002, code)]
    # 失败服已写入的部分结果被删除；其余服的结果文件完整
    assert handle_files(services.store, result.result_handle) == {"1001_normal.log": EXPECTED_A}
    assert_public(result.payload)

    targets = {t.svr_id: t for t in result.targets}
    assert targets[1001].status == "ok"
    assert targets[1002].status == code.value and len(targets[1002].scripts) == scripts
    assert all(c.closed for c in services.connector.conns)
    assert services.limiter.active_calls == 0 and services.limiter._per_server == {}
    if isinstance(error, RuntimeError):
        assert "hunter2" in caplog.text  # 原始异常只写运行日志


# ---------------------------------------------------------------------------
# 对外错误不含原始错误文本（10.3）
# ---------------------------------------------------------------------------


WHOLE_CALL_FAILURES = [
    ("lookup", raw_error, ErrorCode.INTERNAL_ERROR),
    ("lookup", lambda: chained(ErrorCode.DB_UNAVAILABLE), ErrorCode.DB_UNAVAILABLE),
    ("create_handle", lambda: OSError(13, RAW_TEXT), ErrorCode.INTERNAL_ERROR),
    ("create_handle", lambda: chained(ErrorCode.RESULT_STORE_FAILED), ErrorCode.RESULT_STORE_FAILED),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("where", "make_error", "code"), WHOLE_CALL_FAILURES,
                         ids=[f"{f[0]}-{f[2].value}" for f in WHOLE_CALL_FAILURES])
async def test_whole_call_errors_hide_raw_text(tmp_path, where, make_error, code) -> None:
    services, _ = make_services(tmp_path)

    async def failing_lookup(svr_ids):
        raise make_error()

    def failing_create_handle():
        raise make_error()

    if where == "lookup":
        services.db.lookup_servers = failing_lookup
    else:
        services.store.create_handle = failing_create_handle
    result = await search_logs(services, params([1001, 1002]))
    assert result.is_error and result.status == code.value
    assert result.payload == {"error": error_dict(code)}
    assert_public(result.payload)
    assert os.listdir(services.store.directory) == []
    assert services.connector.conns == []
    assert services.limiter.active_calls == 0


@pytest.mark.asyncio
async def test_list_servers_hides_raw_text(tmp_path) -> None:
    services, _ = make_services(tmp_path)

    async def failing_list():
        raise raw_error()

    services.db.list_servers = failing_list
    result = await list_servers(services)
    assert result.is_error and result.payload == {"error": error_dict(ErrorCode.INTERNAL_ERROR)}
    assert_public(result.payload)


@pytest.mark.asyncio
async def test_all_stages_failing_at_once_hide_raw_text(tmp_path) -> None:
    """同一次调用中各服在不同阶段失败：摘要中只有错误码与写定的说明。"""
    lookups_db = FakeDb()
    services, remote = make_services(tmp_path, db=lookups_db)
    services.connector.errors[IP_A] = raw_error()
    remote.listings[IP_B] = chained(ErrorCode.REMOTE_TOOL_UNAVAILABLE)
    result = await search_logs(services, params([1001, 1002, 1004, 9999, 1005]))
    assert [s["error"]["code"] for s in result.payload["servers"]] == [
        "INTERNAL_ERROR", "REMOTE_TOOL_UNAVAILABLE", "SSH_FAILED", "SERVER_NOT_FOUND", "INVALID_SERVER_ADDRESS",
    ]
    assert_public(result.payload)
    assert handle_files(services.store, result.result_handle) == {}


# ---------------------------------------------------------------------------
# files_skipped 与 truncated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_files_skipped_joins_failed_files_and_scan_limit(tmp_path) -> None:
    services, _ = make_services(tmp_path, max_scan_bytes=600)  # 选中压缩包与 16:00 的分钟文件，Current 未选
    bad = FileStatus("Z", 1, (0,))  # gzip 解压失败

    async def run_query(conn, query, sink, *, deadline):
        archive, rotated = query.files
        sink.begin_file(archive.index, archive)
        sink.write(archive.index, b"half of a broken archive\n")
        sink.end_file(archive.index, bad)  # 失败的文件按偏移截回
        sink.begin_file(rotated.index, rotated)
        sink.write(rotated.index, b"rotated line\n")
        sink.end_file(rotated.index, OK_STATUS)
        files = (FileOutcome(archive, FileState.FAILED, bad), FileOutcome(rotated, FileState.READ, OK_STATUS))
        return QueryOutcome(files, True, False, 1234, RemoteExit(0, None, 0))

    services.run_query = run_query
    result = await search_logs(services, params([1001]))
    (s,) = result.payload["servers"]
    assert s["status"] == "ok" and s["truncated"] == "scan_limit"
    assert s["files_read"] == [SELECTED[1]]
    assert s["files_skipped"] == [SELECTED[0], SELECTED[2]]  # ② 失败的文件在前，扫描上限未选的在后
    assert (s["bytes"], s["lines"]) == (len(b"rotated line\n"), 1)
    assert handle_files(services.store, result.result_handle) == {"1001_normal.log": b"rotated line\n"}


@pytest.mark.asyncio
async def test_timeout_takes_precedence_over_scan_limit(tmp_path) -> None:
    services, _ = make_services(tmp_path, max_scan_bytes=600)

    async def run_query(conn, query, sink, *, deadline):
        archive, rotated = query.files
        sink.begin_file(archive.index, archive)
        sink.write(archive.index, b"complete\npartial")
        sink.interrupt_file(archive.index)
        files = (FileOutcome(archive, FileState.TRUNCATED), FileOutcome(rotated, FileState.NOT_REACHED))
        return QueryOutcome(files, False, True, 1234, None, True)

    services.run_query = run_query
    result = await search_logs(services, params([1001]))
    (s,) = result.payload["servers"]
    assert s["status"] == "ok" and s["error"] is None and s["truncated"] == "timeout"
    assert s["files_read"] == [SELECTED[0]]
    assert s["files_skipped"] == [SELECTED[1], SELECTED[2]]  # 因超时未读，再接上扫描上限未选
    assert handle_files(services.store, result.result_handle) == {"1001_normal.log": b"complete\n"}


# ---------------------------------------------------------------------------
# svr_ids 顺序与截止时刻
# ---------------------------------------------------------------------------


class ReversedDb(FakeDb):
    """管理库替身：返回的映射与请求顺序相反。"""

    async def lookup_servers(self, svr_ids):
        result = await super().lookup_servers(svr_ids)
        return dict(reversed(list(result.items())))


@pytest.mark.asyncio
async def test_summary_follows_svr_ids_order(tmp_path) -> None:
    services, _ = make_services(tmp_path, db=ReversedDb())
    services.connector.delays = {IP_A: 0.05, IP_DOWN: 0.02}  # 1002 最先完成，1001 最后完成
    order = [1001, 9999, 1004, 1002, 1005]
    result = await search_logs(services, params(order))
    assert [s["svr_id"] for s in result.payload["servers"]] == order
    assert [t.svr_id for t in result.targets] == order
    assert [s["status"] for s in result.payload["servers"]] == ["ok", "failed", "failed", "ok", "failed"]


@pytest.mark.asyncio
async def test_lookup_returning_at_deadline_gives_per_server_timeout(tmp_path) -> None:
    """管理库查询返回时已到截止时刻：各 svr_id 在建连前返回 TIMEOUT，查不到的 svr_id 仍报各自的错误。"""
    services, remote = make_services(tmp_path)
    loop = asyncio.get_running_loop()
    result = await search_logs(services, params([1001, 9999, 1005]), deadline=loop.time() - 1)
    assert not result.is_error
    assert result.payload["servers"] == [
        failed_summary(1001, ErrorCode.TIMEOUT),
        failed_summary(9999, ErrorCode.SERVER_NOT_FOUND),
        failed_summary(1005, ErrorCode.INVALID_SERVER_ADDRESS),
    ]
    assert services.connector.conns == [] and remote.listing_calls == []
    assert handle_files(services.store, result.result_handle) == {}
    assert {t.svr_id: t.status for t in result.targets} == {1001: "TIMEOUT", 9999: "SERVER_NOT_FOUND",
                                                            1005: "INVALID_SERVER_ADDRESS"}
    assert_public(result.payload)
