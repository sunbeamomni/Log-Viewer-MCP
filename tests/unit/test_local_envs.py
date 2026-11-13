"""Local_MCP 多环境的单元测试：环境登记文件、项目文件、env 参数、按环境转发与结果目录、清理与日志脱敏。

一位研发对应多个项目，每个项目可能有线上与内网等多套跳板机服务（每套是一个环境）：

- ``~/.log-viewer-mcp/envs.toml`` 登记全部环境（description、service_url、token），``<项目根目录>/.log-viewer.toml``
  限定本项目可用的环境；没有登记文件时退回旧配置（LOG_MCP_SERVICE_URL、LOG_MCP_TOKEN → 环境 default）。
- 两个工具多一个 env 参数：多于一套时必填，不合法时不发请求；转发前去掉 env，用该环境的地址与 token。
- 结果下载到 ``query_log/<env>/<handle>/``，返回的 JSON 顶层带 env；清理逐个处理登记环境的子目录与旧版的平铺目录。

转发经 httpx.ASGITransport 打到进程内的跳板机应用（与 test_local.py 相同），由审计记录确认用的是哪个 token、
收到的参数中没有 env。另有一个经 stdio 启动 ``python -m log_viewer_mcp.local`` 的测试，确认登记文件与项目文件
在子进程中按 HOME 与工作目录读取。
"""

from __future__ import annotations

import io
import logging
import os
import pathlib
import sys
import time

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from log_viewer_mcp.local.main import (
    DEFAULT_ENVS_FILE,
    ENV_ENVS_FILE,
    PROJECT_FILE,
    Environment,
    LocalMCP,
    LocalSettings,
    SettingsError,
    configure_logging,
    load_settings,
    main,
)
from tests.unit.test_app import AT
from tests.unit.test_app_http import OTHER_TOKEN, VALID_TOKEN
from tests.unit.test_local import REPO_ROOT, audit_lines, payload, served  # noqa: F401 - served 是 fixture
from tests.unit.test_local_cleanup import TTL_H, make_dir

PROD_TOKEN = "prod-token-P1_secret"
DEV_TOKEN = "dev-token-D2_secret"
B_TOKEN = "b-token-B3_secret"

ENVS_TOML = f"""\
[envs.a-prod]
description = "A 项目线上"
service_url = "http://prod.test:8999/"
token = "{PROD_TOKEN}"

[envs.a-dev]
description = "  A 项目内网  "
service_url = "http://dev.test:8999"
token = "{DEV_TOKEN}"

[envs.b-dev]
description = "B 项目内网"
service_url = "https://b.test/base"
token = "{B_TOKEN}"
"""

H1 = "aaaaaaaaaaaaaaaaaaaaa1"
H2 = "bbbbbbbbbbbbbbbbbbbbb2"
H3 = "ccccccccccccccccccccc3"
H4 = "ddddddddddddddddddddd4"


def write(path: pathlib.Path, text: str) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if os.name == "posix":
        os.chmod(path, 0o600)
    return path


def load(tmp_path: pathlib.Path, envs_text: str | None = ENVS_TOML, project_text: str | None = None,
         environ: dict | None = None) -> LocalSettings:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    if envs_text is not None:
        write(home / DEFAULT_ENVS_FILE, envs_text)
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    if project_text is not None:
        write(project / PROJECT_FILE, project_text)
    return load_settings(environ or {}, cwd=project, home=home)


def load_error(tmp_path: pathlib.Path, **kwargs) -> str:
    with pytest.raises(SettingsError) as e:
        load(tmp_path, **kwargs)
    return str(e.value)


def two_envs(tmp_path: pathlib.Path, prod_url: str = "http://prod.test", dev_url: str = "http://dev.test",
             registered: tuple[str, ...] = ()) -> LocalSettings:
    """a-prod 用 VALID_TOKEN（审计中的使用者 zhangsan），a-dev 用 OTHER_TOKEN（wangwu）。"""
    envs = (Environment("a-prod", "A 项目线上", prod_url, VALID_TOKEN),
            Environment("a-dev", "A 项目内网", dev_url, OTHER_TOKEN))
    return LocalSettings(envs=envs, result_dir=tmp_path / "query_log", call_timeout_s=10.0, result_ttl_h=TTL_H,
                         registered_env_names=registered)


# ---------------------------------------------------------------------------
# 环境登记文件与项目文件
# ---------------------------------------------------------------------------


def test_envs_file_registers_all_envs_in_order(tmp_path) -> None:
    s = load(tmp_path)
    assert s.env_names == ("a-prod", "a-dev", "b-dev")
    assert s.registered_env_names == s.env_names
    prod, dev, b = s.envs
    assert (prod.description, prod.service_url, prod.token) == ("A 项目线上", "http://prod.test:8999", PROD_TOKEN)
    assert dev.description == "A 项目内网"  # 去掉首尾空白
    assert b.mcp_url == "https://b.test/base/mcp" and b.results_url == "https://b.test/base/results"
    assert s.envs_file == tmp_path / "home" / DEFAULT_ENVS_FILE and s.project_file is None
    assert s.result_dir == tmp_path / "project" / "query_log"
    assert s.warnings == ()
    text = repr(s)
    for token in (PROD_TOKEN, DEV_TOKEN, B_TOKEN):
        assert token not in text


def test_project_file_selects_envs_in_its_order(tmp_path) -> None:
    s = load(tmp_path, project_text='envs = ["a-dev", "a-prod"]\n')
    assert s.env_names == ("a-dev", "a-prod")
    assert s.registered_env_names == ("a-prod", "a-dev", "b-dev")  # 清理时仍处理 b-dev 的子目录
    assert s.project_file == tmp_path / "project" / PROJECT_FILE
    assert s.tokens == (DEV_TOKEN, PROD_TOKEN)


def test_project_file_with_unregistered_env_fails(tmp_path) -> None:
    text = load_error(tmp_path, project_text='envs = ["a-prod", "c-prod", "c-dev"]\n')
    assert "c-prod、c-dev" in text and "未在环境登记文件" in text
    assert PROD_TOKEN not in text


def test_legacy_env_works_with_project_file_naming_default(tmp_path) -> None:
    legacy = {"LOG_MCP_SERVICE_URL": "http://old.test:8999", "LOG_MCP_TOKEN": "legacy-token-L1"}
    s = load(tmp_path, envs_text=None, project_text='envs = ["default"]\n', environ=legacy)
    assert s.env_names == ("default",) and s.envs_file is None
    text = load_error(tmp_path, envs_text=None, project_text='envs = ["a-prod"]\n', environ=legacy)
    assert "a-prod" in text and "该文件不存在" in text and "legacy-token-L1" not in text


@pytest.mark.parametrize(
    "envs_text, fragment",
    [
        ("", "没有登记任何环境"),
        ("[settings]\nx = 1\n", "未知的项：settings"),
        ('[envs.A-Prod]\ndescription = "d"\nservice_url = "http://h"\ntoken = "SECRET-T0"\n', "环境名 'A-Prod' 不合法"),
        ('[envs.abcdefghijklmnopqrstu]\ndescription = "d"\nservice_url = "http://h"\ntoken = "SECRET-T0"\n',
         "不合法"),  # 21 个字符
        ('[envs.-x]\ndescription = "d"\nservice_url = "http://h"\ntoken = "SECRET-T0"\n', "不合法"),
        ('[envs.a]\ndescription = "d"\nservice_url = "http://h"\n', "a 的 token 缺失"),
        ('[envs.a]\ndescription = "d"\nservice_url = "http://h"\ntoken = "SECRET T0"\n', "a 的 token"),
        ('[envs.a]\ndescription = "d"\nservice_url = "http://h"\ntoken = 12345\n', "a 的 token"),
        ('[envs.a]\ndescription = "d"\nservice_url = "ftp://SECRET-host"\ntoken = "t"\n', "a 的 service_url"),
        ('[envs.a]\ndescription = "d"\nservice_url = "http://u:SECRET@h"\ntoken = "t"\n', "a 的 service_url"),
        ('[envs.a]\nservice_url = "http://h"\ntoken = "t"\n', "a 的 description"),
        ('[envs.a]\ndescription = "第一行\\n第二行"\nservice_url = "http://h"\ntoken = "t"\n', "a 的 description"),
        ('[envs.a]\ndescription = "d"\nservice_url = "http://h"\ntoken = "t"\ntokn = "SECRET-T0"\n', "未知的项：tokn"),
        ('[envs.a]\ndescription = "d"\nservice_url = "http://h"\ntoken = "SECRET-T0\n', "不是合法的 TOML"),
        ('envs = "SECRET-T0"\n', "没有登记任何环境"),
        ("envs = 1\n[envs.a]\n", "不是合法的 TOML"),
    ],
)
def test_invalid_envs_file_is_reported_without_values(tmp_path, envs_text, fragment) -> None:
    text = load_error(tmp_path, envs_text=envs_text)
    assert fragment in text
    assert "SECRET" not in text


def test_all_problems_are_reported_together(tmp_path) -> None:
    envs_text = ('[envs.a]\ndescription = "d"\nservice_url = "nope"\ntoken = "t"\n'
                 '[envs.b]\ndescription = "d"\nservice_url = "http://h"\n')
    text = load_error(tmp_path, envs_text=envs_text, environ={"LOG_MCP_CALL_TIMEOUT_S": "0"})
    assert "a 的 service_url" in text and "b 的 token" in text and "LOG_MCP_CALL_TIMEOUT_S" in text


@pytest.mark.parametrize(
    "project_text, fragment",
    [
        ("envs = []\n", "至少一个"),
        ('envs = "a-prod"\n', "至少一个"),
        ("envs = [1]\n", "至少一个"),
        ('envs = ["a-prod", "a-prod"]\n', "有重复"),
        ('envs = ["A-PROD"]\n', "不合法"),
        ('envs = ["a-prod"]\ndefault = "a-prod"\n', "未知的项：default"),
        ("x = \n", "不是合法的 TOML"),
        ("", "至少一个"),
    ],
)
def test_invalid_project_file_fails(tmp_path, project_text, fragment) -> None:
    assert fragment in load_error(tmp_path, project_text=project_text)


def test_explicit_envs_file_must_exist_and_is_relative_to_cwd(tmp_path) -> None:
    text = load_error(tmp_path, envs_text=None, environ={ENV_ENVS_FILE: "missing.toml"})
    assert "无法读取环境登记文件" in text and str(tmp_path / "project" / "missing.toml") in text

    write(tmp_path / "project" / "conf" / "envs.toml", ENVS_TOML)
    s = load(tmp_path, envs_text=None, environ={ENV_ENVS_FILE: os.path.join("conf", "envs.toml")})
    assert s.env_names == ("a-prod", "a-dev", "b-dev")
    assert s.envs_file == tmp_path / "project" / "conf" / "envs.toml"


def test_envs_file_takes_precedence_over_legacy_variables(tmp_path) -> None:
    s = load(tmp_path, environ={"LOG_MCP_SERVICE_URL": "http://old.test", "LOG_MCP_TOKEN": "legacy-token-L1"})
    assert s.env_names == ("a-prod", "a-dev", "b-dev")
    (warning,) = s.warnings
    assert "忽略环境变量 LOG_MCP_SERVICE_URL 与 LOG_MCP_TOKEN" in warning and "legacy-token-L1" not in warning


@pytest.mark.skipif(os.name != "posix", reason="Windows 上不检查文件的 mode 位")
def test_world_readable_envs_file_gives_warning(tmp_path) -> None:
    load(tmp_path)
    os.chmod(tmp_path / "home" / DEFAULT_ENVS_FILE, 0o644)
    s = load_settings({}, cwd=tmp_path / "project", home=tmp_path / "home")
    (warning,) = s.warnings
    assert "chmod 600" in warning


def test_main_reports_settings_errors_without_tokens(tmp_path, capsys, monkeypatch) -> None:
    home = tmp_path / "home"
    write(home / DEFAULT_ENVS_FILE, ENVS_TOML)
    project = tmp_path / "project"
    write(project / PROJECT_FILE, 'envs = ["a-prod", "z-dev"]\n')
    monkeypatch.chdir(project)
    code = main({}, home=home)
    out, err = capsys.readouterr()
    assert code == 2 and out == ""
    assert "z-dev" in err
    for token in (PROD_TOKEN, DEV_TOKEN, B_TOKEN):
        assert token not in err


# ---------------------------------------------------------------------------
# 工具参数
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tools_require_env_when_project_has_several(tmp_path) -> None:
    local = LocalMCP(two_envs(tmp_path))
    tools = {t.name: t.inputSchema for t in await local.list_tools()}
    for name, schema in tools.items():
        env = schema["properties"]["env"]
        assert list(schema["properties"])[0] == "env", name
        assert env["type"] == "string" and env["enum"] == ["a-prod", "a-dev"]
        assert "a-prod：A 项目线上" in env["description"] and "a-dev：A 项目内网" in env["description"]
        assert "先问用户" in env["description"]
        assert schema["required"][0] == "env"
    assert tools["search_logs"]["required"] == ["env", "svr_ids", "log_type"]
    assert tools["list_servers"]["required"] == ["env"]


def _no_request(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不应被调用
    raise AssertionError(f"不应发出请求：{request.url}")


@pytest.mark.parametrize("arguments, fragment", [
    ({}, "缺少 env"),
    ({"env": None}, "缺少 env"),
    ({"env": "b-dev"}, "env 不合法"),
    ({"env": "A-PROD"}, "env 不合法"),
    ({"env": 1}, "env 不合法"),
    ({"env": ["a-prod"]}, "env 不合法"),
])
@pytest.mark.parametrize("tool", ["search_logs", "list_servers"])
@pytest.mark.asyncio
async def test_missing_or_unknown_env_is_rejected_without_requests(tmp_path, tool, arguments, fragment) -> None:
    now = time.time()
    expired = make_dir(tmp_path / "query_log" / "a-prod" / H1, TTL_H + 1, now)
    local = LocalMCP(two_envs(tmp_path), transport=httpx.MockTransport(_no_request))
    result = await local.call_tool(tool, {"svr_ids": [1001], "log_type": "normal", **arguments})
    assert result.isError is True
    data = payload(result)
    assert "env" not in data
    assert data["error"]["code"] == "INVALID_ARGUMENT"
    assert fragment in data["error"]["message"] and "a-prod、a-dev" in data["error"]["message"]
    assert expired.exists()  # 参数不合法时不清理


# ---------------------------------------------------------------------------
# 按环境转发
# ---------------------------------------------------------------------------


class HostRecorder(httpx.AsyncBaseTransport):
    """记录每个请求的 (host, 方法, 路径, Authorization) 后交给内层传输。"""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner
        self.requests: list[tuple[str, str, str, str | None]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.url.host, request.method, request.url.path,
                              request.headers.get("authorization")))
        return await self.inner.handle_async_request(request)


@pytest.mark.asyncio
async def test_each_env_uses_its_own_address_and_token(served, tmp_path) -> None:
    env, transport = served
    recorder = HostRecorder(transport)
    local = LocalMCP(two_envs(tmp_path), transport=recorder)

    listed = await local.call_tool("list_servers", {"env": "a-dev"})
    assert listed.isError is False
    data = payload(listed)
    assert list(data)[0] == "env" and data["env"] == "a-dev"
    assert data["servers"][0]["svr_id"] == 1001 and data["server_timezone"] == "GMT-2"
    assert {(h, a) for h, _, _, a in recorder.requests} == {("dev.test", f"Bearer {OTHER_TOKEN}")}

    recorder.requests.clear()
    arguments = {"svr_ids": [1001], "env": "a-prod", "log_type": "normal", "at_time": AT}
    searched = await local.call_tool("search_logs", dict(arguments))
    assert searched.isError is False
    data = payload(searched)
    assert data["env"] == "a-prod"
    handle = data["result_handle"]
    (entry,) = data["servers"]
    assert pathlib.Path(entry["local_path"]) == tmp_path / "query_log" / "a-prod" / handle / "1001_normal.log"
    assert pathlib.Path(entry["local_path"]).stat().st_size == entry["bytes"] > 0
    assert {(h, a) for h, _, _, a in recorder.requests} == {("prod.test", f"Bearer {VALID_TOKEN}")}
    assert [(m, p) for _, m, p, _ in recorder.requests if p != "/mcp"] == [
        ("GET", f"/results/{handle}/1001_normal.log"), ("DELETE", f"/results/{handle}")]

    # 跳板机按 token 记使用者；收到的参数中没有 env，其余参数与顺序不变。
    list_rec, search_rec = audit_lines(env)
    assert (list_rec["user"], list_rec["tool"], list_rec["params"]) == ("wangwu", "list_servers", {})
    assert search_rec["user"] == "zhangsan"
    assert search_rec["params"] == {k: v for k, v in arguments.items() if k != "env"}
    assert list(search_rec["params"]) == ["svr_ids", "log_type", "at_time"]


@pytest.mark.asyncio
async def test_server_error_result_carries_env(served, tmp_path) -> None:
    _, transport = served
    local = LocalMCP(two_envs(tmp_path), transport=transport)
    result = await local.call_tool("search_logs", {"env": "a-dev", "svr_ids": [1001], "log_type": "nope"})
    assert result.isError is True
    data = payload(result)
    assert data["env"] == "a-dev" and data["error"]["code"] == "INVALID_ARGUMENT"


@pytest.mark.asyncio
async def test_unreachable_env_reports_service_unavailable_with_env(tmp_path, caplog) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    local = LocalMCP(two_envs(tmp_path), transport=httpx.MockTransport(refuse))
    with caplog.at_level(logging.DEBUG):
        result = await local.call_tool("list_servers", {"env": "a-prod"})
    data = payload(result)
    assert result.isError is True and data == {
        "env": "a-prod", "error": {"code": "SERVICE_UNAVAILABLE", "message": data["error"]["message"]}}
    assert "a-prod" in caplog.text


# ---------------------------------------------------------------------------
# 清理与日志
# ---------------------------------------------------------------------------


def test_cleanup_handles_every_registered_env_and_legacy_dirs(tmp_path) -> None:
    now = time.time()
    root = tmp_path / "query_log"
    make_dir(root / "a-prod" / H1, TTL_H + 1, now, {"1001_normal.log": b"x"})
    make_dir(root / "b-dev" / H2, TTL_H + 1, now)  # 本项目不用、但已登记的环境
    make_dir(root / H3, TTL_H + 1, now)  # 旧版本的平铺目录
    fresh = make_dir(root / "a-dev" / H4, 1, now)
    unknown = make_dir(root / "c-dev" / H1, TTL_H + 1, now)  # 未登记的环境：不动
    settings = two_envs(tmp_path, registered=("a-prod", "a-dev", "b-dev"))

    removed = LocalMCP(settings).cleanup_results()

    assert sorted(removed) == sorted([H3, f"a-prod/{H1}", f"b-dev/{H2}"])
    assert fresh.is_dir() and unknown.is_dir()
    assert (root / "a-prod").is_dir() and (root / "b-dev").is_dir()  # 环境目录本身保留


def test_cleanup_does_not_follow_symlinked_env_dir(tmp_path) -> None:
    now = time.time()
    outside = make_dir(tmp_path / "outside" / H1, TTL_H + 1, now, {"keep.log": b"x"})
    root = tmp_path / "query_log"
    root.mkdir()
    try:
        os.symlink(outside.parent, root / "a-prod", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不能创建符号链接")
    assert LocalMCP(two_envs(tmp_path)).cleanup_results() == []
    assert (outside / "keep.log").exists()


def test_configure_logging_redacts_every_env_token() -> None:
    root = logging.getLogger()
    saved = (root.handlers[:], root.level, logging.getLogger("httpx").level, logging.getLogger("httpcore").level)
    stream = io.StringIO()
    short, long = "tok-AB12", "tok-AB12-longer-token"  # 一个是另一个的前缀
    try:
        configure_logging([short, long, DEV_TOKEN], stream=stream)
        logging.getLogger("log_viewer_mcp.local.test").warning("a=%s b=%s c=%s", long, short, DEV_TOKEN)
    finally:
        root.handlers[:] = saved[0]
        root.setLevel(saved[1])
        logging.getLogger("httpx").setLevel(saved[2])
        logging.getLogger("httpcore").setLevel(saved[3])
    text = stream.getvalue()
    assert "a=*** b=*** c=***" in text
    assert "AB12" not in text and DEV_TOKEN not in text


# ---------------------------------------------------------------------------
# stdio：子进程按 HOME 与工作目录读取登记文件与项目文件
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stdio_reads_envs_file_from_home_and_project_file_from_cwd(tmp_path) -> None:
    home = tmp_path / "home"
    write(home / DEFAULT_ENVS_FILE, ENVS_TOML.replace("http://dev.test:8999", "http://127.0.0.1:9"))
    project = tmp_path / "project"
    write(project / PROJECT_FILE, 'envs = ["a-dev", "a-prod"]\n')
    env = {k: v for k, v in os.environ.items() if not k.startswith("LOG_MCP_")}
    env.update(PYTHONPATH=str(REPO_ROOT), HOME=str(home), USERPROFILE=str(home), LOG_MCP_CALL_TIMEOUT_S="5")
    params = StdioServerParameters(command=sys.executable, args=["-m", "log_viewer_mcp.local"], env=env,
                                   cwd=str(project))
    errlog = tmp_path / "stderr.txt"
    with open(errlog, "w", encoding="utf-8") as err:
        async with stdio_client(params, errlog=err) as (read, write_stream):
            async with ClientSession(read, write_stream) as session:
                await session.initialize()
                tools = {t.name: t.inputSchema for t in (await session.list_tools()).tools}
                missing = await session.call_tool("list_servers", {})
                unreachable = await session.call_tool("list_servers", {"env": "a-dev"})
    assert tools["search_logs"]["properties"]["env"]["enum"] == ["a-dev", "a-prod"]
    assert tools["search_logs"]["required"] == ["env", "svr_ids", "log_type"]
    assert payload(missing)["error"]["code"] == "INVALID_ARGUMENT"
    assert payload(unreachable)["env"] == "a-dev"
    assert payload(unreachable)["error"]["code"] == "SERVICE_UNAVAILABLE"
    stderr = errlog.read_text(encoding="utf-8", errors="replace")
    assert "a-dev=http://127.0.0.1:9" in stderr
    for token in (PROD_TOKEN, DEV_TOKEN, B_TOKEN):
        assert token not in stderr
    assert not (project / "query_log").exists()
