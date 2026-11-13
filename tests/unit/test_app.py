"""FastMCP 子类与工具注册的单元测试（任务 8.4）：在进程内直接调用 call_tool 与 list_tools，不经 HTTP。

替身管理库、SSH 建连与远端执行；真实的 Result_Store 与 AuditLog（临时目录）。
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone

import pytest
from mcp.types import CallToolResult

from log_viewer_mcp.common.tool_docs import SEARCH_LOGS_DESCRIPTION, search_logs_input_schema
from log_viewer_mcp.server.app import LogViewerMCP, create_mcp
from log_viewer_mcp.server.audit import AuditLog
from log_viewer_mcp.server.config import (
    AdminDbConfig,
    AuditConfig,
    Config,
    Limits,
    ListenConfig,
    LogTypeConfig,
    ResultsConfig,
    SshConfig,
    TokenRecord,
)
from log_viewer_mcp.server.context import current_token, current_user
from log_viewer_mcp.server.db import ServerLookup
from log_viewer_mcp.server.remote import (
    FileOutcome,
    FileState,
    FileStatus,
    Listing,
    PlainEntry,
    QueryOutcome,
    RemoteExit,
)
from log_viewer_mcp.server.results import ResultStore
from log_viewer_mcp.server.tools import ToolServices

TZ = timezone(timedelta(hours=-2))
LOGS = "/usr/local/ds/SFS2X/logs"
ZIP = "/usr/local/ds/SFS2X/ziplogs"
NOW = int(datetime(2026, 9, 17, 16, 12, 30, tzinfo=TZ).timestamp())
AT = int(datetime(2026, 9, 17, 16, 1, tzinfo=TZ).timestamp())
IP = "10.0.1.11"
PASSWORD = "db-secret-pw"
TOKEN = "tok-secret-value"

LISTING = Listing(
    plain=(PlainEntry("smartfox.log.2026-09-17-16-00", 11, 300), PlainEntry("smartfox.log", 12, 400)),
    archives=(),
    ionice=True,
)
OK_STATUS = FileStatus("F", 0, (0,))


def make_config(tmp_path) -> Config:
    return Config(
        listen=ListenConfig("0.0.0.0", 8080),
        server_timezone="-02:00",
        tz=TZ,
        tokens_file=str(tmp_path / "tokens.json"),  # 不存在：token 集合为空
        tokens=(),
        admin_db=AdminDbConfig("10.0.0.2", 3306, "logview", "admin", "db_password", PASSWORD),
        ssh=SshConfig("root", 22, "id_ed25519", "known_hosts", 5, b""),
        zip_dir=ZIP,
        limits=replace(Limits(), max_servers=7),
        results=ResultsConfig(dir=str(tmp_path / "results")),
        audit=AuditConfig(dir=str(tmp_path / "audit")),
        log_types={
            "normal": LogTypeConfig("normal", LOGS, ""),
            "login": LogTypeConfig("login", "/usr/local/ds/SFS2X/logpushinit", ".logpushinit"),
        },
    )


class FakeDb:
    def __init__(self) -> None:
        self.calls = 0

    async def lookup_servers(self, svr_ids):
        self.calls += 1
        return {i: ServerLookup(i, IP, None) for i in svr_ids}

    async def list_servers(self):
        self.calls += 1
        return [{"svr_id": 1001, "svr_name": "S1", "zone": "eu", "ip_inner": IP, "is_test": 0}]


class FakeConn:
    def __init__(self, host: str) -> None:
        self.host = host

    async def close(self) -> None:
        pass


class FakeConnector:
    def __init__(self) -> None:
        self.calls = 0

    async def connect(self, ip: str) -> FakeConn:
        self.calls += 1
        return FakeConn(ip)


class FakeRemote:
    def __init__(self) -> None:
        self.block: asyncio.Event | None = None
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()  # run_query 在阻塞时被取消（真实实现此时终止远端进程组）

    async def run_listing(self, conn, script, *, seconds):
        return LISTING

    async def run_query(self, conn, query, sink, *, deadline):
        self.started.set()
        if self.block is not None:
            try:
                await self.block.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        outcomes = []
        for f in query.files:
            sink.begin_file(f.index, f)
            sink.write(f.index, b"2026-09-17 16:00:01,000 | INFO  | t | CMD | x\n")
            sink.end_file(f.index, OK_STATUS)
            outcomes.append(FileOutcome(f, FileState.READ, OK_STATUS))
        return QueryOutcome(tuple(outcomes), True, False, 1234, RemoteExit(0, None, 0))


async def accept_all(pattern: str) -> bool:
    return True


class Env:
    def __init__(self, tmp_path, *, audit_dir=None) -> None:
        self.config = make_config(tmp_path)
        self.store = ResultStore.from_config(self.config.results)
        self.store.prepare()
        self.db = FakeDb()
        self.connector = FakeConnector()
        self.remote = FakeRemote()
        self.services = ToolServices(
            config=self.config, db=self.db, connector=self.connector, store=self.store, clock=lambda: NOW,
            run_listing=self.remote.run_listing, run_query=self.remote.run_query,
        )
        self.audit_dir = audit_dir if audit_dir is not None else self.config.audit.dir
        self.audit = AuditLog(self.audit_dir, TZ, 90, db_password=PASSWORD, clock=lambda: NOW)
        self.mcp = create_mcp(self.config, self.services, self.audit, regex_checker=accept_all)

    def config_with_tokens(self, tokens: tuple[TokenRecord, ...]) -> Config:
        """把 tokens 写入 config.tokens_file（build_asgi_app 的认证中间件从该文件读取），返回带这些 token 的配置。"""
        with open(self.config.tokens_file, "w", encoding="utf-8") as f:
            json.dump([asdict(t) for t in tokens], f, ensure_ascii=False)
        return replace(self.config, tokens=tokens)

    def audit_records(self) -> list[dict]:
        path = os.path.join(self.audit_dir, "audit-2026-09-17.jsonl")
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f]

    def audit_text(self) -> str:
        with open(os.path.join(self.audit_dir, "audit-2026-09-17.jsonl"), encoding="utf-8") as f:
            return f.read()


@pytest.fixture
def env(tmp_path) -> Env:
    return Env(tmp_path)


@pytest.fixture
def as_user():
    u = current_user.set("zhangsan")
    t = current_token.set(TOKEN)
    yield
    current_user.reset(u)
    current_token.reset(t)


def payload(result: CallToolResult) -> dict:
    assert isinstance(result, CallToolResult)
    (content,) = result.content
    data = json.loads(content.text)
    assert result.structuredContent == data
    return data


# ---------------------------------------------------------------------------
# call_tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_argument_rejected_without_db_or_ssh(env, as_user) -> None:
    args = {"svr_ids": [1001], "log_type": "normal", "path": "/etc/passwd"}
    result = await env.mcp.call_tool("search_logs", args)
    assert result.isError is True
    assert payload(result)["error"]["code"] == "INVALID_ARGUMENT"
    assert env.db.calls == 0 and env.connector.calls == 0
    (record,) = env.audit_records()
    assert record["tool"] == "search_logs" and record["user"] == "zhangsan"
    assert record["status"] == "INVALID_ARGUMENT"
    assert record["params"] == args and record["targets"] == [] and record["result_handle"] is None


@pytest.mark.asyncio
async def test_string_svr_id_not_coerced(env) -> None:
    result = await env.mcp.call_tool("search_logs", {"svr_ids": ["1; rm -rf /"], "log_type": "normal"})
    assert payload(result)["error"]["code"] == "INVALID_ARGUMENT"
    assert env.db.calls == 0 and env.connector.calls == 0


@pytest.mark.asyncio
async def test_unknown_tool_rejected_and_audited(env) -> None:
    result = await env.mcp.call_tool("rm_logs", {})
    assert result.isError is True
    assert payload(result)["error"]["code"] == "INVALID_ARGUMENT"
    (record,) = env.audit_records()
    assert record["tool"] == "rm_logs" and record["status"] == "INVALID_ARGUMENT"


@pytest.mark.asyncio
async def test_search_logs_success_returns_summary_and_audits(env, as_user) -> None:
    args = {"svr_ids": [1001, 1001], "log_type": "normal", "at_time": AT, "include_patterns": ["CMD", TOKEN]}
    result = await env.mcp.call_tool("search_logs", args)
    assert result.isError is False
    summary = payload(result)
    handle = summary["result_handle"]
    assert summary["server_timezone"] == "GMT-2"
    (server,) = summary["servers"]
    assert server["svr_id"] == 1001 and server["status"] == "ok" and server["file"] == "1001_normal.log"
    assert server["lines"] == 2
    assert os.path.isfile(os.path.join(env.store.handle_dir(handle), "1001_normal.log"))

    (record,) = env.audit_records()
    assert record["user"] == "zhangsan" and record["tool"] == "search_logs" and record["status"] == "ok"
    assert record["result_handle"] == handle
    assert isinstance(record["duration_ms"], int) and record["duration_ms"] >= 0
    assert record["params"]["svr_ids"] == [1001, 1001] and record["params"]["include_patterns"] == ["CMD", "***"]
    (target,) = record["targets"]
    assert target["svr_id"] == 1001 and target["ip_inner"] == IP and target["status"] == "ok"
    assert len(target["scripts"]) == 2
    assert TOKEN not in env.audit_text() and PASSWORD not in env.audit_text()


@pytest.mark.asyncio
async def test_list_servers_success(env) -> None:
    result = await env.mcp.call_tool("list_servers", {})
    assert result.isError is False
    assert payload(result)["servers"][0]["svr_id"] == 1001
    (record,) = env.audit_records()
    assert record["tool"] == "list_servers" and record["status"] == "ok"


@pytest.mark.asyncio
async def test_audit_failure_returns_audit_failed_and_discards_result(tmp_path) -> None:
    not_dir = tmp_path / "audit_is_file"
    not_dir.write_text("x")
    env = Env(tmp_path, audit_dir=str(not_dir))
    result = await env.mcp.call_tool("search_logs", {"svr_ids": [1001], "log_type": "normal", "at_time": AT})
    assert result.isError is True
    assert payload(result) == {"error": {"code": "AUDIT_FAILED", "message": "写入审计记录失败，本次结果已删除"}}
    assert os.listdir(env.config.results.dir) == []


@pytest.mark.asyncio
async def test_cancelled_call_is_audited_and_result_discarded(env) -> None:
    env.remote.block = asyncio.Event()  # 永不置位
    task = asyncio.create_task(
        env.mcp.call_tool("search_logs", {"svr_ids": [1001], "log_type": "normal", "at_time": AT})
    )
    await asyncio.wait_for(env.remote.started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    (record,) = env.audit_records()
    assert record["status"] == "cancelled"
    assert record["targets"][0]["status"] == "cancelled"
    assert os.listdir(env.config.results.dir) == []


# ---------------------------------------------------------------------------
# list_tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_tools_exposes_schema_from_config(env) -> None:
    tools = {t.name: t for t in await env.mcp.list_tools()}
    assert set(tools) == {"search_logs", "list_servers"}

    search = tools["search_logs"]
    schema = search.inputSchema
    assert schema["required"] == ["svr_ids", "log_type"] and schema["additionalProperties"] is False
    props = schema["properties"]
    assert props["log_type"]["enum"] == ["normal", "login"]
    assert props["svr_ids"]["maxItems"] == 7 and props["svr_ids"]["minItems"] == 1
    assert props["include_patterns"]["maxItems"] == 10
    assert props["include_patterns"]["items"]["maxLength"] == 256
    assert props["context_after"]["maximum"] == 100
    assert props["at_time"]["maximum"] == 4102444799
    assert search.outputSchema is None and search.annotations.readOnlyHint is True

    servers = tools["list_servers"]
    assert servers.inputSchema["properties"] == {} and servers.inputSchema["additionalProperties"] is False


@pytest.mark.asyncio
async def test_search_logs_description_key_points(env) -> None:
    (search,) = [t for t in await env.mcp.list_tools() if t.name == "search_logs"]
    d = search.description
    assert d == SEARCH_LOGS_DESCRIPTION
    for point in (
        "秒级 Unix 时间戳",
        "GMT-2",
        "2026-10-06 21:13:05",
        "1791328385",
        "yyyy-MM-dd HH:mm:ss,SSS | 级别 | 线程 | logger | 消息",
        "logpushinit",
        "PUSHINIT",
        "ANALYSISLOG",
        "polsarlog",
        "context_after",
        "本地按行首",
        "不会被当作命令",
        "INVALID_ARGUMENT",
    ):
        assert point in d, point


def test_description_timestamp_example_is_correct() -> None:
    ts = int(datetime(2026, 10, 6, 21, 13, 5, tzinfo=TZ).timestamp())
    assert ts == 1791328385
    assert datetime.fromtimestamp(ts, timezone.utc).isoformat() == "2026-10-06T23:13:05+00:00"


def test_local_schema_has_no_enum_or_limits() -> None:
    props = search_logs_input_schema()["properties"]
    assert props["log_type"] == {"type": "string", "description": props["log_type"]["description"]}
    assert "maxItems" not in props["svr_ids"] and "maximum" not in props["context_after"]


def test_fastmcp_settings(env) -> None:
    assert isinstance(env.mcp, LogViewerMCP)
    assert env.mcp.settings.stateless_http is True and env.mcp.settings.json_response is True
    assert env.mcp.settings.host == "0.0.0.0" and env.mcp.settings.port == 8080
