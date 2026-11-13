"""Local_MCP 本地结果清理的单元测试（任务 10.5，需求 4.8）。

``cleanup_expired_results`` 只删除 Local_Result_Dir 中名字符合 Result_Handle 格式、lstat 为真实目录且修改时间
早于 Local_Result_TTL 的子项；启动时（main）与每次 search_logs 开始时（call_tool）执行。
"""

from __future__ import annotations

import logging
import os
import pathlib
import sys
import time

import httpx
import pytest

from log_viewer_mcp.local import main as local_main
from log_viewer_mcp.local.main import LocalMCP, cleanup_expired_results
from tests.unit.test_local import SERVICE_URL, payload, settings

TTL_H = 24.0
HANDLE_OLD = "oldOldOldOldOldOldOld_"
HANDLE_OLD2 = "old2-old2-old2-old2-ab"
HANDLE_NEW = "newNewNewNewNewNewNew_"


def make_dir(path: pathlib.Path, age_h: float, now: float, files: dict[str, bytes] | None = None) -> pathlib.Path:
    """创建目录（含文件），再把目录的修改时间设为 now 之前 age_h 小时。"""
    path.mkdir(parents=True)
    for name, data in (files or {}).items():
        (path / name).write_bytes(data)
    stamp = now - age_h * 3600
    os.utime(path, (stamp, stamp))
    return path


def test_removes_only_expired_handle_dirs(tmp_path) -> None:
    now = time.time()
    root = tmp_path / "query_log"
    make_dir(root / HANDLE_OLD, TTL_H + 1, now, {"1001_normal.log": b"x\n"})
    make_dir(root / HANDLE_OLD2, TTL_H + 0.01, now)
    make_dir(root / HANDLE_NEW, TTL_H - 0.01, now, {"1001_normal.log": b"y\n"})

    removed = cleanup_expired_results(root, TTL_H, now=now)

    assert removed == sorted([HANDLE_OLD, HANDLE_OLD2])
    assert sorted(p.name for p in root.iterdir()) == [HANDLE_NEW]
    assert (root / HANDLE_NEW / "1001_normal.log").read_bytes() == b"y\n"


def test_leaves_non_handle_names_and_regular_files(tmp_path) -> None:
    now = time.time()
    root = tmp_path / "query_log"
    names = ["short", "a" * 21, "a" * 23, "abcdefghijklmnopqrstu+", "abcdefghijklmnopqrstu中", "notes"]
    for name in names:
        make_dir(root / name, TTL_H * 10, now, {"f.log": b"keep"})
    file_with_handle_name = root / HANDLE_OLD
    file_with_handle_name.write_bytes(b"regular file")
    stamp = now - TTL_H * 10 * 3600
    os.utime(file_with_handle_name, (stamp, stamp))

    assert cleanup_expired_results(root, TTL_H, now=now) == []

    assert sorted(p.name for p in root.iterdir()) == sorted([*names, HANDLE_OLD])
    assert file_with_handle_name.read_bytes() == b"regular file"
    for name in names:
        assert (root / name / "f.log").read_bytes() == b"keep"


def test_missing_result_dir_is_not_created(tmp_path) -> None:
    root = tmp_path / "query_log"
    assert cleanup_expired_results(root, TTL_H) == []
    assert not root.exists()


def _link_dir(link: pathlib.Path, target: pathlib.Path) -> str:
    """建目录符号链接；不可用时在 Windows 上改建 junction；都不可用时跳过测试。返回所建链接的种类。"""
    try:
        os.symlink(target, link, target_is_directory=True)
        return "symlink"
    except (OSError, NotImplementedError):
        pass
    if sys.platform == "win32":
        import _winapi

        try:
            _winapi.CreateJunction(str(target), str(link))
            return "junction"
        except OSError:
            pass
    pytest.skip("无法创建目录符号链接或 junction")


def test_symlinked_handle_dir_is_not_followed(tmp_path) -> None:
    root = tmp_path / "query_log"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.log").write_bytes(b"keep")
    _link_dir(root / HANDLE_OLD, outside)

    # now 远在未来：链接与目标按修改时间都算过期，仍不应被跟随或删除。
    assert cleanup_expired_results(root, TTL_H, now=time.time() + 1000 * 3600) == []

    assert os.path.lexists(root / HANDLE_OLD)
    assert (outside / "precious.log").read_bytes() == b"keep"


def test_links_inside_expired_dir_are_removed_without_following(tmp_path) -> None:
    now = time.time()
    root = tmp_path / "query_log"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.log").write_bytes(b"keep")
    handle_dir = make_dir(root / HANDLE_OLD, 0, now, {"1001_normal.log": b"x"})
    _link_dir(handle_dir / "link", outside)

    assert cleanup_expired_results(root, TTL_H, now=now + (TTL_H + 1) * 3600) == [HANDLE_OLD]

    assert not os.path.lexists(handle_dir)
    assert (outside / "precious.log").read_bytes() == b"keep"


def test_unlistable_result_dir_is_logged_not_raised(tmp_path, caplog) -> None:
    root = tmp_path / "query_log"
    root.write_bytes(b"not a directory")
    with caplog.at_level(logging.WARNING, logger=local_main.__name__):
        assert cleanup_expired_results(root, TTL_H) == []
    assert "无法列出结果目录" in caplog.text
    assert root.read_bytes() == b"not a directory"


# ---------------------------------------------------------------------------
# 触发时机：每次 search_logs 开始时、启动时
# ---------------------------------------------------------------------------


def _refuse(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


def _expired_dirs(tmp_path) -> tuple[pathlib.Path, pathlib.Path]:
    now = time.time()
    root = tmp_path / "query_log"
    old = make_dir(root / HANDLE_OLD, TTL_H + 1, now, {"1001_normal.log": b"x"})
    new = make_dir(root / HANDLE_NEW, 1, now)
    return old, new


@pytest.mark.asyncio
async def test_search_logs_cleans_up_at_query_start_even_if_service_unavailable(tmp_path) -> None:
    old, new = _expired_dirs(tmp_path)
    local = LocalMCP(settings(tmp_path), transport=httpx.MockTransport(_refuse))

    result = await local.call_tool("search_logs", {"svr_ids": [1001], "keywords": ["x"]})

    assert result.isError is True
    assert payload(result)["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert not old.exists()
    assert new.is_dir()


@pytest.mark.asyncio
async def test_list_servers_does_not_trigger_cleanup(tmp_path) -> None:
    old, new = _expired_dirs(tmp_path)
    local = LocalMCP(settings(tmp_path), transport=httpx.MockTransport(_refuse))

    await local.call_tool("list_servers", {})

    assert old.is_dir() and new.is_dir()


def test_main_cleans_up_at_startup(tmp_path, monkeypatch) -> None:
    old, new = _expired_dirs(tmp_path)
    runs: list[str] = []
    monkeypatch.setattr(LocalMCP, "run", lambda self, transport: runs.append(transport))
    root_logger = logging.getLogger()
    saved = (root_logger.handlers[:], root_logger.level,
             logging.getLogger("httpx").level, logging.getLogger("httpcore").level)
    try:
        code = local_main.main({"LOG_MCP_SERVICE_URL": SERVICE_URL, "LOG_MCP_TOKEN": "startup-token-1",
                                "LOG_MCP_RESULT_DIR": str(tmp_path / "query_log")}, home=tmp_path)
    finally:
        root_logger.handlers[:] = saved[0]
        root_logger.setLevel(saved[1])
        logging.getLogger("httpx").setLevel(saved[2])
        logging.getLogger("httpcore").setLevel(saved[3])

    assert code == 0 and runs == ["stdio"]
    assert not old.exists()
    assert new.is_dir()
