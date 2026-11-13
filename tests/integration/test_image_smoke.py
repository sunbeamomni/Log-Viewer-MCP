"""生产镜像的集成测试（任务 14.12，需求 4.2、6.3、8.8、10.1、10.2、10.7）。

compose 的 server-image 服务由 deploy/Dockerfile 构建，以镜像自己的 ENTRYPOINT 与 CMD（root、
``-c /etc/log-viewer-mcp/config.toml``）运行；Config_Dir 为 docker/server-image/config（tokens.json 用默认路径，
预置一个测试 token），私钥取自 keys 卷，不配置 known_hosts。runner 等它健康后才启动。

在测试运行容器中：
- 不带 token 访问 /mcp 返回 401；
- 经 stdio 启动 log-viewer-mcp-local（LOG_MCP_SERVICE_URL 指向该服务），依次调用 list_servers 与 search_logs
  （与 test_e2e_smoke 相同的参数，时间窗口与真实时钟无关），本地结果文件与参照过滤逐字节相同；
- 审计（以 root 写入的 0600 文件，经 ``sudo -n it-image-audit`` 读取只读挂载的 /image-audit）中有这两次调用的记录。

服务在多次 ``docker compose run`` 之间持续运行，审计会累积；测试只看本次调用前后新增的记录。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from mcp.client.stdio import StdioServerParameters

from log_viewer_mcp.server.config import AdminDbConfig
from log_viewer_mcp.server.db import AdminDb
from tests.integration.itenv import ItEnv
from tests.integration.test_e2e_smoke import (
    CALL_TIMEOUT_S,
    EXPECTED_FILES,
    PATTERNS,
    SEARCH_ARGS,
    _child_env,
    call_via_local,
)
from tests.integration.test_remote_e2e import grep_ref
from tests.integration.test_tools_integration import sample_content

IMAGE_USER = "it-image"  # docker/server-image/config/tokens.json 中登记的使用者
AUDIT_HELPER = "/usr/local/bin/it-image-audit"


@pytest.fixture(scope="module")
def image_url() -> str:
    return os.environ["LVM_IT_IMAGE_URL"]


@pytest.fixture(scope="module")
def image_token() -> str:
    return os.environ["LVM_IT_IMAGE_TOKEN"]


def image_audit_lines() -> list[str]:
    """以 root 读取 server-image 的全部审计行（按文件日期顺序）。"""
    proc = subprocess.run(["sudo", "-n", AUDIT_HELPER], capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")
    return proc.stdout.decode("utf-8").splitlines()


def test_mcp_without_token_is_401(it_env: ItEnv, image_url: str) -> None:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    headers = {"Accept": "application/json, text/event-stream"}
    resp = httpx.post(f"{image_url}/mcp", json=body, headers=headers, timeout=10)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_image_smoke_via_local(it_env: ItEnv, samples, image_url: str, image_token: str,
                                     tmp_path: Path) -> None:
    command = shutil.which("log-viewer-mcp-local")
    assert command is not None, "测试运行镜像中应已安装 log-viewer-mcp-local"
    work = tmp_path / "dev"
    work.mkdir()
    params = StdioServerParameters(
        command=command, args=[], cwd=str(work),
        env=_child_env(LOG_MCP_SERVICE_URL=image_url, LOG_MCP_TOKEN=image_token, LOG_MCP_RESULT_DIR="query_log"),
    )
    local_stderr = tmp_path / "local_stderr.log"

    before = image_audit_lines()
    with open(local_stderr, "w", encoding="utf-8") as errlog:
        listed, searched = await asyncio.wait_for(call_via_local(params, errlog), CALL_TIMEOUT_S)
    after = image_audit_lines()

    # list_servers：与管理库中的行相同
    assert listed.isError is False, listed.content
    db = AdminDbConfig(host=it_env.db_host, port=it_env.db_port, user=it_env.db_user,
                       database=it_env.db_name, password_file=None, password=it_env.db_password)
    db_rows = await AdminDb(db).list_servers()
    assert listed.structuredContent == {"servers": db_rows}

    # search_logs：未配置 known_hosts 时照常登录 gs（8.8），本地文件与参照过滤逐字节相同（4.2）
    assert searched.isError is False, searched.content
    summary = searched.structuredContent
    handle = summary["result_handle"]
    expected = b"".join(grep_ref(sample_content(samples, n), PATTERNS) for n in EXPECTED_FILES)
    (entry,) = summary["servers"]
    assert (entry["svr_id"], entry["status"], entry["error"]) == (1001, "ok", None)
    assert entry["files_read"] == EXPECTED_FILES and entry["files_skipped"] == []
    assert entry["bytes"] == len(expected) and entry["truncated"] == "none"
    result_dir = (work / "query_log").resolve()
    local_path = Path(entry["local_path"])
    assert local_path.is_absolute() and local_path.resolve() == result_dir / handle / "1001_normal.log"
    assert local_path.read_bytes() == expected
    assert sorted(p.name for p in (result_dir / handle).iterdir()) == ["1001_normal.log"]

    # 审计：本次两次调用各一条，使用者为 tokens.json 中登记的名称（6.3）
    assert after[:len(before)] == before, "审计只应追加"
    new_lines = after[len(before):]
    records = [json.loads(line) for line in new_lines]
    assert [(r["user"], r["tool"], r["status"]) for r in records] == [
        (IMAGE_USER, "list_servers", "ok"), (IMAGE_USER, "search_logs", "ok")]
    listed_rec, searched_rec = records
    assert listed_rec["params"] == {} and listed_rec["result_handle"] is None
    assert searched_rec["result_handle"] == handle and searched_rec["params"] == SEARCH_ARGS
    (target,) = searched_rec["targets"]
    assert (target["svr_id"], target["ip_inner"], target["status"]) == (1001, it_env.gs_ip, "ok")

    # token 不出现在审计与 Local_MCP 的输出中
    for text in ("\n".join(new_lines), local_stderr.read_text(encoding="utf-8")):
        assert image_token not in text
