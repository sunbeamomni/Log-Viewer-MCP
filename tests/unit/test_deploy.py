"""deploy/ 下部署产物的静态检查（任务 12.2、14.8、14.9；需求 10.2、10.6、10.7）。

- Dockerfile 与 Dockerfile.dockerignore：基础镜像、安装 ".[server]"、exec 形式的 ENTRYPOINT 与 CMD、不设 USER、构建上下文的允许清单。
- compose.yaml：不引入 YAML 依赖，按本文件的已知结构逐行读取关键项（服务名、build、init、restart、
  network_mode: host 且没有 ports、两个挂载、stop_grace_period、json-file 日志限制）。
- 路径一致：config.py 的默认路径都在 Config_Dir 下，Config_Dir 与审计目录和 compose 的挂载点一致。
- config.example.toml：按 config.py 的启动检查读取，Config_Dir（/etc/log-viewer-mcp）换到临时目录，其中放入假的 ssh_key 与 tokens.json。
"""

import dataclasses
import json
import re
import tomllib
from pathlib import Path

import pytest

from log_viewer_mcp.server import app
from log_viewer_mcp.server import config as config_module
from log_viewer_mcp.server.config import AuditConfig, Limits, ResultsConfig, load_config

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
EXAMPLE_CONFIG = DEPLOY / "config.example.toml"
DOCKERFILE = DEPLOY / "Dockerfile"
DOCKERIGNORE = DEPLOY / "Dockerfile.dockerignore"
COMPOSE = DEPLOY / "compose.yaml"

# 在 fixture 改写 config.py 的默认路径之前取值
CONFIG_DIR = config_module.CONFIG_DIR
CONFIG_PREFIX = CONFIG_DIR + "/"
SHA = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"

# 注释掉的可选项：「# 键 = 值」或「# [表]」
COMMENTED_ITEM = re.compile(r"^# (?=[a-z_]+ = |\[[a-z_]+\])", re.MULTILINE)


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    """临时 Config_Dir：放入 ssh_key、tokens.json 与 known_hosts，并把 config.py 的默认路径指向这里。"""
    d = tmp_path / "config"
    d.mkdir()
    (d / "ssh_key").write_bytes(b"-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----\n")
    (d / "known_hosts").write_bytes(b"10.0.1.11 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIE\n")
    (d / "tokens.json").write_text(
        json.dumps(
            [{"token_id": "a1", "user": "zhangsan", "sha256": SHA, "created_at": "2026-09-17T10:00:00-02:00", "revoked": False}]
        ),
        encoding="utf-8",
    )
    prefix = d.as_posix() + "/"
    monkeypatch.setattr(config_module, "DEFAULT_SSH_KEY_FILE", prefix + "ssh_key")
    monkeypatch.setattr(config_module, "DEFAULT_TOKENS_FILE", prefix + "tokens.json")
    return prefix


def _write(tmp_path, text, config_dir):
    out = tmp_path / "config.toml"
    out.write_text(text.replace(CONFIG_PREFIX, config_dir), encoding="utf-8")
    return str(out)


def _example_uncommented() -> dict:
    """示例配置去掉可选项的注释（password_file 除外）后的内容。"""
    text = EXAMPLE_CONFIG.read_text(encoding="utf-8")
    text = "\n".join(line for line in text.splitlines() if not line.startswith("# password_file"))
    return tomllib.loads(COMMENTED_ITEM.sub("", text))


# ---------------------------------------------------------------------------
# Dockerfile 与 Dockerfile.dockerignore
# ---------------------------------------------------------------------------


def _dockerfile_instructions() -> list[tuple[str, str]]:
    """(指令, 参数) 列表：跳过注释与空行，合并以反斜杠续行的行。"""
    out: list[tuple[str, str]] = []
    pending = ""
    for raw in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not pending and (not line or line.startswith("#")):
            continue
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        line = pending + line
        pending = ""
        instr, _, args = line.partition(" ")
        out.append((instr.upper(), args.strip()))
    assert not pending
    return out


def _only(instr: str) -> str:
    args = [a for i, a in _dockerfile_instructions() if i == instr]
    assert len(args) == 1, f"{instr} 应恰好出现一次"
    return args[0]


def test_dockerfile_base_image_and_install():
    instructions = _dockerfile_instructions()
    assert instructions[0] == ("FROM", "python:3.11-slim-bookworm")
    assert [i for i, _ in instructions].count("FROM") == 1
    env = " ".join(a for i, a in instructions if i == "ENV").split()
    assert {"PYTHONUNBUFFERED=1", "PYTHONDONTWRITEBYTECODE=1"} <= set(env)
    copied = {a.split()[0] for i, a in instructions if i == "COPY"}
    assert copied == {"pyproject.toml", "log_viewer_mcp"}
    runs = [a for i, a in instructions if i == "RUN"]
    assert any("pip install" in a and '".[server]"' in a and "--no-cache-dir" in a for a in runs)


def test_dockerfile_entrypoint_and_cmd_exec_form():
    # exec 形式（JSON 数组）：中间没有 Shell，SIGTERM 直接到达服务进程
    entrypoint = json.loads(_only("ENTRYPOINT"))
    cmd = json.loads(_only("CMD"))
    assert entrypoint == ["log-viewer-mcp-server"]
    assert cmd == ["-c", f"{CONFIG_DIR}/config.toml"]
    assert app.parse_args(cmd).config == CONFIG_PREFIX + "config.toml"
    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["scripts"]
    assert scripts["log-viewer-mcp-server"] == "log_viewer_mcp.server.app:main"
    assert "log-viewer-mcp-token" in scripts


def test_dockerfile_runs_as_root():
    assert "USER" not in {i for i, _ in _dockerfile_instructions()}


def test_dockerignore_is_an_allow_list():
    patterns = [
        line.strip()
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    # 先排除一切，再只放回构建需要的文件：tests、.git、.venv、deploy/config、deploy/audit 都不进入构建上下文
    assert patterns[0] == "*"
    assert {p for p in patterns if p.startswith("!")} == {"!pyproject.toml", "!log_viewer_mcp/"}
    # Dockerfile 复制的文件都在放回的范围内
    copied = {a.split()[0] for i, a in _dockerfile_instructions() if i == "COPY"}
    assert {p.lstrip("!").rstrip("/") for p in patterns if p.startswith("!")} == copied


# ---------------------------------------------------------------------------
# compose.yaml（按本文件的已知结构逐行读取，不引入 YAML 依赖）
# ---------------------------------------------------------------------------


def _strip_comment(line: str) -> str:
    """去掉行尾注释（本文件的注释前都有空白，值中不含「 #」）。"""
    if line.lstrip().startswith("#"):
        return ""
    return re.sub(r"\s+#.*$", "", line).rstrip()


def _parse_flow(text: str):
    """解析单行的流式映射，如 `{ driver: json-file, options: { max-size: "10m" } }`；标量一律为字符串。"""
    tokens = re.findall(r'[{}:,]|"[^"]*"|[^\s{}:,"]+', text)

    def value(i):
        if tokens[i] == "{":
            return mapping(i)
        return tokens[i].strip('"'), i + 1

    def mapping(i):
        assert tokens[i] == "{"
        i += 1
        d = {}
        while tokens[i] != "}":
            key = tokens[i]
            assert tokens[i + 1] == ":"
            d[key], i = value(i + 2)
            if tokens[i] == ",":
                i += 1
        return d, i + 1

    result, end = value(0)
    assert end == len(tokens), text
    return result


def _compose_services() -> dict[str, dict]:
    """{服务名: {键: 值}}；值为标量字符串、流式映射（dict）或块列表（list）。"""
    lines = [(len(s) - len(s.lstrip()), s.strip()) for s in map(_strip_comment, COMPOSE.read_text(encoding="utf-8").splitlines()) if s]
    top = [text for indent, text in lines if indent == 0]
    assert top == ["services:"], "compose.yaml 应只有顶层的 services"
    services: dict[str, dict] = {}
    current: dict | None = None
    last_key: str | None = None
    for indent, text in lines[1:]:
        if indent == 2:
            assert text.endswith(":"), text
            current = services.setdefault(text[:-1], {})
        elif indent == 4:
            assert current is not None
            key, sep, value = text.partition(":")
            assert sep, text
            value = value.strip()
            last_key = key
            current[key] = _parse_flow(value) if value.startswith("{") else (value.strip('"') if value else [])
        elif indent == 6 and text.startswith("- "):
            assert current is not None and isinstance(current[last_key], list), text
            current[last_key].append(text[2:].strip().strip('"'))
        else:
            raise AssertionError(f"compose.yaml 中有未预期的结构：{text}")
    return services


def _service() -> dict:
    services = _compose_services()
    assert list(services) == ["log-viewer-mcp"]
    return services["log-viewer-mcp"]


def _volumes() -> dict[str, str]:
    """{容器内路径: 宿主机路径}；挂载都可写（Token_CLI 要写 tokens.json）。"""
    out = {}
    for item in _service()["volumes"]:
        parts = item.split(":")
        assert len(parts) == 2, f"挂载不应带选项（如 :ro）：{item}"
        host, container = parts
        out[container] = host
    return out


def _seconds(duration: str) -> int:
    m = re.fullmatch(r"(?:(\d+)m)?(?:(\d+)s)?", duration)
    assert m and duration, duration
    return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)


def test_compose_prefers_prebuilt_image():
    # 默认拉取 CI 发布的镜像（名称须为小写，与 .github/workflows/docker-image.yml 一致），可用环境变量替换
    s = _service()
    assert s["image"] == "${LOG_VIEWER_MCP_IMAGE:-ghcr.io/sunbeamomni/log-viewer-mcp:latest}"
    # 不写 pull_policy：同时有 image 与 build 时 compose 先拉取，拉取不到才构建
    assert "pull_policy" not in s


def test_release_workflow_builds_the_compose_image():
    text = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "branches: [main]" in text
    assert "platforms: linux/amd64,linux/arm64" in text
    assert "file: deploy/Dockerfile" in text
    # 工作流推送 ghcr.io/<仓库所有者小写>/log-viewer-mcp，compose 默认拉取同名镜像
    assert 'echo "name=ghcr.io/${GITHUB_REPOSITORY_OWNER,,}/log-viewer-mcp"' in text
    assert "type=raw,value=latest" in text


def test_compose_service_build_and_restart():
    s = _service()
    assert s["build"] == {"context": "..", "dockerfile": "deploy/Dockerfile"}
    assert (DEPLOY / s["build"]["context"]).resolve() / s["build"]["dockerfile"] == DOCKERFILE
    assert s["init"] == "true"
    assert s["restart"] == "unless-stopped"


def test_compose_uses_host_network_without_ports():
    s = _service()
    assert s["network_mode"] == "host"
    assert "ports" not in s
    assert not re.search(r"^\s*ports\s*:", COMPOSE.read_text(encoding="utf-8"), re.MULTILINE)


def test_compose_volumes_match_config_paths():
    assert _volumes() == {
        CONFIG_DIR: "./config",
        AuditConfig().dir: "./audit",
    }
    # 审计目录：config.py 的默认值、示例配置（注释中的默认值）与 compose 的挂载点相同
    assert AuditConfig().dir == config_module.DEFAULT_AUDIT_DIR
    assert _example_uncommented()["audit"]["dir"] == AuditConfig().dir
    # Config_Dir：默认路径都在挂载点之下
    for path in (config_module.DEFAULT_TOKENS_FILE, config_module.DEFAULT_SSH_KEY_FILE):
        assert path.startswith(CONFIG_PREFIX), path
    # 示例配置中注释掉的凭据路径也都在 Config_Dir 中
    data = _example_uncommented()
    for path in (data["tokens_file"], data["ssh"]["key_file"], data["ssh"]["known_hosts"]):
        assert path.startswith(CONFIG_PREFIX), path
    assert "\n# password_file = \"" + CONFIG_PREFIX in EXAMPLE_CONFIG.read_text(encoding="utf-8")


def test_compose_stop_grace_period_covers_shutdown():
    assert _seconds(_service()["stop_grace_period"]) > app.SHUTDOWN_GRACE_S + app.GRACEFUL_SHUTDOWN_TIMEOUT_S


def test_compose_logging_is_limited():
    logging = _service()["logging"]
    assert logging["driver"] == "json-file"
    assert re.fullmatch(r"\d+[kmg]", logging["options"]["max-size"])
    assert int(logging["options"]["max-file"]) >= 1


# ---------------------------------------------------------------------------
# 仓库中的其他部署相关文件
# ---------------------------------------------------------------------------


def test_gitignore_excludes_config_and_audit():
    lines = {line.strip() for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()}
    assert {"deploy/config/", "deploy/audit/"} <= lines


def test_systemd_unit_removed():
    assert not (DEPLOY / "log-viewer-mcp.service").exists()


# ---------------------------------------------------------------------------
# config.example.toml
# ---------------------------------------------------------------------------


def test_example_config_passes_startup_checks(tmp_path, config_dir):
    c = load_config(_write(tmp_path, EXAMPLE_CONFIG.read_text(encoding="utf-8"), config_dir))
    assert (c.listen.address, c.listen.port) == ("0.0.0.0", 8999)
    assert (c.admin_db.host, c.admin_db.user, c.admin_db.database) == ("10.0.0.2", "logview", "admin")
    assert c.admin_db.password_file is None
    assert c.zip_dir == "/usr/local/ds/SFS2X/ziplogs"
    assert {name: (t.dir, t.suffix) for name, t in c.log_types.items()} == {
        "normal": ("/usr/local/ds/SFS2X/logs", ""),
        "login": ("/usr/local/ds/SFS2X/logpushinit", ".logpushinit"),
        "analysis": ("/usr/local/ds/SFS2X/loganalys", ".loganalys"),
        "pulsar": ("/usr/local/ds/SFS2X/polsarlog", ".polsarlog"),
    }
    assert c.ssh.key_file == config_dir + "ssh_key"
    assert c.ssh.known_hosts is None
    assert c.audit == AuditConfig()
    assert [t.user for t in c.active_tokens] == ["zhangsan"]


def test_example_commented_items_are_the_defaults(tmp_path, config_dir):
    """去掉可选项的注释（password_file 与 password 二选一，除外）后仍能启动，且取值与省略时相同。"""
    text = EXAMPLE_CONFIG.read_text(encoding="utf-8")
    text = "\n".join(line for line in text.splitlines() if not line.startswith("# password_file"))
    uncommented = COMMENTED_ITEM.sub("", text)
    assert uncommented != text
    c = load_config(_write(tmp_path, uncommented, config_dir))
    assert c.server_timezone == "-02:00"
    assert c.timezone_label == "GMT-2"
    assert c.tokens_file == config_dir + "tokens.json"
    assert c.admin_db.port == 3306
    assert (c.ssh.user, c.ssh.port, c.ssh.key_file, c.ssh.connect_timeout_s) == ("root", 22, config_dir + "ssh_key", 10)
    assert c.ssh.known_hosts == config_dir + "known_hosts"
    assert c.limits == Limits()
    assert c.results == ResultsConfig()
    assert c.audit == AuditConfig()
    # 每个可选项都写出了
    data = tomllib.loads(uncommented)
    assert set(data["limits"]) == {f.name for f in dataclasses.fields(Limits)}
    assert set(data["results"]) == {f.name for f in dataclasses.fields(ResultsConfig)}
    assert set(data["audit"]) == {f.name for f in dataclasses.fields(AuditConfig)}
    assert set(data["ssh"]) == {"user", "port", "key_file", "known_hosts", "connect_timeout_s"}
    assert "\n# password_file = " in EXAMPLE_CONFIG.read_text(encoding="utf-8")
