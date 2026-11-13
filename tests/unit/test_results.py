"""server/results.py 的单元测试（任务 7.1、7.2 的基本测试与任务 7.3 的补充）。

覆盖：截回与截断（含已写出到磁盘、需 ftruncate 的情形）、空结果为空文件（1.5、2.7、9.3）；
RESULT_STORE_FAILED 的各出错点（5.4）；Result_Store 的启动检查与清空（5.3）；
/results/ 的 404、HEAD 与大文件下载（5.2）；过期清理（5.3）。
"""

from __future__ import annotations

import asyncio
import errno
import os
import stat
import sys

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from log_viewer_mcp.common.errors import ErrorCode, LogViewerError
from log_viewer_mcp.common.formats import is_valid_result_handle
from log_viewer_mcp.server import results
from log_viewer_mcp.server.config import ConfigError, ResultsConfig
from log_viewer_mcp.server.remote import FileStatus
from log_viewer_mcp.server.results import ResultStore, ResultStoreError, result_routes

OK = FileStatus("F", 0, (0,))
NO_MATCH = FileStatus("Z", 0, (1, 1))
FAILED = FileStatus("F", 0, (2,))
MISSING = FileStatus("N", None, ())

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="需要 POSIX 权限位")


@pytest.fixture(params=[0, 4, results.DEFAULT_BUFFER_SIZE], ids=["unbuffered", "tiny", "default"])
def store(request, tmp_path) -> ResultStore:
    return ResultStore(str(tmp_path / "store"), buffer_size=request.param)


@pytest.fixture(autouse=True)
def _store_dir(tmp_path):
    os.mkdir(tmp_path / "store", 0o700)


def read(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def test_create_handle_makes_private_directory(store):
    handle = store.create_handle()
    assert is_valid_result_handle(handle) and len(handle) == 22
    st = os.lstat(store.handle_dir(handle))
    assert stat.S_ISDIR(st.st_mode)
    if sys.platform != "win32":
        assert stat.S_IMODE(st.st_mode) == 0o700
    assert store.create_handle() != handle


def test_empty_result_is_empty_file(store):
    handle = store.create_handle()
    writer = store.open_writer(handle, 1001, "normal")
    assert os.path.isfile(writer.path)
    writer.begin_file(0)
    writer.end_file(0, NO_MATCH)
    info = writer.close()
    assert (info.file_name, info.bytes, info.lines) == ("1001_normal.log", 0, 0)
    assert read(writer.path) == b""
    assert writer.path == store.result_path(handle, "1001_normal.log")


@posix_only
def test_result_file_mode_is_0600(store):
    writer = store.open_writer(store.create_handle(), 7, "login")
    assert stat.S_IMODE(os.stat(writer.path).st_mode) == 0o600
    writer.close()


def test_failed_file_is_rolled_back_and_counts_are_final(store):
    writer = store.open_writer(store.create_handle(), 1, "normal")
    writer.begin_file(0)
    writer.write(0, b"a1\na")
    writer.write(0, b"2\n")
    writer.end_file(0, OK)
    writer.begin_file(1)
    writer.write(1, b"partial output\nmore")
    writer.write(1, b" of failed file\n")
    writer.end_file(1, FAILED)
    writer.begin_file(2)
    writer.end_file(2, MISSING)
    writer.begin_file(3)
    writer.write(3, b"c1\n--\nc2\n")
    writer.end_file(3, OK)
    info = writer.close()
    assert read(writer.path) == b"a1\na2\nc1\n--\nc2\n"
    assert (info.bytes, info.lines) == (len(b"a1\na2\nc1\n--\nc2\n"), 5)


def test_interrupt_keeps_complete_lines_only(store):
    writer = store.open_writer(store.create_handle(), 1, "normal")
    writer.begin_file(0)
    writer.write(0, b"x1\n")
    writer.end_file(0, OK)
    writer.begin_file(1)
    writer.write(1, b"y1\ny2\ny3-par")
    writer.write(1, b"tial")
    writer.interrupt_file(1)
    info = writer.close()
    assert read(writer.path) == b"x1\ny1\ny2\n"
    assert (info.bytes, info.lines) == (9, 3)


def test_interrupt_without_complete_line_rolls_back_to_file_start(store):
    writer = store.open_writer(store.create_handle(), 1, "normal")
    writer.begin_file(0)
    writer.write(0, b"x1\n")
    writer.end_file(0, OK)
    writer.begin_file(1)
    writer.write(1, b"no newline yet")
    writer.interrupt_file(1)
    assert writer.close().bytes == 3
    assert read(writer.path) == b"x1\n"


def test_existing_file_is_not_reused_and_handle_is_discarded(store):
    handle = store.create_handle()
    store.open_writer(handle, 1, "normal").close()
    with pytest.raises(LogViewerError) as exc:
        store.open_writer(handle, 1, "normal")
    assert exc.value.code is ErrorCode.RESULT_STORE_FAILED
    assert not os.path.exists(store.handle_dir(handle))


def test_write_error_discards_handle_directory(store, monkeypatch):
    handle = store.create_handle()
    other = store.open_writer(handle, 2, "normal")
    other.close()
    writer = store.open_writer(handle, 1, "normal")
    writer.begin_file(0)

    def enospc(fd, data):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(results.os, "write", enospc)
    with pytest.raises(ResultStoreError) as exc:
        writer.write(0, b"x" * (store.buffer_size + 1))
        writer.close()
    monkeypatch.undo()
    assert exc.value.code is ErrorCode.RESULT_STORE_FAILED
    assert exc.value.message == "写入结果文件失败"
    assert not os.path.exists(store.handle_dir(handle))
    with pytest.raises(ResultStoreError):
        writer.write(0, b"y\n")


def test_out_of_order_calls_are_internal_errors(store):
    writer = store.open_writer(store.create_handle(), 1, "normal")
    with pytest.raises(LogViewerError) as exc:
        writer.write(0, b"x\n")
    assert exc.value.code is ErrorCode.INTERNAL_ERROR
    writer.begin_file(0)
    with pytest.raises(LogViewerError):
        writer.end_file(1, OK)
    writer.close()


def test_abort_removes_file(store):
    handle = store.create_handle()
    writer = store.open_writer(handle, 1, "normal")
    writer.begin_file(0)
    writer.write(0, b"x\n")
    writer.abort()
    assert not os.path.exists(writer.path)
    assert os.path.isdir(store.handle_dir(handle))


def test_invalid_names_are_rejected(store):
    with pytest.raises(ValueError):
        store.handle_dir("../" + "a" * 19)
    handle = store.create_handle()
    with pytest.raises(ValueError):
        store.open_writer(handle, 0, "normal")
    with pytest.raises(ValueError):
        store.open_writer(handle, 1, "../x")


# ---------------------------------------------------------------------------
# Result_Store 管理与 /results/ 处理函数（任务 7.2 的基本测试）
# ---------------------------------------------------------------------------


class Clock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def try_symlink(src, dst, *, target_is_directory=False) -> None:
    try:
        os.symlink(src, dst, target_is_directory=target_is_directory)
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"无法创建符号链接：{e}")


def dir_link(target, link) -> None:
    """建立指向目录的链接：优先符号链接；Windows 上没有权限时改用 junction（同为重解析点）。"""
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError) as e:
        if sys.platform != "win32":
            pytest.skip(f"无法创建符号链接：{e}")
    import _winapi

    _winapi.CreateJunction(os.path.abspath(target), os.path.abspath(link))


def write_result(store: ResultStore, handle: str, svr_id: int, data: bytes) -> str:
    writer = store.open_writer(handle, svr_id, "normal")
    writer.begin_file(0)
    writer.write(0, data)
    writer.end_file(0, OK)
    return writer.close().file_name


def test_prepare_creates_missing_store(tmp_path):
    directory = tmp_path / "a" / "query_log"
    ResultStore(str(directory)).prepare()
    st = os.lstat(directory)
    assert stat.S_ISDIR(st.st_mode)
    if sys.platform != "win32":
        assert stat.S_IMODE(st.st_mode) == 0o700


def test_prepare_clears_old_results_without_following_links(tmp_path):
    directory = tmp_path / "store"
    old = directory / ("A" * 22)
    old.mkdir()
    (old / "1_normal.log").write_bytes(b"old\n")
    (directory / "stray").write_bytes(b"x")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_bytes(b"keep")
    dir_link(outside, directory / "link")
    ResultStore(str(directory)).prepare()
    assert os.listdir(directory) == []
    assert (outside / "keep.txt").read_bytes() == b"keep"


def test_prepare_rejects_symlink_and_file(tmp_path):
    not_dir = tmp_path / "file"
    not_dir.write_bytes(b"x")
    with pytest.raises(ConfigError):
        ResultStore(str(not_dir)).prepare()
    link = tmp_path / "link"
    # 目标本身是合格的目录：只信任 lstat，链接仍被拒绝。
    (tmp_path / "store" / "keep").write_bytes(b"keep")
    dir_link(tmp_path / "store", link)
    with pytest.raises(ConfigError):
        ResultStore(str(link)).prepare()
    assert (tmp_path / "store" / "keep").read_bytes() == b"keep"


def test_prepare_accepts_existing_0755_store_and_clears_it(tmp_path):
    # 不检查属主与权限位（5.1）：已有的 0755 目录照常启动，旧结果照常清空（5.3）。
    directory = tmp_path / "store"
    old = directory / ("A" * 22)
    old.mkdir()
    (old / "1_normal.log").write_bytes(b"old\n")
    os.chmod(directory, 0o755)
    store = ResultStore(str(directory))
    store.prepare()
    assert os.listdir(directory) == []
    handle = store.create_handle()
    writer = store.open_writer(handle, 1, "normal")
    writer.close()
    if sys.platform != "win32":
        assert stat.S_IMODE(os.lstat(store.handle_dir(handle)).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(writer.path).st_mode) == 0o600


def test_from_config_uses_ttl_and_interval(tmp_path):
    store = ResultStore.from_config(ResultsConfig(dir=str(tmp_path), ttl_min=2, cleanup_interval_min=3))
    assert (store.directory, store.ttl_s, store.cleanup_interval_s) == (str(tmp_path), 120, 180)


def test_cleanup_removes_only_expired_handles(tmp_path):
    clock = Clock()
    store = ResultStore(str(tmp_path / "store"), ttl_s=60, clock=clock)
    old = store.create_handle()
    clock.t += 30
    new = store.create_handle()
    assert store.expires_at(old) == clock.t + 30
    clock.t += 29
    assert store.cleanup() == []
    clock.t += 1
    assert store.cleanup() == [old]
    assert not os.path.exists(store.handle_dir(old))
    assert os.path.isdir(store.handle_dir(new))
    assert store.expires_at(old) is None


@pytest.mark.asyncio
async def test_run_cleanup_deletes_expired_and_stops_on_cancel(tmp_path):
    clock = Clock()
    store = ResultStore(str(tmp_path / "store"), ttl_s=1, clock=clock)
    handle = store.create_handle()
    clock.t += 1
    task = asyncio.create_task(store.run_cleanup(0.01))
    for _ in range(200):
        if not os.path.exists(store.handle_dir(handle)):
            break
        await asyncio.sleep(0.01)
    assert not os.path.exists(store.handle_dir(handle))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.fixture
def http(tmp_path):
    store = ResultStore(str(tmp_path / "store"))
    with TestClient(Starlette(routes=result_routes(store))) as client:
        yield store, client


def test_download_streams_file(http):
    store, client = http
    handle = store.create_handle()
    data = b"line 1\n\xe4\xb8\xad\xe6\x96\x87\n" + b"x" * (results.READ_CHUNK_SIZE + 10) + b"\n"
    name = write_result(store, handle, 1001, data)
    resp = client.get(f"/results/{handle}/{name}")
    assert resp.status_code == 200
    assert resp.content == data
    assert resp.headers["content-length"] == str(len(data))
    assert resp.headers["content-type"] == "application/octet-stream"
    head = client.head(f"/results/{handle}/{name}")
    assert head.status_code == 200 and head.content == b""
    assert head.headers["content-length"] == str(len(data))


def test_download_empty_file(http):
    store, client = http
    handle = store.create_handle()
    store.open_writer(handle, 7, "login").close()
    resp = client.get(f"/results/{handle}/7_login.log")
    assert (resp.status_code, resp.content, resp.headers["content-length"]) == (200, b"", "0")


def test_invalid_or_missing_paths_are_404(http):
    store, client = http
    handle = store.create_handle()
    write_result(store, handle, 1, b"x\n")
    for path in [
        f"/results/{handle}/2_normal.log",  # 文件不存在
        f"/results/{'B' * 22}/1_normal.log",  # handle 不存在
        f"/results/{handle[:-1]}/1_normal.log",  # handle 格式不符
        f"/results/{handle}/01_normal.log.bak",  # 文件名格式不符
        f"/results/{handle}/1_Normal.log",
        f"/results/{handle}/..%2F1_normal.log",
    ]:
        assert client.get(path).status_code == 404, path
    assert client.get(f"/results/{handle}").status_code == 405  # 该路由只接受 DELETE
    assert client.delete(f"/results/{'B' * 22}").status_code == 404
    assert client.delete("/results/not-a-handle").status_code == 404
    assert client.post(f"/results/{handle}/1_normal.log").status_code == 405


def test_delete_removes_handle_directory(http):
    store, client = http
    handle = store.create_handle()
    name = write_result(store, handle, 1, b"x\n")
    resp = client.delete(f"/results/{handle}")
    assert resp.status_code == 204 and resp.content == b""
    assert not os.path.exists(store.handle_dir(handle))
    assert store.expires_at(handle) is None
    assert client.get(f"/results/{handle}/{name}").status_code == 404
    assert client.delete(f"/results/{handle}").status_code == 404


def test_symlinked_result_file_is_404(http, tmp_path):
    store, client = http
    if not getattr(os, "O_NOFOLLOW", 0):
        pytest.skip("系统不支持 O_NOFOLLOW")
    secret = tmp_path / "secret"
    secret.write_bytes(b"secret\n")
    handle = store.create_handle()
    try_symlink(secret, os.path.join(store.handle_dir(handle), "1_normal.log"))
    assert client.get(f"/results/{handle}/1_normal.log").status_code == 404


# ---------------------------------------------------------------------------
# 任务 7.3：截回与截断（2.7、9.3）、空结果（1.5）
# ---------------------------------------------------------------------------


def counts(writer) -> tuple[int, int]:
    return writer.bytes, writer.lines


@pytest.mark.parametrize("buffer_size", [0, 3, 1 << 20], ids=["unbuffered", "tiny", "large"])
def test_rollback_and_interrupt_counts_after_each_step(tmp_path, buffer_size):
    # buffer_size 为 0 或很小时，失败的文件已写出到磁盘，截回走 ftruncate；很大时只裁剪缓冲。
    store = ResultStore(str(tmp_path / "store"), buffer_size=buffer_size)
    writer = store.open_writer(store.create_handle(), 1001, "normal")
    writer.begin_file(0)
    writer.write(0, b"a1\n")
    writer.write(0, b"a2\n--\n")
    writer.end_file(0, OK)
    assert counts(writer) == (9, 3)

    writer.begin_file(1)
    writer.write(1, b"b1\nb2\nb")
    assert counts(writer) == (16, 5)
    writer.write(1, b"3\n")
    writer.end_file(1, FAILED)  # 截回到 begin_file 时的偏移
    assert counts(writer) == (9, 3)

    writer.begin_file(2)
    writer.end_file(2, NO_MATCH)
    assert counts(writer) == (9, 3)

    writer.begin_file(3)
    writer.write(3, b"c1\nc")
    writer.write(3, b"2\nc3-par")
    assert counts(writer) == (21, 5)
    writer.interrupt_file(3)  # 截到最后一个完整行
    assert counts(writer) == (15, 5)

    info = writer.close()
    assert read(writer.path) == b"a1\na2\n--\nc1\nc2\n"
    assert (info.file_name, info.bytes, info.lines) == ("1001_normal.log", 15, 5)


@pytest.mark.parametrize("buffer_size", [0, 2, 1 << 20], ids=["unbuffered", "tiny", "large"])
def test_rollback_restores_last_line_end_for_later_interrupt(tmp_path, buffer_size):
    # 失败文件中的 LF 不能影响之后的超时截断：截回后"最后一个完整行"回到截回前。
    store = ResultStore(str(tmp_path / "store"), buffer_size=buffer_size)
    writer = store.open_writer(store.create_handle(), 1, "normal")
    writer.begin_file(0)
    writer.write(0, b"a\n")
    writer.end_file(0, OK)
    writer.begin_file(1)
    writer.write(1, b"bbbbbb\ncccccc\n")
    writer.end_file(1, FAILED)
    writer.begin_file(2)
    writer.write(2, b"xy")
    writer.interrupt_file(2)
    info = writer.close()
    assert read(writer.path) == b"a\n"
    assert (info.bytes, info.lines) == (2, 1)


@pytest.mark.parametrize("buffer_size", [0, 1 << 20], ids=["unbuffered", "large"])
def test_rollback_of_first_file_leaves_empty_file_and_writing_continues(tmp_path, buffer_size):
    store = ResultStore(str(tmp_path / "store"), buffer_size=buffer_size)
    writer = store.open_writer(store.create_handle(), 1, "normal")
    writer.begin_file(0)
    writer.write(0, b"bad\n" * 1000)
    writer.end_file(0, FAILED)
    assert counts(writer) == (0, 0)
    writer.begin_file(1)
    writer.write(1, b"ok\n")
    writer.end_file(1, OK)
    info = writer.close()
    assert read(writer.path) == b"ok\n"
    assert (info.bytes, info.lines) == (3, 1)


def test_close_during_file_keeps_complete_lines_only(store):
    writer = store.open_writer(store.create_handle(), 1, "normal")
    writer.begin_file(0)
    writer.write(0, b"x1\nx2-partial")
    info = writer.close()
    assert read(writer.path) == b"x1\n"
    assert (info.bytes, info.lines) == (3, 1)
    assert writer.close() is info  # 可重复调用


def test_empty_results_are_empty_files(store):
    handle = store.create_handle()
    # 没有选中任何文件
    none_selected = store.open_writer(handle, 1, "normal")
    assert none_selected.close() == results.ResultFileInfo("1_normal.log", 0, 0)
    # 选中的文件都没有匹配、不存在或失败
    no_lines = store.open_writer(handle, 2, "normal")
    for index, status in enumerate([NO_MATCH, MISSING, FAILED]):
        no_lines.begin_file(index)
        if status is FAILED:
            no_lines.write(index, b"partial\n")
        no_lines.end_file(index, status)
    assert no_lines.close() == results.ResultFileInfo("2_normal.log", 0, 0)
    for name in ("1_normal.log", "2_normal.log"):
        path = store.result_path(handle, name)
        assert os.path.isfile(path) and read(path) == b""


# ---------------------------------------------------------------------------
# 任务 7.3：RESULT_STORE_FAILED（5.4）
# ---------------------------------------------------------------------------

RAW_ERROR = "No space left on device (raw os text)"


def failing_for_fd(name: str, fd: int, *, close_first: bool = False):
    """os.<name> 的替身：只对 fd 抛出一次 ENOSPC，其余调用照常。

    只抛一次：fd 关闭后其号码会被重用（例如 shutil.rmtree 打开的目录描述符），不能再拦截。
    """
    real = getattr(os, name)
    fired = False

    def fake(*args, **kwargs):
        nonlocal fired
        if not fired and args and args[0] == fd:
            fired = True
            if close_first:
                real(*args, **kwargs)
            raise OSError(errno.ENOSPC, RAW_ERROR)
        return real(*args, **kwargs)

    return fake


def assert_store_failed(exc_info, caplog) -> None:
    err = exc_info.value
    assert err.code is ErrorCode.RESULT_STORE_FAILED
    assert err.to_dict() == {"code": "RESULT_STORE_FAILED", "message": "写入结果文件失败"}
    assert RAW_ERROR not in str(err) and RAW_ERROR not in repr(err)
    assert err.__cause__ is None and err.__suppress_context__
    # 原始错误只写运行日志
    assert RAW_ERROR in caplog.text


def assert_writer_dead(writer) -> None:
    for call in (
        lambda: writer.begin_file(9),
        lambda: writer.write(9, b"x\n"),
        lambda: writer.end_file(9, OK),
        lambda: writer.interrupt_file(9),
        writer.close,
    ):
        with pytest.raises(ResultStoreError):
            call()


def started_writer(store: ResultStore, handle: str):
    writer = store.open_writer(handle, 1, "normal")
    writer.begin_file(0)
    writer.write(0, b"a\n")
    writer.end_file(0, OK)
    return writer


@pytest.mark.parametrize("where", ["write", "flush_on_close", "ftruncate", "close"])
def test_store_failures_discard_handle_and_hide_raw_error(tmp_path, monkeypatch, caplog, where):
    buffer_size = 1 << 20 if where == "flush_on_close" else 0
    store = ResultStore(str(tmp_path / "store"), buffer_size=buffer_size)
    handle = store.create_handle()
    write_result(store, handle, 2, b"other server\n")  # 同一 handle 下已写完的其他文件也一并删除
    writer = started_writer(store, handle)
    fd = writer._fd
    with monkeypatch.context() as m, pytest.raises(ResultStoreError) as exc:
        if where == "write":
            m.setattr(os, "write", failing_for_fd("write", fd))
            writer.begin_file(1)
            writer.write(1, b"b\n")
        elif where == "flush_on_close":
            m.setattr(os, "write", failing_for_fd("write", fd))
            writer.close()
        elif where == "ftruncate":
            m.setattr(os, "ftruncate", failing_for_fd("ftruncate", fd))
            writer.begin_file(1)
            writer.write(1, b"b\n")
            writer.end_file(1, FAILED)
        else:
            m.setattr(os, "close", failing_for_fd("close", fd, close_first=True))
            writer.close()
    assert_store_failed(exc, caplog)
    assert not os.path.exists(store.handle_dir(handle))
    assert store.expires_at(handle) is None
    assert writer._fd is None  # 文件描述符已关闭
    assert_writer_dead(writer)


def test_open_failure_discards_handle(tmp_path, monkeypatch, caplog):
    store = ResultStore(str(tmp_path / "store"))
    handle = store.create_handle()
    write_result(store, handle, 2, b"other\n")
    target = store.result_path(handle, "1_normal.log")
    real_open = os.open

    def fake_open(path, *args, **kwargs):
        if os.fspath(path) == target:
            raise OSError(errno.ENOSPC, RAW_ERROR)
        return real_open(path, *args, **kwargs)

    with monkeypatch.context() as m, pytest.raises(ResultStoreError) as exc:
        m.setattr(os, "open", fake_open)
        store.open_writer(handle, 1, "normal")
    assert_store_failed(exc, caplog)
    assert not os.path.exists(store.handle_dir(handle))
    assert store.expires_at(handle) is None


def test_open_writer_after_handle_removed_fails(tmp_path):
    store = ResultStore(str(tmp_path / "store"))
    handle = store.create_handle()
    os.rmdir(store.handle_dir(handle))
    with pytest.raises(ResultStoreError):
        store.open_writer(handle, 1, "normal")
    assert store.expires_at(handle) is None


@posix_only
def test_open_writer_does_not_follow_symlink(tmp_path):
    store = ResultStore(str(tmp_path / "store"))
    handle = store.create_handle()
    victim = tmp_path / "victim"
    victim.write_bytes(b"keep\n")
    try_symlink(victim, store.result_path(handle, "1_normal.log"))
    with pytest.raises(ResultStoreError):
        store.open_writer(handle, 1, "normal")
    assert victim.read_bytes() == b"keep\n"
    assert not os.path.lexists(store.handle_dir(handle))


def test_create_handle_failures(tmp_path, monkeypatch, caplog):
    missing = ResultStore(str(tmp_path / "missing"))
    with pytest.raises(ResultStoreError):
        missing.create_handle()
    assert missing.pop_expired(float("inf")) == []

    store = ResultStore(str(tmp_path / "store"))
    real_chmod = os.chmod

    def fake_chmod(path, *args, **kwargs):
        if os.path.dirname(os.fspath(path)) == store.directory:
            raise OSError(errno.EPERM, RAW_ERROR)
        return real_chmod(path, *args, **kwargs)

    with monkeypatch.context() as m, pytest.raises(ResultStoreError) as exc:
        m.setattr(os, "chmod", fake_chmod)
        store.create_handle()
    assert RAW_ERROR not in str(exc.value)
    assert os.listdir(store.directory) == []
    assert store.pop_expired(float("inf")) == []


def test_create_handle_retries_on_collision(tmp_path, monkeypatch):
    store = ResultStore(str(tmp_path / "store"))
    taken = "A" * 22
    os.mkdir(store.handle_dir(taken))
    fresh = "B" * 22
    names = iter([taken, fresh])
    monkeypatch.setattr(results, "new_result_handle", lambda: next(names))
    assert store.create_handle() == fresh
    monkeypatch.setattr(results, "new_result_handle", lambda: taken)
    with pytest.raises(ResultStoreError):
        store.create_handle()
    assert os.path.isdir(store.handle_dir(taken))  # 已存在的目录不被删除


# ---------------------------------------------------------------------------
# 任务 7.3：Result_Store 启动检查与清空（5.3）
# ---------------------------------------------------------------------------


def test_prepare_rejects_store_under_a_file(tmp_path):
    (tmp_path / "file").write_bytes(b"x")
    with pytest.raises(ConfigError):
        ResultStore(str(tmp_path / "file" / "a" / "query_log")).prepare()


def test_prepare_forgets_old_handles_and_clears_store(tmp_path):
    store = ResultStore(str(tmp_path / "store"))
    handle = store.create_handle()
    write_result(store, handle, 1, b"x\n")
    store.prepare()
    assert os.listdir(store.directory) == []
    assert store.expires_at(handle) is None
    assert store.pop_expired(float("inf")) == []


def test_prepare_fails_when_old_results_cannot_be_removed(tmp_path, monkeypatch):
    directory = tmp_path / "store"
    (directory / "stray").write_bytes(b"x")

    def refuse(path):
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(results, "_remove_entry", refuse)
    with pytest.raises(ConfigError):
        ResultStore(str(directory)).prepare()


@posix_only
def test_prepare_and_create_handle_ignore_umask(tmp_path):
    old = os.umask(0o277)
    try:
        store = ResultStore(str(tmp_path / "query_log"))
        store.prepare()
        handle = store.create_handle()
    finally:
        os.umask(old)
    assert stat.S_IMODE(os.lstat(store.directory).st_mode) == 0o700
    assert stat.S_IMODE(os.lstat(store.handle_dir(handle)).st_mode) == 0o700


# ---------------------------------------------------------------------------
# 任务 7.3：/results/ 的 404、HEAD 与下载（5.2）
# ---------------------------------------------------------------------------


def client_for(store: ResultStore) -> TestClient:
    return TestClient(Starlette(routes=result_routes(store)))


def test_head_on_empty_and_missing_files(http):
    store, client = http
    handle = store.create_handle()
    store.open_writer(handle, 1, "normal").close()
    head = client.head(f"/results/{handle}/1_normal.log")
    assert (head.status_code, head.content, head.headers["content-length"]) == (200, b"", "0")
    assert client.head(f"/results/{handle}/2_normal.log").status_code == 404
    assert client.head(f"/results/{'B' * 22}/1_normal.log").status_code == 404


@pytest.mark.parametrize("size", [2 * results.READ_CHUNK_SIZE, 3 * results.READ_CHUNK_SIZE + 123])
def test_download_large_file_bytes_and_headers(http, size):
    store, client = http
    handle = store.create_handle()
    data = (os.urandom(size // 2) + b"\r\n\x00\n" * (size // 8 + 1))[:size]
    assert len(data) == size
    name = write_result(store, handle, 1, data)
    resp = client.get(f"/results/{handle}/{name}")
    assert resp.status_code == 200
    assert resp.headers["content-length"] == str(size)
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert len(resp.content) == size and resp.content == data


def test_not_found_cases(http, tmp_path):
    store, client = http
    handle = store.create_handle()
    write_result(store, handle, 1, b"x\n")
    hdir = store.handle_dir(handle)
    os.mkdir(os.path.join(hdir, "2_normal.log"))  # 目录代替文件
    file_handle = "F" * 22
    (tmp_path / "store" / file_handle).write_bytes(b"x")  # 文件代替 handle 目录
    for path in [
        f"/results/{handle}/2_normal.log",
        f"/results/{file_handle}/1_normal.log",
        f"/results/{handle}/1_normal.log/x",
        f"/results/{handle}/%2E%2E",
        f"/results/{handle}/1_normal.LOG",
        f"/results/{handle}%2F..%2F/1_normal.log",
        "/results/",
    ]:
        assert client.get(path).status_code == 404, path
    assert client.delete(f"/results/{file_handle}").status_code == 404
    assert (tmp_path / "store" / file_handle).read_bytes() == b"x"  # 不删除非目录项


def test_linked_handle_directory_is_404_and_target_kept(http, tmp_path):
    store, client = http
    real = store.create_handle()
    write_result(store, real, 1, b"x\n")
    linked = "L" * 22
    dir_link(store.handle_dir(real), store.handle_dir(linked))
    assert client.get(f"/results/{linked}/1_normal.log").status_code == 404
    assert client.delete(f"/results/{linked}").status_code == 404
    assert read(store.result_path(real, "1_normal.log")) == b"x\n"


@posix_only
def test_fifo_in_place_of_result_file_is_404(http):
    store, client = http
    handle = store.create_handle()
    os.mkfifo(store.result_path(handle, "1_normal.log"), 0o600)
    assert client.get(f"/results/{handle}/1_normal.log").status_code == 404


def test_expired_results_are_404(tmp_path):
    clock = Clock()
    store = ResultStore(str(tmp_path / "store"), ttl_s=60, clock=clock)
    handle = store.create_handle()
    name = write_result(store, handle, 1, b"x\n")
    with client_for(store) as client:
        assert client.get(f"/results/{handle}/{name}").status_code == 200
        clock.t += 60
        assert store.cleanup() == [handle]
        assert client.get(f"/results/{handle}/{name}").status_code == 404
        assert client.delete(f"/results/{handle}").status_code == 404


def test_delete_failure_is_500_without_raw_error(http, monkeypatch, caplog):
    store, client = http
    handle = store.create_handle()

    def refuse(h):
        raise PermissionError(errno.EACCES, RAW_ERROR)

    monkeypatch.setattr(store, "remove_handle_dir", refuse)
    resp = client.delete(f"/results/{handle}")
    assert resp.status_code == 500
    assert RAW_ERROR not in resp.text
    assert RAW_ERROR in caplog.text


class Sent:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def __call__(self, message) -> None:
        self.messages.append(message)


async def no_receive():  # pragma: no cover - 响应不读取请求体
    return {"type": "http.disconnect"}


def fd_is_closed(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError:
        return True
    return False


@pytest.mark.asyncio
async def test_file_response_closes_fd_and_sends_once(tmp_path):
    path = tmp_path / "f"
    path.write_bytes(b"abc\n")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    response = results._ResultFileResponse(fd, 4)
    sent = Sent()
    await response({"type": "http", "method": "GET"}, no_receive, sent)
    assert sent.messages[0]["status"] == 200
    assert b"".join(m.get("body", b"") for m in sent.messages[1:]) == b"abc\n"
    assert sent.messages[-1].get("more_body", False) is False
    assert fd_is_closed(fd)
    with pytest.raises(RuntimeError):
        await response({"type": "http", "method": "GET"}, no_receive, Sent())


@pytest.mark.asyncio
async def test_file_response_shrunk_file_does_not_finish_body(tmp_path):
    path = tmp_path / "f"
    path.write_bytes(b"abc\n")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    response = results._ResultFileResponse(fd, 10)  # 比实际长：发送中途读到文件末尾
    sent = Sent()
    with pytest.raises(RuntimeError):
        await response({"type": "http", "method": "GET"}, no_receive, sent)
    assert all(m.get("more_body", False) for m in sent.messages[1:])  # 没有发送结束帧
    assert fd_is_closed(fd)


# ---------------------------------------------------------------------------
# 任务 7.3：过期清理（5.3）
# ---------------------------------------------------------------------------


def test_ttl_boundary_uses_creation_time(tmp_path):
    clock = Clock()
    store = ResultStore(str(tmp_path / "store"), ttl_s=600, clock=clock)
    handle = store.create_handle()
    assert store.expires_at(handle) == clock.t + 600
    assert store.cleanup(clock.t + 599.999) == []
    assert os.path.isdir(store.handle_dir(handle))
    assert store.cleanup(clock.t + 600) == [handle]
    assert not os.path.exists(store.handle_dir(handle))
    assert store.cleanup(clock.t + 10_000) == []


def test_delete_between_pop_and_remove_is_harmless(http, caplog):
    store, client = http
    handle = store.create_handle()
    write_result(store, handle, 1, b"x\n")
    expired = store.pop_expired(float("inf"))
    assert expired == [handle]
    assert client.delete(f"/results/{handle}").status_code == 204
    store._remove_dirs(expired)  # 后台线程中的删除：目录已不在，不报错
    assert "删除过期结果目录失败" not in caplog.text


def test_delete_then_cleanup_and_cleanup_then_delete(tmp_path):
    clock = Clock()
    store = ResultStore(str(tmp_path / "store"), ttl_s=1, clock=clock)
    first = store.create_handle()
    second = store.create_handle()
    clock.t += 1
    with client_for(store) as client:
        assert client.delete(f"/results/{first}").status_code == 204
        assert store.cleanup() == [second]  # first 已由 DELETE 忘记
        assert client.delete(f"/results/{second}").status_code == 404
    assert os.listdir(store.directory) == []


def test_rmtree_ignores_entries_removed_concurrently():
    # shutil.rmtree 的错误回调：并发删除造成的 FileNotFoundError 忽略，其他错误照常抛出。
    results._ignore_missing(os.unlink, "x", FileNotFoundError(errno.ENOENT, "gone"))
    results._ignore_missing(os.unlink, "x", (FileNotFoundError, FileNotFoundError(), None))
    with pytest.raises(PermissionError):
        results._ignore_missing(os.unlink, "x", PermissionError(errno.EACCES, "denied"))


async def wait_until(predicate, timeout: float = 5.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


@pytest.mark.asyncio
async def test_run_cleanup_waits_for_interval_and_keeps_unexpired(tmp_path):
    clock = Clock()
    store = ResultStore(str(tmp_path / "store"), ttl_s=60, cleanup_interval_s=0.3, clock=clock)
    old = store.create_handle()
    clock.t += 30
    young = store.create_handle()
    clock.t += 30  # old 恰好到期，young 还剩 30 秒
    task = asyncio.create_task(store.run_cleanup())  # 默认间隔取 cleanup_interval_s
    try:
        await asyncio.sleep(0.05)
        assert os.path.isdir(store.handle_dir(old))  # 第一次清理在等待一个间隔之后
        assert await wait_until(lambda: not os.path.exists(store.handle_dir(old)))
        assert os.path.isdir(store.handle_dir(young))
        assert store.expires_at(young) == clock.t + 30
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_run_cleanup_survives_errors(tmp_path, monkeypatch, caplog):
    clock = Clock()
    store = ResultStore(str(tmp_path / "store"), ttl_s=1, clock=clock)
    handle = store.create_handle()
    clock.t += 1
    real_pop = store.pop_expired
    calls = []

    def flaky(now=None):
        calls.append(now)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return real_pop(now)

    monkeypatch.setattr(store, "pop_expired", flaky)
    task = asyncio.create_task(store.run_cleanup(0.01))
    try:
        assert await wait_until(lambda: not os.path.exists(store.handle_dir(handle)))
        assert len(calls) >= 2
        assert "删除过期结果失败" in caplog.text
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
