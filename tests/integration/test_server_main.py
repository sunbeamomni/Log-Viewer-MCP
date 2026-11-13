"""服务入口的集成冒烟测试（任务 8.6）：以子进程运行 ``python -m log_viewer_mcp.server -c config.toml``。

在测试运行容器中执行：启动检查连真实的 Admin_DB，监听 127.0.0.1 上的空闲端口。确认 /results/ 与 /mcp 没有
有效 token 时返回 401，经 SDK 的 Streamable HTTP 客户端带 Authorization 头调用 list_servers 能拿到服务器列表，
审计中记下使用者，标准错误（运行日志）中不出现 token 与数据库密码。
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from tests.integration.itenv import ServiceFiles

REPO_ROOT = Path(__file__).resolve().parents[2]
HANDLE = "q3Zr0V2m1yX8kD5sWb7aLg"
STARTUP_TIMEOUT_S = 30


class RunningServer:
    def __init__(self, files: ServiceFiles) -> None:
        self.files = files
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "log_viewer_mcp.server", "-c", str(files.config_path)],
            cwd=REPO_ROOT,
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self._stderr: str | None = None

    def wait_until_listening(self) -> None:
        deadline = time.monotonic() + STARTUP_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise AssertionError(f"服务提前退出（{self.proc.returncode}）：{self.stop()}")
            try:
                httpx.get(f"{self.files.base_url}/results/x", timeout=1)
                return
            except httpx.TransportError:
                time.sleep(0.2)
        raise AssertionError("服务没有在时限内开始监听")

    def stop(self) -> str:
        """发 SIGTERM（uvicorn 正常停机）并返回标准错误的全部内容；可重复调用。"""
        if self._stderr is None:
            if self.proc.poll() is None:
                self.proc.send_signal(signal.SIGTERM)
            try:
                _, err = self.proc.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                _, err = self.proc.communicate()
            self._stderr = err.decode("utf-8", errors="replace")
        return self._stderr


@pytest.fixture
def server(service_files: ServiceFiles) -> Iterator[RunningServer]:
    running = RunningServer(service_files)
    try:
        running.wait_until_listening()
        yield running
    finally:
        running.stop()


async def call_list_servers(base_url: str, token: str):
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30) as http:
        async with streamable_http_client(f"{base_url}/mcp", http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool("list_servers", {})


def test_server_main_auth_and_list_servers(server: RunningServer) -> None:
    files = server.files
    base = files.base_url

    for headers in ({}, {"Authorization": f"Bearer {files.revoked_token}"}, {"Authorization": "Bearer nope"}):
        r = httpx.get(f"{base}/results/{HANDLE}/1001_normal.log", headers=headers, timeout=5)
        assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
        assert httpx.post(f"{base}/mcp", headers=headers, json={}, timeout=5).status_code == 401

    r = httpx.get(f"{base}/results/{HANDLE}/1001_normal.log",
                  headers={"Authorization": f"Bearer {files.token}"}, timeout=5)
    assert r.status_code == 404

    result = asyncio.run(call_list_servers(base, files.token))
    assert result.isError is False
    svr_ids = [s["svr_id"] for s in result.structuredContent["servers"]]
    assert 1001 in svr_ids and svr_ids == sorted(svr_ids)

    stderr = server.stop()
    # uvicorn 正常停机后会把捕获的 SIGTERM 重新发给自己，所以退出状态为被 SIGTERM 结束。
    assert server.proc.returncode in (0, -signal.SIGTERM), stderr
    assert "Application startup complete" in stderr and "Application shutdown complete" in stderr
    records = [json.loads(line) for p in sorted(files.audit_dir.glob("audit-*.jsonl"))
               for line in p.read_text(encoding="utf-8").splitlines()]
    assert [(r["user"], r["tool"], r["status"]) for r in records] == [(files.user, "list_servers", "ok")]

    assert "认证失败" in stderr
    for secret in (files.token, files.revoked_token, files.password_file.read_text(encoding="utf-8").strip()):
        assert secret not in stderr


def test_server_main_refuses_unreadable_config(tmp_path) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "log_viewer_mcp.server", "-c", str(tmp_path / "missing.toml")],
        cwd=REPO_ROOT, env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        capture_output=True, timeout=30,
    )
    assert proc.returncode == 2
    assert "启动失败" in proc.stderr.decode("utf-8", errors="replace")
