"""端到端冒烟测试（任务 12.1，需求 3.1、4.1、4.2、4.3、6.3）：按 design.md「Architecture」的时序走完整条链路。

在测试运行容器中：
1. 用 Token_CLI（``python -m log_viewer_mcp.token_cli -f <tokens.json> create <user>``）为新使用者生成 token；
2. 以子进程启动跳板机服务（``python -m log_viewer_mcp.server -c config.toml``，同 test_server_main.py），等它开始监听；
3. 经 MCP SDK 的 stdio 客户端启动 ``python -m log_viewer_mcp.local``（HOME 下的环境登记文件 envs.toml 登记环境 it，
   工作目录中的项目文件 .log-viewer.toml 选用它），依次带 env 调用 list_servers 与 search_logs（svr_id 1001 → gs）。

确认：list_servers 返回管理库中的全部行；search_logs 的本地结果文件存在，内容与参照过滤
（test_remote_e2e.grep_ref，对样例日志的内存模型）逐字节相同；跳板机 Result_Store 中该 handle 已删除；
审计中有两次调用的记录，使用者为 Token_CLI 登记的名称；服务收到 SIGTERM 后正常退出。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from log_viewer_mcp.server.db import AdminDb
from tests.integration import sample_logs
from tests.integration.itenv import GameServers, ItEnv, ServiceFiles
from tests.integration.test_remote_e2e import grep_ref
from tests.integration.test_server_main import REPO_ROOT, RunningServer
from tests.integration.test_tools_integration import sample_content

SMOKE_USER = "e2e-smoke"
CALL_TIMEOUT_S = 90
PATTERNS = ["SAMPLE_MARK", "uid=123"]
SEARCH_ARGS = {
    "svr_ids": [1001],
    "log_type": "normal",
    "start_time": sample_logs.server_time(2026, 9, 17, 16, 0),
    "end_time": sample_logs.server_time(2026, 9, 17, 16, 4),
    "include_patterns": PATTERNS,
}
# [16:00, 16:04) 加 1 分钟：16-00、16-01 只在 ziplogs（摘要中是压缩包名），16-02 至 16-04 读非压缩文件；
# Current（16:10 起）不相交，与服务的真实时钟无关。
EXPECTED_FILES = ["smartfox.log.2026-09-17-16-00.tar.gz", "smartfox.log.2026-09-17-16-01.tar.gz",
                  "smartfox.log.2026-09-17-16-02", "smartfox.log.2026-09-17-16-03", "smartfox.log.2026-09-17-16-04"]

_TOKEN_LINE = re.compile(r"^token：(\S+)$", re.MULTILINE)


def _child_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("LOG_MCP_")}
    return {**env, "PYTHONPATH": str(REPO_ROOT), "PYTHONIOENCODING": "utf-8", **extra}


IT_ENV = "it"


def write_env_files(tmp_path: Path, work: Path, service_url: str, token: str) -> Path:
    """在 tmp_path/home 写环境登记文件（只有环境 it，权限 600），在 work 写项目文件；返回 home。"""
    home = tmp_path / "home"
    envs_file = home / ".log-viewer-mcp" / "envs.toml"
    envs_file.parent.mkdir(parents=True)
    envs_file.write_text(f'[envs.{IT_ENV}]\ndescription = "集成测试环境"\nservice_url = "{service_url}"\n'
                         f'token = "{token}"\n', encoding="utf-8")
    os.chmod(envs_file, 0o600)
    (work / ".log-viewer.toml").write_text(f'envs = ["{IT_ENV}"]\n', encoding="utf-8")
    return home


def create_token(tokens_file: Path, user: str) -> str:
    """用 Token_CLI 登记一个新 token，返回只打印一次的明文。"""
    proc = subprocess.run(
        [sys.executable, "-m", "log_viewer_mcp.token_cli", "-f", str(tokens_file), "create", user],
        cwd=REPO_ROOT, env=_child_env(), capture_output=True, timeout=30,
    )
    out = proc.stdout.decode("utf-8")
    assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")
    (token,) = _TOKEN_LINE.findall(out)
    assert token not in tokens_file.read_text(encoding="utf-8")  # 只存摘要
    return token


@pytest.fixture
def smoke(service_files: ServiceFiles) -> Iterator[tuple[RunningServer, str]]:
    token = create_token(service_files.tokens_file, SMOKE_USER)
    running = RunningServer(service_files)  # 启动时读取含新 token 的 tokens.json
    try:
        running.wait_until_listening()
        yield running, token
    finally:
        running.stop()


async def call_via_local(params: StdioServerParameters, errlog) -> tuple:
    async with stdio_client(params, errlog=errlog) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.call_tool("list_servers", {"env": IT_ENV})
            searched = await session.call_tool("search_logs", {"env": IT_ENV, **SEARCH_ARGS})
    return listed, searched


def audit_records(files: ServiceFiles) -> list[dict]:
    return [json.loads(line) for p in sorted(files.audit_dir.glob("audit-*.jsonl"))
            for line in p.read_text(encoding="utf-8").splitlines()]


@pytest.mark.asyncio
async def test_end_to_end_smoke(it_env: ItEnv, samples, game_servers: GameServers,
                                smoke: tuple[RunningServer, str], tmp_path: Path) -> None:
    server, token = smoke
    files = server.files
    work = tmp_path / "dev"
    work.mkdir()
    home = write_env_files(tmp_path, work, files.base_url, token)
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "log_viewer_mcp.local"], cwd=str(work),
        env=_child_env(HOME=str(home), LOG_MCP_RESULT_DIR="query_log"),
    )
    local_stderr = tmp_path / "local_stderr.log"

    async with game_servers.assert_unchanged(it_env.gs_ip):
        with open(local_stderr, "w", encoding="utf-8") as errlog:
            listed, searched = await asyncio.wait_for(call_via_local(params, errlog), CALL_TIMEOUT_S)

    # list_servers：与管理库中的行相同（3.1）
    assert listed.isError is False
    db_rows = await AdminDb(files.load().admin_db).list_servers()
    assert listed.structuredContent == {"env": IT_ENV, "server_timezone": "GMT-2",
                                        "log_types": listed.structuredContent["log_types"], "servers": db_rows}
    assert "normal" in listed.structuredContent["log_types"]
    assert {1001, 1002, 2001, 4294967295} <= {s["svr_id"] for s in db_rows}

    # search_logs：摘要中只有本地路径，本地文件与参照过滤逐字节相同（4.1、4.2）
    assert searched.isError is False, searched.content
    summary = searched.structuredContent
    handle = summary["result_handle"]
    assert summary["env"] == IT_ENV
    assert summary["time_range"] == {
        "start": SEARCH_ARGS["start_time"], "end": SEARCH_ARGS["end_time"],
        "start_server": "2026-09-17 16:00:00", "end_server": "2026-09-17 16:04:00",
    }
    expected = b"".join(grep_ref(sample_content(samples, n), PATTERNS) for n in EXPECTED_FILES)
    assert expected.count(b"SAMPLE_MARK") == len(EXPECTED_FILES)
    (entry,) = summary["servers"]
    local_path = entry["local_path"]
    assert "file" not in entry
    assert entry == {
        "svr_id": 1001, "status": "ok", "error": None, "local_path": local_path,
        "bytes": len(expected), "lines": expected.count(b"\n"),
        "files_read": EXPECTED_FILES, "files_skipped": [], "truncated": "none", "available_range": None,
    }
    assert os.path.isabs(local_path)
    result_dir = (work / "query_log" / IT_ENV).resolve()
    assert Path(local_path).resolve() == result_dir / handle / "1001_normal.log"
    assert Path(local_path).read_bytes() == expected
    assert sorted(p.name for p in (result_dir / handle).iterdir()) == ["1001_normal.log"]  # 没有残留的 .part
    assert "SAMPLE_MARK" not in json.dumps(summary, ensure_ascii=False)  # 响应中没有日志内容

    # 跳板机 Result_Store 中该 handle 已删除（4.3）
    assert not (files.results_dir / handle).exists()
    assert os.listdir(files.results_dir) == []

    stderr = server.stop()
    assert server.proc.returncode in (0, -signal.SIGTERM), stderr
    assert "Application shutdown complete" in stderr

    # 审计：两次调用各一条，使用者为 Token_CLI 登记的名称（6.3）
    records = audit_records(files)
    assert [(r["user"], r["tool"], r["status"]) for r in records] == [
        (SMOKE_USER, "list_servers", "ok"), (SMOKE_USER, "search_logs", "ok")]
    listed_rec, searched_rec = records
    assert listed_rec["params"] == {} and listed_rec["result_handle"] is None
    assert searched_rec["result_handle"] == handle and searched_rec["params"] == SEARCH_ARGS
    (target,) = searched_rec["targets"]
    assert (target["svr_id"], target["ip_inner"], target["status"]) == (1001, it_env.gs_ip, "ok")
    assert len(target["scripts"]) == 2

    # token 不出现在两边的运行日志与审计中（4.9、6.4）
    for text in (stderr, local_stderr.read_text(encoding="utf-8"),
                 *(p.read_text(encoding="utf-8") for p in files.audit_dir.glob("audit-*.jsonl"))):
        assert token not in text
