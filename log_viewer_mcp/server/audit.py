"""审计日志（需求 6.3、6.4、6.5；design.md「审计（audit.py）」与「Data Models」）。

- 每次工具调用结束（含被拒绝的调用）向 ``<audit.dir>/audit-YYYY-MM-DD.jsonl`` 追加一行 JSON。
  日期与记录中的 time 都按服务器时区（Config.tz，例如 -02:00）计算，取写入时刻。
- 序列化前，把记录中所有字符串（含对象的键）里的数据库密码与本请求 token 原文替换为 ``***``。
  掩码在序列化之前做：JSON 转义会改变含引号、反斜杠或非 ASCII 字符的原文，序列化后再替换会漏掉。
- asyncio.Lock 保证同一进程内各条记录依次写入；每条记录用一次 os.write（不足时续写）写入
  以 O_APPEND 打开的文件，没有用户态缓冲，写完即到内核。写到一半失败时截回写入前的长度，
  不留下半行。
- 写入失败抛出 AuditWriteError（错误码 AUDIT_FAILED），由调用方返回 AUDIT_FAILED 并删除本次结果。
- 后台任务删除日期早于 Audit_Retention 的文件：今天为 D 时删除日期早于 D − retention_days 的文件，
  这些文件中最新的记录也已超过保留时长。
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import stat
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from typing import Any

from log_viewer_mcp.common.errors import ErrorCode, LogViewerError
from log_viewer_mcp.server.config import Config, ConfigError

__all__ = [
    "AUDIT_FILE_PATTERN",
    "AuditLog",
    "AuditTarget",
    "AuditWriteError",
    "MASK",
    "audit_file_name",
    "format_time",
    "mask_secrets",
    "serialize_record",
]

logger = logging.getLogger(__name__)

MASK = "***"

# audit-YYYY-MM-DD.jsonl；清理时只认这个格式且日期合法的文件。
AUDIT_FILE_PATTERN = re.compile(r"audit-([0-9]{4})-([0-9]{2})-([0-9]{2})\.jsonl")

DEFAULT_CLEANUP_INTERVAL_S = 3600

_OPEN_FLAGS = (
    os.O_WRONLY
    | os.O_APPEND
    | os.O_CREAT
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_BINARY", 0)
)


class AuditWriteError(LogViewerError):
    """审计记录写入失败；调用方应返回 AUDIT_FAILED 并删除本次结果（6.5）。"""

    def __init__(self) -> None:
        super().__init__(ErrorCode.AUDIT_FAILED)


@dataclass(frozen=True)
class AuditTarget:
    """一个目标游戏服：svr_id、ip_inner（查不到时为 None）、在游戏服上执行的脚本与该服状态。"""

    svr_id: int
    ip_inner: str | None
    scripts: Sequence[str] = field(default_factory=tuple)
    status: str = "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "svr_id": self.svr_id,
            "ip_inner": self.ip_inner,
            "scripts": list(self.scripts),
            "status": self.status,
        }


# ---------------------------------------------------------------------------
# 纯函数：时间、文件名、掩码、序列化
# ---------------------------------------------------------------------------


def format_time(ts: float, tz: tzinfo) -> str:
    """ISO 8601、秒精度、带偏移的服务器时间，例如 2026-09-17T16:53:12-02:00。"""
    return datetime.fromtimestamp(int(ts), tz).isoformat(timespec="seconds")


def audit_file_name(day: date) -> str:
    return f"audit-{day.isoformat()}.jsonl"


def _secret_list(secrets: Iterable[str | None]) -> list[str]:
    # 去掉空值；长的先替换，避免一个密钥是另一个的子串时只替换了一部分。
    unique = {s for s in secrets if isinstance(s, str) and s != ""}
    return sorted(unique, key=len, reverse=True)


def _mask_text(text: str, secrets: Sequence[str]) -> str:
    for s in secrets:
        if s in text:
            text = text.replace(s, MASK)
    return text


def mask_secrets(value: Any, secrets: Iterable[str | None]) -> Any:
    """返回 value 的副本，其中所有字符串（含对象的键）里的各个密钥原文替换为 ``***``。

    dict、list、tuple 递归处理；str、int、float、bool、None 以外的值先转为字符串再掩码，
    保证序列化不会因类型失败，也不会绕过掩码。
    """
    return _mask(value, _secret_list(secrets))


def _mask(value: Any, secrets: Sequence[str]) -> Any:
    if isinstance(value, str):
        return _mask_text(value, secrets)
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        # NaN 与 ±Infinity 不是合法 JSON，改写为字符串。
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Mapping):
        return {_mask_text(str(k), secrets): _mask(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mask(v, secrets) for v in value]
    if isinstance(value, AuditTarget):
        return _mask(value.to_dict(), secrets)
    return _mask_text(str(value), secrets)


def serialize_record(record: Mapping[str, Any], secrets: Iterable[str | None]) -> bytes:
    """掩码后序列化为一行 UTF-8 JSON（以 LF 结尾）。

    JSON 会转义字符串中的 CR、LF 等控制字符，所以输出只有末尾一个换行。字符串中有孤立代理项
    （例如被拒绝调用的参数）时无法编码为 UTF-8，改用 \\uXXXX 转义输出。
    """
    masked = mask_secrets(record, secrets)
    text = json.dumps(masked, ensure_ascii=False)
    try:
        data = text.encode("utf-8")
    except UnicodeEncodeError:
        data = json.dumps(masked, ensure_ascii=True).encode("ascii")
    return data + b"\n"


# ---------------------------------------------------------------------------
# 写入与清理
# ---------------------------------------------------------------------------


class AuditLog:
    """按天的 JSONL 审计日志。

    db_password 在构造时给出；token 按请求在 write 时给出。两者在每条记录中都会被掩码。
    clock 返回 Unix 时间（秒），测试可注入。
    """

    def __init__(
        self,
        directory: str,
        tz: tzinfo,
        retention_days: int,
        *,
        db_password: str | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if type(retention_days) is not int or retention_days < 1:
            raise ValueError("retention_days 必须是正整数")
        self.directory = directory
        self.tz = tz
        self.retention_days = retention_days
        self._db_password = db_password
        self._clock = clock
        self._lock = asyncio.Lock()

    @classmethod
    def from_config(cls, config: Config, *, clock: Callable[[], float] = time.time) -> AuditLog:
        return cls(
            config.audit.dir,
            config.tz,
            config.audit.retention_days,
            db_password=config.admin_db.password,
            clock=clock,
        )

    # -- 启动 ---------------------------------------------------------------

    def prepare(self) -> None:
        """启动检查：审计目录不存在则以 0700 创建；须是真实目录（不是符号链接），否则 ConfigError。"""
        try:
            os.makedirs(self.directory, mode=0o700, exist_ok=True)
            st = os.lstat(self.directory)
        except OSError as e:
            raise ConfigError(f"审计目录 {self.directory} 不可用：{e.strerror}") from None
        if not stat.S_ISDIR(st.st_mode):
            raise ConfigError(f"审计目录 {self.directory} 不是目录（或是符号链接）")
        if not os.access(self.directory, os.W_OK | os.X_OK):
            raise ConfigError(f"审计目录 {self.directory} 不可写")

    # -- 写入 ---------------------------------------------------------------

    def build_record(
        self,
        *,
        now: float,
        user: str | None,
        tool: str,
        params: Any,
        status: str,
        duration_ms: int,
        result_handle: str | None = None,
        targets: Iterable[AuditTarget | Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        """按 design.md「Data Models」的字段顺序组装记录（未掩码）。"""
        return {
            "time": format_time(now, self.tz),
            "user": user,
            "tool": tool,
            "result_handle": result_handle,
            "params": params,
            "targets": [t.to_dict() if isinstance(t, AuditTarget) else dict(t) for t in targets],
            "status": status,
            "duration_ms": int(duration_ms),
        }

    async def write(
        self,
        *,
        user: str | None,
        tool: str,
        params: Any,
        status: str,
        duration_ms: int,
        result_handle: str | None = None,
        targets: Iterable[AuditTarget | Mapping[str, Any]] = (),
        token: str | None = None,
    ) -> None:
        """追加一条记录；时间取写入时刻。失败抛出 AuditWriteError（6.5）。

        token 为本请求的 token 原文，与数据库密码一起在记录中被掩码（6.4）。
        """
        try:
            now = self._clock()
            record = self.build_record(
                now=now,
                user=user,
                tool=tool,
                params=params,
                status=status,
                duration_ms=duration_ms,
                result_handle=result_handle,
                targets=targets,
            )
            line = serialize_record(record, (self._db_password, token))
            path = os.path.join(self.directory, audit_file_name(self._day(now)))
        except Exception:
            logger.exception("审计记录序列化失败")
            raise AuditWriteError() from None
        async with self._lock:
            try:
                await asyncio.to_thread(_append_line, self.directory, path, line)
            except OSError as e:
                # 只写错误类别与路径，不写记录内容（可能含未知的敏感数据）。
                logger.error("审计记录写入失败：%s：%s", path, e.strerror or type(e).__name__)
                raise AuditWriteError() from None

    def _day(self, ts: float) -> date:
        return datetime.fromtimestamp(int(ts), self.tz).date()

    # -- 清理 ---------------------------------------------------------------

    def cleanup(self, now: float | None = None) -> list[str]:
        """删除日期早于（今天 − retention_days）的审计文件，返回被删除的文件名。"""
        if now is None:
            now = self._clock()
        cutoff = self._day(now) - timedelta(days=self.retention_days)
        removed: list[str] = []
        try:
            names = os.listdir(self.directory)
        except OSError as e:
            logger.warning("无法列举审计目录 %s：%s", self.directory, e.strerror)
            return removed
        for name in sorted(names):
            day = _parse_file_day(name)
            if day is None or day >= cutoff:
                continue
            path = os.path.join(self.directory, name)
            try:
                if not stat.S_ISREG(os.lstat(path).st_mode):
                    continue
                os.unlink(path)
            except FileNotFoundError:
                continue
            except OSError as e:
                logger.warning("无法删除过期审计文件 %s：%s", path, e.strerror)
                continue
            removed.append(name)
        if removed:
            logger.info("已删除过期审计文件 %d 个", len(removed))
        return removed

    async def run_cleanup(self, interval_s: float = DEFAULT_CLEANUP_INTERVAL_S) -> None:
        """后台任务：立即清理一次，之后每 interval_s 秒一次；被取消时结束。"""
        while True:
            try:
                await asyncio.to_thread(self.cleanup)
            except Exception:
                logger.exception("清理过期审计文件失败")
            await asyncio.sleep(interval_s)


def _parse_file_day(name: str) -> date | None:
    m = AUDIT_FILE_PATTERN.fullmatch(name)
    if m is None:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _append_line(directory: str, path: str, line: bytes) -> None:
    """在线程中执行：以 O_APPEND 打开（不跟随符号链接，新建为 0600），整行写入。

    写到一半出错时把文件截回写入前的长度，保证文件中只有完整的行。
    """
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd = os.open(path, _OPEN_FLAGS, 0o600)
    try:
        start = os.fstat(fd).st_size
        view = memoryview(line)
        try:
            while view:
                n = os.write(fd, view)
                if n <= 0:
                    raise OSError("审计文件写入返回 0 字节")
                view = view[n:]
        except OSError:
            try:
                os.ftruncate(fd, start)
            except OSError:
                pass
            raise
    finally:
        os.close(fd)
