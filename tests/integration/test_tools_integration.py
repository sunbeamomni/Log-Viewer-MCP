"""search_logs 与 list_servers 编排的端到端集成测试（任务 8.1）：真实的管理库、游戏服与 Result_Store。

- 1001 → gs、1002 → gs-noionice 成功，结果文件与参照过滤逐字节相同；9999 → SERVER_NOT_FOUND、
  1005 → INVALID_SERVER_ADDRESS（都不发起 SSH）、1004 → SSH_FAILED，只影响各自的 svr_id（1.7、7.4、7.5）。
- Time_Range 内没有文件：结果为空文件，摘要给出 available_range。
- 全程 gs 上的日志目录、/tmp 与 /root 没有任何改动（只读）。
"""

from __future__ import annotations

import os

import pytest

from log_viewer_mcp.common.errors import ErrorCode, error_dict
from log_viewer_mcp.server.results import ResultStore
from log_viewer_mcp.server.tools import ToolServices, list_servers, search_logs
from log_viewer_mcp.server.validate import SearchParams
from tests.integration import sample_logs
from tests.integration.itenv import GameServers, ItEnv
from tests.integration.test_remote_e2e import grep_ref

PATTERNS = ("SAMPLE_MARK", "uid=123")
EXPECTED_FILES = ["smartfox.log.2026-09-17-16-%02d" % m for m in (0, 1, 2, 3, 4, 6, 7, 8, 9)]
# 只在 ziplogs 中的时段，摘要中的名称是压缩包名
EXPECTED_FILES = [n if not n.endswith(("-00", "-01")) else n + ".tar.gz" for n in EXPECTED_FILES] + ["smartfox.log"]


def make_services(service_files) -> ToolServices:
    config = service_files.load()
    store = ResultStore.from_config(config.results)
    store.prepare()
    return ToolServices.from_config(config, store=store, clock=lambda: sample_logs.GENERATED_AT)


def sample_content(samples: sample_logs.SampleSet, name: str) -> bytes:
    if name == "smartfox.log":
        return samples.current("normal").content
    ts = name.removeprefix("smartfox.log.").removesuffix(".tar.gz")
    return samples.period("normal", ts).content


def read_result(store: ResultStore, handle: str, name: str) -> bytes:
    with open(store.result_path(handle, name), "rb") as f:
        return f.read()


@pytest.mark.asyncio
async def test_search_logs_end_to_end(it_env: ItEnv, service_files, samples, game_servers: GameServers) -> None:
    services = make_services(service_files)
    params = SearchParams(
        svr_ids=(1001, 1002, 9999, 1005, 1004), log_type="normal",
        start_time=sample_logs.server_time(2026, 9, 17, 16, 0), end_time=sample_logs.server_time(2026, 9, 17, 16, 12),
        include_patterns=PATTERNS,
    )
    async with game_servers.assert_unchanged(it_env.gs_ip):
        result = await search_logs(services, params)

    assert not result.is_error and result.status == "ok"
    s = result.payload
    handle = s["result_handle"]
    assert s["time_range"] == {
        "start": params.start_time, "end": params.end_time,
        "start_server": "2026-09-17 16:00:00", "end_server": "2026-09-17 16:12:00",
    }
    assert s["server_timezone"] == "GMT-2"
    servers = {x["svr_id"]: x for x in s["servers"]}
    assert [x["svr_id"] for x in s["servers"]] == list(params.svr_ids)

    expected = b"".join(grep_ref(sample_content(samples, n), PATTERNS) for n in EXPECTED_FILES)
    assert expected.count(b"SAMPLE_MARK") == len(EXPECTED_FILES)
    for svr_id in (1001, 1002):
        x = servers[svr_id]
        name = f"{svr_id}_normal.log"
        assert x == {
            "svr_id": svr_id, "status": "ok", "error": None, "file": name,
            "bytes": len(expected), "lines": expected.count(b"\n"),
            "files_read": EXPECTED_FILES, "files_skipped": [], "truncated": "none", "available_range": None,
        }
        assert read_result(services.store, handle, name) == expected

    for svr_id, code in ((9999, ErrorCode.SERVER_NOT_FOUND), (1005, ErrorCode.INVALID_SERVER_ADDRESS),
                         (1004, ErrorCode.SSH_FAILED)):
        x = servers[svr_id]
        assert x["status"] == "failed" and x["error"] == error_dict(code) and x["file"] is None
    assert sorted(os.listdir(services.store.handle_dir(handle))) == ["1001_normal.log", "1002_normal.log"]

    targets = {t.svr_id: t for t in result.targets}
    assert targets[1001].ip_inner == it_env.gs_ip and len(targets[1001].scripts) == 2
    assert targets[1002].ip_inner == it_env.gs_noionice_ip and len(targets[1002].scripts) == 2
    assert (targets[1004].ip_inner, list(targets[1004].scripts), targets[1004].status) == (
        it_env.gs_unreachable_ip, [], "SSH_FAILED")
    assert targets[9999].ip_inner is None and targets[1005].ip_inner is None
    services.store.discard(handle)


@pytest.mark.asyncio
async def test_no_files_in_range_gives_available_range(it_env: ItEnv, service_files, game_servers) -> None:
    services = make_services(service_files)
    params = SearchParams(svr_ids=(1001,), log_type="normal", at_time=sample_logs.server_time(2026, 9, 10, 12, 0))
    async with game_servers.assert_unchanged(it_env.gs_ip):
        result = await search_logs(services, params)
    (x,) = result.payload["servers"]
    assert x["status"] == "ok" and (x["bytes"], x["lines"], x["files_read"]) == (0, 0, [])
    assert x["available_range"] == {
        "earliest": sample_logs.server_time(2026, 9, 14), "latest": sample_logs.GENERATED_AT,
        "earliest_server": "2026-09-14 00:00:00", "latest_server": "2026-09-17 16:12:30",
    }
    assert read_result(services.store, result.result_handle, "1001_normal.log") == b""
    assert len(result.targets[0].scripts) == 1  # 没有执行 ②
    services.store.discard(result.result_handle)


@pytest.mark.asyncio
async def test_list_servers_end_to_end(service_files) -> None:
    result = await list_servers(make_services(service_files))
    assert not result.is_error
    ids = [x["svr_id"] for x in result.payload["servers"]]
    assert ids == sorted(ids) and {1001, 1002, 2001, 4294967295} <= set(ids)
    assert set(result.payload["servers"][0]) == {"svr_id", "svr_name", "zone", "ip_inner", "is_test"}
