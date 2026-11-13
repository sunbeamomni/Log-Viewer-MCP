"""Result_Store 与结果文件（需求 1.5、2.7、5.1–5.4、9.3；design.md「结果文件（results.py）」）。

结果写入（任务 7.1）：

- ``ResultStore.create_handle()`` 生成 Result_Handle（``secrets.token_urlsafe(16)``，22 个字符、128 位，
  按 formats.py 的正则 fullmatch 校验），并以 0700 新建 ``<Result_Store>/<handle>/``。
- ``ResultStore.open_writer(handle, svr_id, log_type)`` 以 O_CREAT | O_EXCL | O_NOFOLLOW、0600 新建
  ``<handle>/<svr_id>_<log_type>.log`` 并返回 ``ResultFileWriter``。文件在打开时即创建，
  没有匹配行时就是空文件（1.5）。
- ``ResultFileWriter`` 是 remote.py 中 ``ResultSink`` 的文件实现：

  - ``begin_file``：记下当前偏移与行数；
  - ``write``：追加原始 grep 输出（不一定在行边界上切分），统计字节数与 LF 个数；
  - ``end_file``：``status.ok`` 为假时截回到 begin_file 时的偏移（2.7）；
  - ``interrupt_file``：超时时截到最后一个完整行（9.3）；
  - ``close()``：写出缓冲、关闭文件，返回最终的文件名、字节数与行数（截回、截断之后的值）。

- 任何文件操作出错（例如 ENOSPC）时：关闭文件、删除本次整个 handle 目录，抛出
  ``LogViewerError(RESULT_STORE_FAILED)``（5.4）。原始错误只写运行日志。同一 handle 下其他
  svr_id 的写入器不会自动得知，调用方收到 RESULT_STORE_FAILED 后应把整次调用作为失败处理（整次错误）。

写入是同步文件 I/O，由 run_query 在事件循环中直接调用。结果写在跳板机本地磁盘，且写入器在内存中
缓冲（默认 256 KiB 才写一次），每次系统调用耗时很短，不放进线程，以免打乱 ResultSink 的调用顺序。

Result_Store 管理（任务 7.2，5.3）：

- ``ResultStore.prepare()``：启动检查。Result_Store 不存在则以 0700 创建，再用 lstat 确认它是真实目录
  （不是符号链接；Windows 上也不是 junction 等重解析点），否则抛出 ``ConfigError``（拒绝启动）。
  不检查属主与权限位（5.1）：默认的 /tmp/query_log 在容器自己的 /tmp 中，没有其他用户；
  新建的 handle 目录与结果文件仍为 0700、0600。
  检查通过后清空其中的旧结果：只删除 Result_Store 内的条目本身，目录用 rmtree（不跟随其中的符号链接），
  文件与符号链接只 unlink，不跟随；删除失败同样拒绝启动。
- 内存中记录每个 handle 的过期时刻（创建时刻 + Result_TTL）。``cleanup()`` 删除已过期的 handle 目录；
  ``run_cleanup()`` 是后台任务，每 Cleanup_Interval（默认 5 分钟）执行一次，被取消时结束。
  过期记录只在内存中：启动时已清空 Result_Store，之后其中的 handle 都由本进程创建。

/results/ 的处理函数（任务 7.2，5.2；路由由 8.6 经 ``mount_result_routes`` 用 FastMCP.custom_route 挂载）：

- ``GET /results/<handle>/<文件名>``：流式返回结果文件，``Content-Type: application/octet-stream``
  （日志原始字节，不保证是 UTF-8），带 Content-Length。文件以 O_NOFOLLOW 打开，再对同一个文件描述符
  fstat 确认是普通文件。Starlette 对 GET 路由同时接受 HEAD，此时只返回响应头。
- ``DELETE /results/<handle>``：删除整个 handle 目录，返回 204。
- handle 与文件名先按 formats.py 的正则 fullmatch；不符，或 Result_Store 中没有该 handle 目录或文件
  （包括已过期被删除、已被 DELETE 的）时返回 404。是否存在以磁盘为准：启动时已清空，
  其中的 handle 都由本进程创建。其他方法由 Starlette 路由返回 405，不匹配的路径返回 404。
- 认证由 8.6 的 ASGI 中间件在路由之前完成，处理函数假定请求已认证。
  （FastMCP.custom_route 注册的路由不经过 SDK 自带的认证，所以必须由中间件包住整个应用。）
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import secrets
import shutil
import stat
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import anyio.to_thread
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from log_viewer_mcp.common.errors import ErrorCode, LogViewerError
from log_viewer_mcp.common.formats import (
    RESULT_HANDLE_LENGTH,
    is_valid_result_file_name,
    is_valid_result_handle,
    result_file_name,
)
from log_viewer_mcp.server.config import ConfigError, ResultsConfig

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from log_viewer_mcp.server.remote import FileStatus, QueryFile

__all__ = [
    "DEFAULT_BUFFER_SIZE",
    "DEFAULT_CLEANUP_INTERVAL_S",
    "DEFAULT_TTL_S",
    "RESULT_FILE_ROUTE",
    "RESULT_HANDLE_ROUTE",
    "ResultFileInfo",
    "ResultFileWriter",
    "ResultHandlers",
    "ResultRouteSpec",
    "ResultStore",
    "ResultStoreError",
    "mount_result_routes",
    "new_result_handle",
    "result_route_specs",
    "result_routes",
]

logger = logging.getLogger(__name__)

DIR_MODE = 0o700
FILE_MODE = 0o600
DEFAULT_BUFFER_SIZE = 256 * 1024
DEFAULT_TTL_S = ResultsConfig.ttl_min * 60
DEFAULT_CLEANUP_INTERVAL_S = ResultsConfig.cleanup_interval_min * 60

# 下载时每次读取的字节数。
READ_CHUNK_SIZE = 256 * 1024

RESULT_FILE_ROUTE = "/results/{handle}/{file_name}"
RESULT_HANDLE_ROUTE = "/results/{handle}"

# Windows 的重解析点（符号链接、junction）；POSIX 上 stat 结果没有 st_file_attributes。
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400

# 新建 handle 时的重试次数：128 位随机数实际上不会碰撞，重试只是防御。
_CREATE_HANDLE_ATTEMPTS = 5

_OPEN_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_BINARY", 0)
)

# 下载时打开结果文件：不跟随符号链接；O_NONBLOCK 防止指向 FIFO 时阻塞。
_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_BINARY", 0)
)


class ResultStoreError(LogViewerError):
    """写结果文件失败（RESULT_STORE_FAILED，5.4）；抛出时本次 handle 目录已被删除。"""

    def __init__(self) -> None:
        super().__init__(ErrorCode.RESULT_STORE_FAILED)


def new_result_handle() -> str:
    """生成 Result_Handle：secrets.token_urlsafe(16)，22 个字符、128 位（5.1）。"""
    handle = secrets.token_urlsafe(16)
    if len(handle) != RESULT_HANDLE_LENGTH or not is_valid_result_handle(handle):
        raise RuntimeError("生成的 Result_Handle 格式不符")
    return handle


@dataclass(frozen=True)
class ResultFileInfo:
    """一个结果文件的最终信息：文件名（摘要的 file）、字节数与行数（LF 个数）。"""

    file_name: str
    bytes: int
    lines: int


class ResultStore:
    """``<Result_Store>/<handle>/<svr_id>_<log_type>.log`` 的目录布局、写入、启动检查与过期删除。

    ttl_s 为 Result_TTL（秒），cleanup_interval_s 为后台清理的间隔（秒）；clock 返回 Unix 时间，测试可注入。
    """

    def __init__(
        self,
        directory: str,
        *,
        ttl_s: float = DEFAULT_TTL_S,
        cleanup_interval_s: float = DEFAULT_CLEANUP_INTERVAL_S,
        buffer_size: int = DEFAULT_BUFFER_SIZE,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if type(buffer_size) is not int or buffer_size < 0:
            raise ValueError("buffer_size 必须是非负整数")
        if not ttl_s > 0:
            raise ValueError("ttl_s 必须为正数")
        if not cleanup_interval_s > 0:
            raise ValueError("cleanup_interval_s 必须为正数")
        self.directory = directory
        self.ttl_s = ttl_s
        self.cleanup_interval_s = cleanup_interval_s
        self.buffer_size = buffer_size
        self._clock = clock
        # handle → 过期时刻（Unix 时间，创建时刻 + Result_TTL）。只在事件循环中读写。
        self._expires: dict[str, float] = {}

    @classmethod
    def from_config(cls, config: ResultsConfig, **kwargs) -> ResultStore:
        kwargs.setdefault("ttl_s", config.ttl_min * 60)
        kwargs.setdefault("cleanup_interval_s", config.cleanup_interval_min * 60)
        return cls(config.dir, **kwargs)

    # -- 启动检查 -----------------------------------------------------------

    def prepare(self) -> None:
        """启动时：创建（如不存在）并检查 Result_Store，然后清空旧结果；不满足时抛出 ConfigError（5.3）。

        只要求是真实目录（不是符号链接），不检查属主与权限位（5.1）。
        """
        path = self.directory
        created = False
        try:
            os.mkdir(path, DIR_MODE)
            created = True
        except FileExistsError:
            pass
        except FileNotFoundError:
            # 上级目录不存在：上级按默认权限创建，Result_Store 本身为 0700。
            try:
                os.makedirs(path, DIR_MODE)
                created = True
            except OSError as e:
                raise ConfigError(f"无法创建 Result_Store {path}：{e.strerror}") from None
        except OSError as e:
            raise ConfigError(f"无法创建 Result_Store {path}：{e.strerror}") from None
        if created:
            # mkdir 的权限受 umask 影响；经不跟随符号链接的目录描述符再设一次。
            _fchmod_dir_nofollow(path, DIR_MODE)

        try:
            st = os.lstat(path)
        except OSError as e:
            raise ConfigError(f"Result_Store {path} 不可用：{e.strerror}") from None
        if not stat.S_ISDIR(st.st_mode) or _is_reparse_point(st):
            raise ConfigError(f"Result_Store {path} 不是真实目录（或是符号链接），拒绝启动")

        self._expires.clear()
        self._clear_store()

    def _clear_store(self) -> None:
        try:
            with os.scandir(self.directory) as it:
                names = [entry.name for entry in it]
        except OSError as e:
            raise ConfigError(f"无法列举 Result_Store {self.directory}：{e.strerror}") from None
        for name in names:
            path = os.path.join(self.directory, name)
            try:
                _remove_entry(path)
            except OSError as e:
                raise ConfigError(
                    f"无法清空 Result_Store 中的旧结果 {path}：{e.strerror or type(e).__name__}"
                ) from None
        if names:
            logger.info("启动时已清空 Result_Store 中的旧结果 %d 项", len(names))

    # -- 过期删除 -----------------------------------------------------------

    def expires_at(self, handle: str) -> float | None:
        """handle 的过期时刻；不由本进程记录（或已删除）时为 None。"""
        return self._expires.get(handle)

    def pop_expired(self, now: float | None = None) -> list[str]:
        """取出并忘记过期时刻不晚于 now 的 handle（不删除目录）。"""
        if now is None:
            now = self._clock()
        expired = [h for h, t in self._expires.items() if t <= now]
        for handle in expired:
            del self._expires[handle]
        return expired

    def cleanup(self, now: float | None = None) -> list[str]:
        """同步删除已过期的 handle 目录，返回这些 handle。删除失败只写运行日志。"""
        expired = self.pop_expired(now)
        self._remove_dirs(expired)
        return expired

    async def run_cleanup(self, interval_s: float | None = None) -> None:
        """后台任务：每 interval_s 秒（默认 cleanup_interval_s）删除一次过期结果；被取消时结束。

        过期记录在事件循环中取出，目录删除放在线程中。启动时 prepare 已清空，所以先等待再清理。
        """
        interval = self.cleanup_interval_s if interval_s is None else interval_s
        while True:
            await asyncio.sleep(interval)
            try:
                expired = self.pop_expired()
                if expired:
                    await asyncio.to_thread(self._remove_dirs, expired)
            except Exception:
                logger.exception("删除过期结果失败")

    def _remove_dirs(self, handles: list[str]) -> None:
        removed = 0
        for handle in handles:
            try:
                if self.remove_handle_dir(handle):
                    removed += 1
            except OSError as e:
                _log_os_error("删除过期结果目录失败", self.handle_dir(handle), e)
        if removed:
            logger.info("已删除过期结果 %d 个", removed)

    # -- 路径 ---------------------------------------------------------------

    def handle_dir(self, handle: str) -> str:
        """handle 目录的路径；handle 不符合格式时抛出 ValueError（不拼接未校验的值）。"""
        if not is_valid_result_handle(handle):
            raise ValueError("Result_Handle 格式不符")
        return os.path.join(self.directory, handle)

    def result_path(self, handle: str, file_name: str) -> str:
        """结果文件的路径；handle 或文件名不符合格式时抛出 ValueError。"""
        if not is_valid_result_file_name(file_name):
            raise ValueError("结果文件名格式不符")
        return os.path.join(self.handle_dir(handle), file_name)

    # -- 新建与删除 ---------------------------------------------------------

    def create_handle(self) -> str:
        """生成新的 Result_Handle 并以 0700 新建其目录；失败抛出 ResultStoreError。"""
        for _ in range(_CREATE_HANDLE_ATTEMPTS):
            handle = new_result_handle()
            path = self.handle_dir(handle)
            try:
                os.mkdir(path, DIR_MODE)
            except FileExistsError:
                continue
            except OSError as e:
                _log_os_error("新建结果目录失败", path, e)
                raise ResultStoreError() from None
            try:
                # mkdir 的权限受 umask 影响，再显式设一次。
                os.chmod(path, DIR_MODE)
            except OSError as e:
                _log_os_error("设置结果目录权限失败", path, e)
                self.discard(handle)
                raise ResultStoreError() from None
            self._expires[handle] = self._clock() + self.ttl_s
            return handle
        logger.error("新建结果目录失败：连续 %d 次 handle 已存在", _CREATE_HANDLE_ATTEMPTS)
        raise ResultStoreError()

    def open_writer(self, handle: str, svr_id: int, log_type: str) -> ResultFileWriter:
        """新建 ``<handle>/<svr_id>_<log_type>.log``（0600，O_EXCL，不跟随符号链接）。

        文件此时即已存在（没有匹配行时为空文件）。失败时删除整个 handle 目录并抛出 ResultStoreError。
        svr_id 或 log_type 不合法时抛出 ValueError（调用方的编程错误，不删除目录）。
        """
        name = result_file_name(svr_id, log_type)
        path = self.result_path(handle, name)
        try:
            fd = os.open(path, _OPEN_FLAGS, FILE_MODE)
        except OSError as e:
            _log_os_error("新建结果文件失败", path, e)
            self.discard(handle)
            raise ResultStoreError() from None
        return ResultFileWriter(self, handle, name, path, fd, self.buffer_size)

    def discard(self, handle: str) -> None:
        """删除整个 handle 目录（不存在时忽略）。不跟随符号链接；删除失败只写运行日志。"""
        self.forget(handle)
        try:
            self._remove_handle_entry(handle)
        except OSError as e:
            _log_os_error("删除结果目录失败", self.handle_dir(handle), e)

    def forget(self, handle: str) -> None:
        """不再记录 handle 的过期时刻（目录已删除或即将删除）。"""
        self._expires.pop(handle, None)

    def remove_handle_dir(self, handle: str) -> bool:
        """删除 handle 目录；它不存在或不是真实目录时返回 False（不删除）。

        只做文件操作、不改内存记录，可在线程中调用。删除出错时抛出 OSError。
        """
        path = self.handle_dir(handle)
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return False
        if not stat.S_ISDIR(st.st_mode) or _is_reparse_point(st):
            return False
        _rmtree(path)
        return True

    def _remove_handle_entry(self, handle: str) -> None:
        # 写入失败时的清理：handle 位置上不论是什么（不应出现符号链接或文件）都删除该项本身，不跟随。
        path = self.handle_dir(handle)
        try:
            _remove_entry(path)
        except FileNotFoundError:
            pass

    # -- 读取 ---------------------------------------------------------------

    def open_result(self, handle: str, file_name: str) -> tuple[int, int] | None:
        """以只读、O_NOFOLLOW 打开结果文件，返回 (文件描述符, 字节数)；不存在或不是普通文件时返回 None。

        handle 或文件名不符合格式时同样返回 None（不拼接未校验的值）。调用方负责关闭文件描述符。
        """
        if not is_valid_result_handle(handle) or not is_valid_result_file_name(file_name):
            return None
        dir_path = self.handle_dir(handle)
        try:
            dst = os.lstat(dir_path)
        except OSError as e:
            if e.errno not in (errno.ENOENT, errno.ENOTDIR):
                _log_os_error("无法检查结果目录", dir_path, e)
            return None
        if not stat.S_ISDIR(dst.st_mode) or _is_reparse_point(dst):
            return None
        path = os.path.join(dir_path, file_name)
        try:
            fd = os.open(path, _READ_FLAGS)
        except OSError as e:
            # ENOENT：不存在；ELOOP：是符号链接；EACCES/EISDIR 等：不是可读的普通文件。
            if e.errno != errno.ENOENT:
                _log_os_error("无法打开结果文件", path, e)
            return None
        try:
            st = os.fstat(fd)
        except OSError as e:
            os.close(fd)
            _log_os_error("无法检查结果文件", path, e)
            return None
        if not stat.S_ISREG(st.st_mode):
            os.close(fd)
            return None
        return fd, st.st_size


class ResultFileWriter:
    """一个 svr_id 的结果文件；实现 remote.ResultSink（方法同步，按事件顺序调用）。

    记账按"逻辑偏移"进行（已写入文件的字节加上内存缓冲）：

    - ``_size``、``_lines``：当前字节数与 LF 个数；
    - ``_last_lf_end``：最后一个 LF 之后的偏移（没有 LF 时为 0），超时截断用；
    - begin_file 时保存三者的快照，失败截回时恢复。

    截回或截断的目标不早于已写入文件的长度时只裁剪缓冲；否则丢弃缓冲并 ftruncate。
    """

    def __init__(
        self,
        store: ResultStore,
        handle: str,
        file_name: str,
        path: str,
        fd: int,
        buffer_size: int,
    ) -> None:
        self._store = store
        self.handle = handle
        self.file_name = file_name
        self.path = path
        self._fd: int | None = fd
        self._buffer_size = buffer_size
        self._buffer = bytearray()
        self._flushed = 0  # 已写入文件（不含缓冲）的字节数，也是文件描述符的当前位置
        self._size = 0
        self._lines = 0
        self._last_lf_end = 0
        self._current: int | None = None
        self._mark: tuple[int, int, int] = (0, 0, 0)
        self._failed = False
        self._info: ResultFileInfo | None = None

    # -- 统计 ---------------------------------------------------------------

    @property
    def bytes(self) -> int:
        return self._size

    @property
    def lines(self) -> int:
        return self._lines

    # -- ResultSink ---------------------------------------------------------

    def begin_file(self, index: int, file: QueryFile | None = None) -> None:
        self._check_open()
        if self._current is not None:
            raise LogViewerError(ErrorCode.INTERNAL_ERROR)
        self._current = index
        self._mark = (self._size, self._lines, self._last_lf_end)

    def write(self, index: int, data: bytes) -> None:
        self._check_open()
        self._check_current(index)
        if not data:
            return
        n_lf = data.count(b"\n")
        if n_lf:
            self._lines += n_lf
            self._last_lf_end = self._size + data.rfind(b"\n") + 1
        self._size += len(data)
        self._buffer += data
        if len(self._buffer) >= self._buffer_size:
            self._guard(self._flush)

    def end_file(self, index: int, status: FileStatus) -> None:
        self._check_open()
        self._check_current(index)
        self._current = None
        if not status.ok:
            size, lines, last_lf_end = self._mark
            self._guard(self._truncate, size)
            self._lines = lines
            self._last_lf_end = last_lf_end

    def interrupt_file(self, index: int) -> None:
        self._check_open()
        self._check_current(index)
        self._current = None
        begin = self._mark[0]
        # 截到本文件内最后一个完整行；本文件还没有完整行时截回到开始前的偏移。
        # 被截掉的是不含 LF 的残行，行数不变。
        target = max(begin, self._last_lf_end)
        self._guard(self._truncate, target)

    # -- 结束 ---------------------------------------------------------------

    def close(self) -> ResultFileInfo:
        """写出缓冲并关闭文件，返回最终信息；可重复调用。失败抛出 ResultStoreError。"""
        if self._info is not None:
            return self._info
        self._check_open()
        if self._current is not None:
            # 调用方没有结束正在读取的序号就关闭：按超时处理，只保留完整行。
            self.interrupt_file(self._current)
        self._guard(self._flush)
        fd, self._fd = self._fd, None
        assert fd is not None
        try:
            os.close(fd)
        except OSError as e:
            self._fail("关闭结果文件失败", e)
        self._info = ResultFileInfo(self.file_name, self._size, self._lines)
        return self._info

    def abort(self) -> None:
        """丢弃本文件（该 svr_id 失败时，摘要中 file 为 null）：关闭并删除文件，不抛出异常。"""
        self._close_fd()
        self._failed = True
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        except OSError as e:
            _log_os_error("删除结果文件失败", self.path, e)

    # -- 内部 ---------------------------------------------------------------

    def _check_open(self) -> None:
        if self._failed or self._fd is None:
            raise ResultStoreError()

    def _check_current(self, index: int) -> None:
        if self._current != index:
            raise LogViewerError(ErrorCode.INTERNAL_ERROR)

    def _guard(self, fn: Callable[..., None], *args: int) -> None:
        try:
            fn(*args)
        except OSError as e:
            self._fail("写入结果文件失败", e)

    def _flush(self) -> None:
        fd = self._fd
        assert fd is not None
        view = memoryview(self._buffer)
        try:
            while view:
                n = os.write(fd, view)
                if n <= 0:
                    raise OSError("结果文件写入返回 0 字节")
                view = view[n:]
                self._flushed += n
        finally:
            view.release()
        self._buffer.clear()

    def _truncate(self, target: int) -> None:
        if target >= self._flushed:
            del self._buffer[target - self._flushed :]
        else:
            fd = self._fd
            assert fd is not None
            self._buffer.clear()
            os.ftruncate(fd, target)
            os.lseek(fd, target, os.SEEK_SET)
            self._flushed = target
        self._size = target

    def _fail(self, what: str, e: OSError) -> None:
        _log_os_error(what, self.path, e)
        self._failed = True
        self._close_fd()
        self._store.discard(self.handle)
        raise ResultStoreError() from None

    def _close_fd(self) -> None:
        fd, self._fd = self._fd, None
        self._buffer.clear()
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def _log_os_error(what: str, path: str, e: OSError) -> None:
    # 只写错误类别与路径；对外只返回 RESULT_STORE_FAILED 或 HTTP 状态码（10.3）。
    logger.error("%s：%s：%s", what, path, e.strerror or type(e).__name__)


# ---------------------------------------------------------------------------
# 文件系统辅助
# ---------------------------------------------------------------------------


def _is_reparse_point(st: os.stat_result) -> bool:
    """Windows 上的符号链接或 junction（lstat 可能把 junction 报告为目录）；POSIX 上恒为假。"""
    return bool(getattr(st, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


def _ignore_missing(func: Any, path: str, exc: Any) -> None:
    # shutil.rmtree 的错误回调：并发删除造成的"已不存在"忽略，其余错误照常抛出。
    e = exc if isinstance(exc, BaseException) else exc[1]
    if isinstance(e, FileNotFoundError):
        return
    raise e


def _rmtree(path: str) -> None:
    """删除目录树，不跟随其中的符号链接（shutil.rmtree 的行为）；顶层是符号链接时抛出 OSError。"""
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_ignore_missing)
    else:
        shutil.rmtree(path, onerror=_ignore_missing)


def _remove_entry(path: str) -> None:
    """删除一项本身：真实目录整棵删除，文件、符号链接与 junction 只删除该项，不跟随。"""
    st = os.lstat(path)
    if stat.S_ISDIR(st.st_mode) and not _is_reparse_point(st):
        _rmtree(path)
    elif stat.S_ISDIR(st.st_mode):
        os.rmdir(path)  # Windows junction：只删除链接本身
    else:
        os.unlink(path)


def _fchmod_dir_nofollow(path: str, mode: int) -> None:
    """经 O_DIRECTORY | O_NOFOLLOW 打开的描述符设置目录权限；非 POSIX 系统上不做任何事。"""
    if not hasattr(os, "fchmod") or not hasattr(os, "O_DIRECTORY"):
        return
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
        try:
            os.fchmod(fd, mode)
        finally:
            os.close(fd)
    except OSError as e:
        raise ConfigError(f"无法设置 Result_Store {path} 的权限：{e.strerror}") from None


# ---------------------------------------------------------------------------
# /results/ 的处理函数
# ---------------------------------------------------------------------------


class _ResultFileResponse(Response):
    """从已打开的文件描述符流式发送 size 个字节；发送结束、出错或被取消时关闭描述符。

    不用 FileResponse：它会按路径重新打开文件，丢掉 O_NOFOLLOW 与 fstat 的检查。
    """

    media_type = "application/octet-stream"

    def __init__(self, fd: int, size: int) -> None:
        self._fd: int | None = fd
        self._size = size
        super().__init__(
            headers={
                "content-length": str(size),
                "cache-control": "no-store",
                "x-content-type-options": "nosniff",
            }
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            raise RuntimeError("结果文件响应只能发送一次")
        try:
            await send({"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers})
            if scope.get("method") == "HEAD":
                await send({"type": "http.response.body", "body": b""})
                return
            remaining = self._size
            if remaining == 0:
                await send({"type": "http.response.body", "body": b""})
                return
            while remaining > 0:
                # 读取在线程中进行；被取消时 anyio 等线程返回后才抛出，finally 中的 close 不会与 read 并发。
                chunk = await anyio.to_thread.run_sync(os.read, fd, min(READ_CHUNK_SIZE, remaining))
                if not chunk:
                    # 结果文件写完后不再改动，不应出现。不发送结束帧，让服务器中断连接，
                    # 客户端据 Content-Length 判定下载不完整。
                    raise RuntimeError("结果文件在发送过程中变短")
                remaining -= len(chunk)
                await send({"type": "http.response.body", "body": chunk, "more_body": remaining > 0})
        finally:
            os.close(fd)


def _not_found() -> Response:
    return PlainTextResponse("Not Found", status_code=404)


class ResultHandlers:
    """绑定到一个 ResultStore 的 /results/ 处理函数（Starlette endpoint）。请求已由中间件认证。"""

    def __init__(self, store: ResultStore) -> None:
        self.store = store

    async def download(self, request: Request) -> Response:
        """GET /results/<handle>/<文件名>：200 流式返回；格式不符或不存在时 404。"""
        handle = request.path_params.get("handle")
        file_name = request.path_params.get("file_name")
        if not is_valid_result_handle(handle) or not is_valid_result_file_name(file_name):
            return _not_found()
        # 打开与 fstat 在事件循环中直接做（本地磁盘，耗时很短），避免线程返回前被取消而泄漏描述符。
        opened = self.store.open_result(handle, file_name)
        if opened is None:
            return _not_found()
        fd, size = opened
        return _ResultFileResponse(fd, size)

    async def delete(self, request: Request) -> Response:
        """DELETE /results/<handle>：删除整个目录并返回 204；格式不符或不存在时 404。"""
        handle = request.path_params.get("handle")
        if not is_valid_result_handle(handle):
            return _not_found()
        try:
            removed = await anyio.to_thread.run_sync(self.store.remove_handle_dir, handle)
        except OSError as e:
            _log_os_error("删除结果目录失败", self.store.handle_dir(handle), e)
            return PlainTextResponse("Internal Server Error", status_code=500)
        self.store.forget(handle)
        if not removed:
            return _not_found()
        return Response(status_code=204)


@dataclass(frozen=True)
class ResultRouteSpec:
    """一条 /results/ 路由：供 FastMCP.custom_route 或 starlette Route 使用。"""

    path: str
    endpoint: Callable[[Request], Awaitable[Response]]
    methods: tuple[str, ...]
    name: str


def result_route_specs(store: ResultStore) -> tuple[ResultRouteSpec, ...]:
    """/results/ 的两条路由。GET 路由由 Starlette 自动同时接受 HEAD。"""
    handlers = ResultHandlers(store)
    return (
        ResultRouteSpec(RESULT_FILE_ROUTE, handlers.download, ("GET",), "results_download"),
        ResultRouteSpec(RESULT_HANDLE_ROUTE, handlers.delete, ("DELETE",), "results_delete"),
    )


def result_routes(store: ResultStore) -> list[Route]:
    """/results/ 的 Starlette 路由（测试或自行组装应用时使用）。"""
    return [
        Route(spec.path, endpoint=spec.endpoint, methods=list(spec.methods), name=spec.name, include_in_schema=False)
        for spec in result_route_specs(store)
    ]


def mount_result_routes(mcp: FastMCP, store: ResultStore) -> None:
    """用 FastMCP.custom_route 挂载 /results/；须在 streamable_http_app() 之前调用。

    custom_route 的路由不经过 SDK 自带的认证，认证由包住整个应用的 ASGI 中间件完成（8.6）。
    """
    for spec in result_route_specs(store):
        mcp.custom_route(spec.path, methods=list(spec.methods), name=spec.name, include_in_schema=False)(
            spec.endpoint
        )
