"""tokens.json 变化后重新读取的单元测试（任务 14.5；需求 6.1、6.2、6.6、6.7）。

认证中间件包住一个只返回 200 的 ASGI 应用，经 httpx 的 ASGITransport 发请求；tokens.json 由 Token_CLI 的函数
（同目录临时文件加 os.replace）或测试中同样以 os.replace 写入。每一步写入的内容大小都与上一步不同，不依赖
st_ino 与 mtime 的精度。最后一个用例经 build_asgi_app 组装的完整应用确认接线。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import logging
import os
import re
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from starlette.responses import PlainTextResponse

from log_viewer_mcp.server import tokens as tokens_module
from log_viewer_mcp.server.app import BearerAuthMiddleware, build_asgi_app, create_mcp
from log_viewer_mcp.server.config import TokenRecord
from log_viewer_mcp.server.tokens import TokenSet
from log_viewer_mcp.token_cli import main as tcli
from tests.unit.test_app import Env, accept_all
from tests.unit.test_app_http import lifespan

LOGGER = tokens_module.logger.name
TOKEN_LINE = re.compile(r"^token：(\S+)$", re.M)
READ_FAILED = "重新读取 token 登记文件失败"
MISSING = "不存在"


async def ok_app(scope, receive, send) -> None:
    await PlainTextResponse("ok")(scope, receive, send)


@contextlib.asynccontextmanager
async def serve(path: Path) -> AsyncIterator[httpx.AsyncClient]:
    mw = BearerAuthMiddleware(ok_app, TokenSet(str(path)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mw), base_url="http://testserver") as c:
        yield c


async def status(client: httpx.AsyncClient, token: str) -> int:
    response = await client.get("/results/x", headers={"Authorization": f"Bearer {token}"})
    return response.status_code


def create(path: Path, user: str) -> str:
    out = io.StringIO()
    assert tcli.cmd_create(str(path), user, out) == 0
    (token,) = TOKEN_LINE.findall(out.getvalue())
    return token


def revoke(path: Path, token: str) -> None:
    digest = hashlib.sha256(token.encode()).hexdigest()
    (record,) = [r for r in tcli.load_tokens(str(path), missing_ok=False) if r.sha256 == digest]
    assert tcli.cmd_revoke(str(path), record.token_id, io.StringIO()) == 0


def replace_file(path: Path, data: bytes) -> None:
    """与 Token_CLI 一样经同目录临时文件加 os.replace 写入。"""
    tmp = path.with_name(".tokens.test.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def record(token: str, token_id: str, user: str) -> TokenRecord:
    return TokenRecord(token_id, user, hashlib.sha256(token.encode()).hexdigest(), "2026-09-01T00:00:00-02:00", False)


def messages(caplog, level: int | None = None) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == LOGGER and (level is None or r.levelno == level)]


@pytest.fixture
def path(tmp_path) -> Path:
    return tmp_path / "tokens.json"


@pytest.fixture
def logs(caplog) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.DEBUG)
    return caplog


@pytest.mark.asyncio
async def test_missing_at_startup_then_create_takes_effect_immediately(path, logs) -> None:
    async with serve(path) as client:
        assert [m for m in messages(logs, logging.WARNING) if MISSING in m]  # 启动时写 warning
        assert await status(client, "any-token") == 401

        first = create(path, "zhangsan")
        assert await status(client, first) == 200

        second = create(path, "lisi")
        assert await status(client, first) == 200
        assert await status(client, second) == 200
    assert any("有效 token 2 个" in m for m in messages(logs, logging.INFO))


@pytest.mark.asyncio
async def test_revoke_takes_effect_immediately(path) -> None:
    first = create(path, "zhangsan")
    second = create(path, "lisi")
    async with serve(path) as client:
        assert await status(client, first) == 200
        revoke(path, first)
        assert await status(client, first) == 401
        assert await status(client, second) == 200


@pytest.mark.asyncio
async def test_corrupt_file_keeps_previous_set_and_warns_once_per_state(path, logs) -> None:
    good = create(path, "zhangsan")
    async with serve(path) as client:
        assert await status(client, good) == 200

        replace_file(path, b"{not json")
        for _ in range(3):
            assert await status(client, good) == 200
        assert len([m for m in messages(logs, logging.WARNING) if READ_FAILED in m]) == 1

        # 文件再变化时重试：恢复为合法内容（只有另一个 token）即按新内容认证。
        other = "other-token-Ghi_789-rst"
        replace_file(path, tcli.serialize_tokens([record(other, "t2", "wangwu")]))
        assert await status(client, good) == 401
        assert await status(client, other) == 200

        # 另一个无法解析的状态再警告一次，仍沿用最近一次成功读取的集合。
        replace_file(path, b'[{"token_id": "x"}]')
        assert await status(client, other) == 200
        assert await status(client, other) == 200
        assert len([m for m in messages(logs, logging.WARNING) if READ_FAILED in m]) == 2


@pytest.mark.asyncio
async def test_deleted_file_rejects_every_token(path, logs) -> None:
    first = create(path, "zhangsan")
    second = create(path, "lisi")
    async with serve(path) as client:
        assert await status(client, first) == 200
        os.remove(path)
        assert await status(client, first) == 401
        assert await status(client, second) == 401
        assert [m for m in messages(logs, logging.WARNING) if MISSING in m]

        third = create(path, "wangwu")  # 文件重新出现即按其内容认证
        assert await status(client, third) == 200
        assert await status(client, first) == 401


@pytest.mark.asyncio
async def test_logs_contain_no_token_or_digest(path, logs) -> None:
    async with serve(path) as client:
        first = create(path, "zhangsan")
        await status(client, first)
        second = create(path, "lisi")
        await status(client, second)
        revoke(path, first)
        await status(client, first)
        replace_file(path, b"[" + path.read_bytes())  # 内容无法解析
        await status(client, second)
        os.remove(path)
        await status(client, second)

    text = "\n".join(r.getMessage() for r in logs.records)
    assert "有效 token" in text and READ_FAILED in text
    for token in (first, second):
        assert token not in text
        assert hashlib.sha256(token.encode()).hexdigest() not in text


def test_one_stat_per_authentication(path, monkeypatch) -> None:
    token = create(path, "zhangsan")
    mw = BearerAuthMiddleware(ok_app, TokenSet(str(path)))
    calls = []
    real_stat = os.stat

    def counting_stat(p, *args, **kwargs):
        if os.fspath(p) == str(path):
            calls.append(p)
        return real_stat(p, *args, **kwargs)

    monkeypatch.setattr(tokens_module.os, "stat", counting_stat)
    headers = [(b"authorization", f"Bearer {token}".encode())]
    assert mw.authenticate(headers) == ("zhangsan", token)
    assert mw.authenticate([]) is None
    assert len(calls) == 2


def test_unreadable_file_at_construction_falls_back_to_given_tokens(path, logs) -> None:
    token = "startup-token-Abc_123"
    path.write_bytes(b"{not json")  # 启动检查之后才被写坏
    token_set = TokenSet(str(path), (record(token, "t1", "zhangsan"),))
    assert token_set.active_count == 1
    assert len([m for m in messages(logs, logging.WARNING) if READ_FAILED in m]) == 1
    token_set.refresh()
    assert len([m for m in messages(logs, logging.WARNING) if READ_FAILED in m]) == 1


@pytest.mark.asyncio
async def test_build_asgi_app_follows_tokens_file(tmp_path) -> None:
    env = Env(tmp_path)
    tokens_file = Path(env.config.tokens_file)
    token = create(tokens_file, "zhangsan")
    config = env.config_with_tokens(tuple(tcli.load_tokens(str(tokens_file), missing_ok=False)))
    app = build_asgi_app(create_mcp(config, env.services, env.audit, regex_checker=accept_all))
    async with lifespan(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
            url = "/results/q3Zr0V2m1yX8kD5sWb7aLg/1001_normal.log"
            headers = {"Authorization": f"Bearer {token}"}
            assert (await c.get(url, headers=headers)).status_code == 404  # 认证通过，结果不存在
            revoke(tokens_file, token)
            assert (await c.get(url, headers=headers)).status_code == 401
