"""包结构、入口点与锁定依赖的冒烟测试（任务 1.1、14.1）。"""

import importlib
import importlib.metadata

import pytest

# design.md「Architecture」的入口表
ENTRY_POINTS = {
    "log-viewer-mcp-local": "log_viewer_mcp.local.main:main",
    "log-viewer-mcp-server": "log_viewer_mcp.server.app:main",
    "log-viewer-mcp-token": "log_viewer_mcp.token_cli.main:main",
}


@pytest.mark.parametrize(
    "module",
    [
        "log_viewer_mcp.common",
        "log_viewer_mcp.server",
        "log_viewer_mcp.server.app",
        "log_viewer_mcp.local",
        "log_viewer_mcp.local.main",
        "log_viewer_mcp.token_cli",
        "log_viewer_mcp.token_cli.main",
    ],
)
def test_subpackages_import(module):
    importlib.import_module(module)


@pytest.mark.parametrize("package", ["server", "local", "token_cli"])
def test_main_modules_exist(package):
    assert importlib.util.find_spec(f"log_viewer_mcp.{package}.__main__") is not None


def test_pinned_mcp_provides_fastmcp_streamable_http_app():
    # design.md「服务入口（app.py）」依赖的 FastMCP 构造参数与 streamable_http_app()
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("log-viewer", stateless_http=True, json_response=True, host="0.0.0.0")
    app = mcp.streamable_http_app()
    assert any(getattr(r, "path", None) == "/mcp" for r in app.routes)


def test_pinned_server_and_local_dependencies_import():
    for name in ["asyncssh", "mysql.connector", "uvicorn", "httpx", "mcp.client.streamable_http"]:
        importlib.import_module(name)


def test_console_scripts_registered_in_package_metadata():
    # 开发环境改动 pyproject 后须重新 pip install -e ".[server,test]"，元数据才会更新
    scripts = importlib.metadata.distribution("log-viewer-mcp").entry_points.select(group="console_scripts")
    assert {ep.name: ep.value for ep in scripts} == ENTRY_POINTS


@pytest.mark.parametrize("name", sorted(ENTRY_POINTS))
def test_console_script_targets_are_callable(name):
    (ep,) = importlib.metadata.distribution("log-viewer-mcp").entry_points.select(
        group="console_scripts", name=name
    )
    assert callable(ep.load())
