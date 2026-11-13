"""认证用的 token 集合：tokens.json 变化后重新读取（需求 6.1、6.6、6.7；design.md「服务入口（app.py）」）。

``TokenSet`` 记住上次读取时 tokens.json 的 ``(st_mtime_ns, st_size, st_ino)``。认证中间件在每个 HTTP 请求认证之前
调用 ``refresh``：对 tokens.json 做一次 ``os.stat``（本地文件，直接在事件循环中执行），与记录相同即直接返回；不同时
用 config.py 的 ``parse_tokens`` 重新读取，成功则整体替换集合并写一条 info 日志（只有有效 token 个数）。

- 文件不存在：集合为空（所有请求 401），写一条 warning；之后文件出现即按其内容认证。
- 读取或解析失败：沿用上次成功读取的集合，写一条 warning；同一个文件状态只警告一次，文件再变化时重试。
- Token_CLI 用 os.replace 写入，每次改动都会换 inode，所以同一秒内、大小相同的改动也能发现。

日志中不出现 token 与摘要：parse_tokens 的错误说明不含 token 与摘要的值。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable

from log_viewer_mcp.server.config import ConfigError, TokenRecord, parse_tokens, read_regular_file

__all__ = ["TokenSet"]

logger = logging.getLogger(__name__)

# 文件状态：(st_mtime_ns, st_size, st_ino)；文件不存在时为 _MISSING。
_Signature = tuple[int, int, int] | str
_MISSING = "missing"
_UNREAD = "unread"  # 尚未读取（构造时），与任何状态都不同


def _entries(tokens: Iterable[TokenRecord]) -> tuple[tuple[bytes, str], ...]:
    """未吊销的 (摘要的 ASCII 字节, 使用者)，供 hmac.compare_digest 比较。"""
    return tuple((t.sha256.encode("ascii"), t.user) for t in tokens if not t.revoked)


class TokenSet:
    """有效 token 的集合；path 为 None 时是固定集合（不读文件）。

    构造时即读取一次 path：config.py 在启动检查之前读过 tokens.json，构造时再读一次，启动期间的改动也不会漏掉。
    这次读取失败时沿用 tokens（启动时 config.py 读到的集合）；文件不存在时集合为空并写一条 warning。
    """

    def __init__(self, path: str | None, tokens: Iterable[TokenRecord] = ()) -> None:
        self._path = path
        self._entries = _entries(tokens)
        self._signature: _Signature = _UNREAD
        self._warned: _Signature | None = None  # 已为其写过读取失败警告的文件状态
        if path is not None:
            self.refresh(startup=True)

    @property
    def path(self) -> str | None:
        return self._path

    @property
    def entries(self) -> tuple[tuple[bytes, str], ...]:
        """当前集合（不检查文件）：未吊销的 (摘要, 使用者)。"""
        return self._entries

    @property
    def active_count(self) -> int:
        return len(self._entries)

    def refresh(self, *, startup: bool = False) -> tuple[tuple[bytes, str], ...]:
        """检查 tokens.json 的状态，变化时重新读取；返回认证该请求用的集合。"""
        path = self._path
        if path is None:
            return self._entries
        try:
            st = os.stat(path)
        except (FileNotFoundError, NotADirectoryError):
            signature: _Signature = _MISSING
        except OSError as e:
            # 无法得知文件状态：沿用旧集合，每种错误只警告一次。
            self._warn_once(f"stat:{e.errno}", "无法访问 token 登记文件 %s（%s），沿用上次的 token 集合（有效 %d 个）",
                            path, e.strerror, len(self._entries))
            return self._entries
        else:
            signature = (st.st_mtime_ns, st.st_size, st.st_ino)

        if signature == self._signature:
            return self._entries

        if signature == _MISSING:
            self._entries = ()
            self._signature = _MISSING
            self._warned = None
            if startup:
                logger.warning("token 登记文件 %s 不存在，以空 token 集合启动（所有请求返回 401），"
                               "用 log-viewer-mcp-token create 创建 token 后即生效", path)
            else:
                logger.warning("token 登记文件 %s 不存在，现在没有有效 token（所有请求返回 401）", path)
            return self._entries

        # 先取状态再读取：两者之间文件又被替换时，读到的是较新的内容，下一个请求发现状态不同会再读一次。
        try:
            records = parse_tokens(read_regular_file(path, "token 登记文件"), path)
        except ConfigError as e:
            self._signature = signature
            self._warn_once(signature, "重新读取 token 登记文件失败，沿用上次的 token 集合（有效 %d 个）：%s",
                            len(self._entries), e)
            return self._entries

        self._entries = _entries(records)
        self._signature = signature
        self._warned = None
        if not startup:
            logger.info("token 登记文件已重新读取：有效 token %d 个", len(self._entries))
        return self._entries

    def _warn_once(self, state: _Signature, message: str, *args: object) -> None:
        if self._warned == state:
            return
        self._warned = state
        logger.warning(message, *args)
