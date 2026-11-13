"""客户端配置示例、SKILL.md 与 README 的检查（任务 12.3；需求 1.8、4.1、4.9、4.11）。

- examples/ 下 Kiro、Claude Code、Codex 的配置能被解析，只写启动 Local_MCP 的命令，不含地址与 token；
  环境登记文件 envs.toml 的模板只有占位符，把占位符换成实际值后 Local_MCP 能读取它与项目文件 .log-viewer.toml。
- SKILL.md 的 YAML 头只含简单的 name 与 description（不依赖 PyYAML：逐行按 `键: 值` 解析，并排除会改变
  YAML 含义的写法），正文与 search_logs 的工具说明（common/tool_docs.py）一致。
- README 中的相对链接都指向仓库中存在的文件。
"""

import json
import re
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from log_viewer_mcp.common.tool_docs import LIST_SERVERS, SEARCH_LOGS, SEARCH_LOGS_DESCRIPTION
from log_viewer_mcp.local import main as local_main

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples"
SKILL_DIR = ROOT / "skills" / "log-viewer"
SKILL_FILE = SKILL_DIR / "SKILL.md"
README = ROOT / "README.md"

SERVER_NAME = "log-viewer"
ENVS_EXAMPLE = EXAMPLES / "envs.toml"
PROJECT_EXAMPLE = EXAMPLES / ".log-viewer.toml"
# envs.toml 模板中的占位符：地址形如 <A 线上跳板机地址>，token 形如 <A 线上的 token>。
HOST_PLACEHOLDER = re.compile(r"<[^<>]*跳板机地址>")
TOKEN_PLACEHOLDER = re.compile(r"<[^<>]*token>")


def _example_files() -> list[Path]:
    return [EXAMPLES / "kiro" / "mcp.json", EXAMPLES / "claude-code" / ".mcp.json", EXAMPLES / "codex" / "config.toml"]


def _kiro():
    return json.loads((EXAMPLES / "kiro" / "mcp.json").read_text(encoding="utf-8"))["mcpServers"][SERVER_NAME]


def _claude_code():
    return json.loads((EXAMPLES / "claude-code" / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"][SERVER_NAME]


def _codex():
    with open(EXAMPLES / "codex" / "config.toml", "rb") as f:
        return tomllib.load(f)["mcp_servers"][SERVER_NAME]


CLIENT_CONFIGS = pytest.mark.parametrize("load", [_kiro, _claude_code, _codex], ids=["kiro", "claude-code", "codex"])


# ---------------------------------------------------------------------------
# 客户端配置示例
# ---------------------------------------------------------------------------


@CLIENT_CONFIGS
def test_example_starts_local_mcp_over_stdio(load):
    # 前提是已执行 uv tool install git+<仓库地址>：直接启动安装好的命令，不带参数。
    server = load()
    assert server["command"] == "log-viewer-mcp-local"
    assert server.get("args", []) == []
    assert server.get("type", "stdio") == "stdio"


def test_examples_do_not_use_uvx():
    for path in _example_files():
        assert "uvx" not in path.read_text(encoding="utf-8"), path


@CLIENT_CONFIGS
def test_example_has_no_address_or_token(load):
    # 地址与 token 只写在 envs.toml 中；客户端配置可以提交到仓库（Claude Code 的项目级 .mcp.json）。
    server = load()
    assert "env" not in server


def test_client_examples_do_not_mention_tokens_or_old_variables():
    for path in _example_files():
        text = path.read_text(encoding="utf-8")
        for needle in ("<token>", local_main.ENV_TOKEN, local_main.ENV_SERVICE_URL, "跳板机地址>"):
            assert needle not in text, (path, needle)


def _envs_example() -> dict:
    with open(ENVS_EXAMPLE, "rb") as f:
        return tomllib.load(f)


def test_envs_example_has_only_placeholders():
    envs = _envs_example()["envs"]
    assert list(envs) == ["a-prod", "a-dev", "b-dev"]
    for name, item in envs.items():
        assert set(item) == {"description", "service_url", "token"}, name
        assert TOKEN_PLACEHOLDER.fullmatch(item["token"]), name
        assert HOST_PLACEHOLDER.search(item["service_url"]), name


def _fill_placeholders(text: str) -> str:
    counter = iter(range(1, 100))
    text = HOST_PLACEHOLDER.sub(lambda _: f"10.0.0.{next(counter)}", text)
    return TOKEN_PLACEHOLDER.sub(lambda _: f"tk-example-{next(counter)}", text)


def test_examples_are_accepted_after_filling_placeholders(tmp_path):
    envs_file = tmp_path / "home" / local_main.DEFAULT_ENVS_FILE
    envs_file.parent.mkdir(parents=True)
    envs_file.write_text(_fill_placeholders(ENVS_EXAMPLE.read_text(encoding="utf-8")), encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()

    # 没有项目文件：可用全部登记的环境。
    settings = local_main.load_settings({}, cwd=project, home=tmp_path / "home")
    assert settings.env_names == ("a-prod", "a-dev", "b-dev")
    assert settings.envs_file == envs_file and settings.project_file is None
    assert settings.envs[0].mcp_url == "http://10.0.0.1:8999/mcp"
    assert settings.result_dir == project / "query_log"

    # 项目文件模板限定为 A 项目的两套环境。
    (project / local_main.PROJECT_FILE).write_bytes(PROJECT_EXAMPLE.read_bytes())
    settings = local_main.load_settings({}, cwd=project, home=tmp_path / "home")
    assert settings.env_names == ("a-prod", "a-dev")
    assert settings.registered_env_names == ("a-prod", "a-dev", "b-dev")
    assert PROJECT_EXAMPLE.name == local_main.PROJECT_FILE


def test_codex_tool_timeout_covers_local_mcp_deadline():
    # Local_MCP 等待结果与下载共用 Service_Call_Timeout，之后的 DELETE 另限 DELETE_TIMEOUT_S。
    assert _codex()["tool_timeout_sec"] >= local_main.DEFAULT_CALL_TIMEOUT_S + local_main.DELETE_TIMEOUT_S


def test_codex_startup_timeout_is_at_least_20s():
    assert _codex()["startup_timeout_sec"] >= 20


# ---------------------------------------------------------------------------
# SKILL.md
# ---------------------------------------------------------------------------


def _skill_parts() -> tuple[dict[str, str], str]:
    text = SKILL_FILE.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    head, sep, body = text[len("---\n") :].partition("\n---\n")
    assert sep, "YAML 头没有结束的 ---"
    fields: dict[str, str] = {}
    for line in head.splitlines():
        key, colon, value = line.partition(": ")
        assert colon and re.fullmatch(r"[a-z_]+", key), f"YAML 头中无法解析的行：{line!r}"
        assert key not in fields
        # 简单的 plain scalar：不含会被 YAML 解释为映射、注释或其他结构的写法。
        assert value == value.strip() and value
        assert ": " not in value and " #" not in value
        assert value[0] not in "-?:,[]{}#&*!|>'\"%@`"
        fields[key] = value
    return fields, body


def test_skill_front_matter_has_name_and_description():
    fields, _ = _skill_parts()
    assert set(fields) == {"name", "description"}
    assert fields["name"] == SKILL_DIR.name == "log-viewer"
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", fields["name"]) and len(fields["name"]) <= 64
    description = fields["description"]
    assert len(description) <= 1024
    for phrase in ("SFS2X 日志", "秒级 Unix 时间戳", "GMT-2", "server_timezone", "线上", "内网", SEARCH_LOGS,
                   LIST_SERVERS):
        assert phrase in description


def test_timestamp_example_is_correct():
    server_time = datetime(2026, 10, 6, 21, 13, 5, tzinfo=timezone(timedelta(hours=-2)))
    assert int(server_time.timestamp()) == 1791328385
    assert server_time.astimezone(timezone.utc).isoformat() == "2026-10-06T23:13:05+00:00"


def test_skill_body_is_consistent_with_tool_description():
    _, body = _skill_parts()
    # 工具说明中的换算命令原样出现在 SKILL.md 中。
    commands = re.findall(r'python -c "[^"]*"', SEARCH_LOGS_DESCRIPTION)
    assert commands
    for command in commands:
        assert command in body
    for phrase in (
        "秒级 Unix 时间戳",
        "服务器时间 GMT-2（UTC−2）",
        "2026-10-06 21:13:05",
        "2026-10-06T23:13:05Z",
        "1791328385",
        "yyyy-MM-dd HH:mm:ss,SSS | 级别 | 线程 | logger | 消息",
        "2026-10-06 21:13:05,123 | INFO  | <线程> | CMD | <SERVER_ID> | CMD | <请求名> | <uid> | …",
        "context_after",
        "终点多取 1 分钟",
        "模式只作为 grep 的匹配内容",
        "分流出去的日志不会出现在 normal 里",
        "list_servers",
        "server_timezone",
        "log_types",
    ):
        assert phrase in SEARCH_LOGS_DESCRIPTION, phrase
        assert phrase in body, phrase
    # 环境：SKILL.md 说明 env 的选法，且不再提示检查已不用的 LOG_MCP_SERVICE_URL。
    assert "## 环境：先选 env" in body
    assert "先问用户" in body and "query_log/<env>/" in body
    assert local_main.ENV_SERVICE_URL not in body
    # log_type 与目录：工具说明中的每一项在 SKILL.md 的表格中有对应的一行。
    log_types = re.findall(r"^- ([a-z]+)：([a-z]+) 目录", SEARCH_LOGS_DESCRIPTION, re.M)
    assert [t for t, _ in log_types] == ["normal", "login", "analysis", "pulsar"]
    for log_type, directory in log_types:
        assert re.search(rf"^\| `{log_type}` \| {directory} \|", body, re.M), log_type


def test_skill_example_timestamps_match_their_server_time():
    _, body = _skill_parts()
    gmt_minus_2 = timezone(timedelta(hours=-2))
    assert int(datetime(2026, 9, 17, 16, 0, tzinfo=gmt_minus_2).timestamp()) == 1789668000
    assert int(datetime(2026, 9, 17, 17, 0, tzinfo=gmt_minus_2).timestamp()) == 1789671600
    assert '"start_time": 1789668000, "end_time": 1789671600' in body


# ---------------------------------------------------------------------------
# README
# ---------------------------------------------------------------------------


def test_readme_links_point_to_existing_files():
    text = README.read_text(encoding="utf-8")
    links = re.findall(r"\]\(([^)#\s]+)\)", text)
    assert links
    for link in links:
        if "://" in link:
            continue
        assert (ROOT / link).exists(), link


def test_readme_lists_skill_install_locations():
    text = README.read_text(encoding="utf-8")
    for location in (
        ".kiro/skills/log-viewer/SKILL.md",
        "~/.kiro/skills/log-viewer/SKILL.md",
        ".claude/skills/log-viewer/SKILL.md",
        "~/.claude/skills/log-viewer/SKILL.md",
        ".agents/skills/log-viewer/SKILL.md",
        "~/.agents/skills/log-viewer/SKILL.md",
    ):
        assert location in text, location
