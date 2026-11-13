"""日志文件的识别与选取（需求 2；design.md「文件选取（select.py，纯函数）」）。

本模块只含纯函数，不访问文件系统与网络：输入是 ① 列目录得到的文件名，输出是解析结果。

文件名（2.2）：
- 非压缩目录：``smartfox.log``（Current_Log_File）与 ``smartfox.log.<TS>``（Rotated_Log_File）；
- ziplogs：``smartfox.log.<TS><suffix>.tar.gz``（Archive_File），suffix 取请求 Log_Type
  在 [log_types] 中的配置值（normal 为空串）；
- 其余名称一律忽略。

TS 为 ``YYYY-MM-DD``、``YYYY-MM-DD-HH`` 或 ``YYYY-MM-DD-HH-mm``，是服务器时间，表示该文件
覆盖时段的起点，且必须是合法的日期时间。Covered_Period（2.3）：Rotated 与 Archive 为
[TS, TS + 粒度)，按 Server_Timezone（固定偏移）换算为 Unix 秒；Current 为
[同类型 Rotated 与 Archive 中最晚的终点, 当前时刻)，两者都没有时起点不限；最晚的终点不早于
当前时刻时为空区间，不会被选中。

所有规则用 ``re.fullmatch`` 与显式字符类 ``[0-9]``：``\\d`` 会匹配非 ASCII 数字，
``$`` 允许末尾换行。

文件选取（select_files，2.3–2.6、9.5、1.6）：
- 选出 Covered_Period 与 [start, end + 1 分钟) 相交的文件；同一 TS 两处都有时配成一项，
  读 Rotated，Archive 只作读取时的后备；按起点排序，Current 最后。
- 扫描上限按 ① 列出的磁盘大小累计。配对项按将要读取的文件计，即 Rotated 的大小
  （读取时 Rotated 已被压缩删除而改读 Archive，读的字节只会更少）。加入下一项将超过
  Max_Scan_Bytes 时停止，该项与其后各项全部记入 skipped 并标 scan_limit。
- 没有任何文件与 Time_Range 相交时给出 available_range：最早文件的 Covered_Period 起点
  （只有 Current 时为 None，即起点不限）与最晚文件的终点（有 Current 时为 now）。
  该类型一个文件都没有时也为 None。
"""

from __future__ import annotations

import enum
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

__all__ = [
    "CURRENT_FILE_NAME",
    "END_SLACK_SECONDS",
    "AvailableRange",
    "FileKind",
    "Granularity",
    "ListedFile",
    "LogFile",
    "SelectedFile",
    "Selection",
    "archive_file_name",
    "current_period",
    "format_timestamp",
    "parse_archive_name",
    "parse_plain_name",
    "parse_timestamp",
    "rotated_file_name",
    "select_files",
]

CURRENT_FILE_NAME = "smartfox.log"

# 2.3：选取时 Time_Range 的终点多放宽 1 分钟（异步 appender 可能把临近周期结束的行写进下一个文件）。
END_SLACK_SECONDS = 60

# TS 的三种粒度：天、小时、分钟。
_TS = r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?:-[0-9]{2}(?:-[0-9]{2})?)?"
_TS_PATTERN = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})(?:-([0-9]{2})(?:-([0-9]{2}))?)?")
_ROTATED_PATTERN = re.compile(r"smartfox\.log\.(" + _TS + r")")

# 压缩文件后缀：空串，或 "." 加字母开头（与 config.SUFFIX_PATTERN 一致）。
_SUFFIX_PATTERN = re.compile(r"(?:\.[A-Za-z][A-Za-z0-9_-]{0,31})?")

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_ONE_SECOND = timedelta(seconds=1)


class FileKind(enum.Enum):
    CURRENT = "current"
    ROTATED = "rotated"
    ARCHIVE = "archive"


class Granularity(enum.Enum):
    """切割粒度，值为秒数（固定偏移时区没有夏令时，天恒为 86400 秒）。"""

    DAY = 86400
    HOUR = 3600
    MINUTE = 60

    @property
    def seconds(self) -> int:
        return self.value


@dataclass(frozen=True)
class LogFile:
    """一个已识别的日志文件名。

    - name：文件名（只含 ``[A-Za-z0-9._-]``）。
    - kind：Current、Rotated 或 Archive。
    - ts：文件名中的时间戳文本（Current 为 None）；同一 ts 的 Rotated 与 Archive 是同一时段。
    - granularity：由 ts 的长度决定（Current 为 None）。
    - start、end：Covered_Period [start, end) 的 Unix 秒（Current 为 None，由 current_period 计算）。
    """

    name: str
    kind: FileKind
    ts: str | None = None
    granularity: Granularity | None = None
    start: int | None = None
    end: int | None = None


def parse_timestamp(ts: object, tz: timezone) -> tuple[int, Granularity] | None:
    """把文件名中的 TS（服务器时间）解析为 (起点 Unix 秒, 粒度)；格式不符或不是合法时间时返回 None。"""
    if type(ts) is not str:
        return None
    m = _TS_PATTERN.fullmatch(ts)
    if m is None:
        return None
    year, month, day, hour, minute = m.groups()
    if minute is not None:
        granularity = Granularity.MINUTE
    elif hour is not None:
        granularity = Granularity.HOUR
    else:
        granularity = Granularity.DAY
    try:
        local = datetime(
            int(year), int(month), int(day), int(hour or 0), int(minute or 0), tzinfo=tz
        )
    except ValueError:  # 非法日期（如 02-30、小时 24、年份 0000）
        return None
    return (local - _EPOCH) // _ONE_SECOND, granularity


def format_timestamp(start: int, granularity: Granularity, tz: timezone) -> str:
    """parse_timestamp 的逆运算：把 Unix 秒按 Server_Timezone 格式化为指定粒度的 TS。"""
    local = _EPOCH.astimezone(tz) + timedelta(seconds=start)
    text = "%04d-%02d-%02d" % (local.year, local.month, local.day)
    if granularity is not Granularity.DAY:
        text += "-%02d" % local.hour
    if granularity is Granularity.MINUTE:
        text += "-%02d" % local.minute
    return text


def rotated_file_name(ts: str) -> str:
    return "smartfox.log." + ts


def archive_file_name(ts: str, suffix: str) -> str:
    return "smartfox.log." + ts + suffix + ".tar.gz"


def _timed(name: str, kind: FileKind, ts: str, tz: timezone) -> LogFile | None:
    parsed = parse_timestamp(ts, tz)
    if parsed is None:
        return None
    start, granularity = parsed
    return LogFile(
        name=name, kind=kind, ts=ts, granularity=granularity,
        start=start, end=start + granularity.seconds,
    )


def parse_plain_name(name: object, tz: timezone) -> LogFile | None:
    """识别非压缩目录中的文件名：Current 或 Rotated；其他名称返回 None。"""
    if type(name) is not str:
        return None
    if name == CURRENT_FILE_NAME:
        return LogFile(name=name, kind=FileKind.CURRENT)
    m = _ROTATED_PATTERN.fullmatch(name)
    if m is None:
        return None
    return _timed(name, FileKind.ROTATED, m.group(1), tz)


def parse_archive_name(name: object, suffix: str, tz: timezone) -> LogFile | None:
    """识别 ziplogs 中属于该 Log_Type（由 suffix 区分）的 Archive；其他名称返回 None。"""
    if type(name) is not str:
        return None
    if type(suffix) is not str or _SUFFIX_PATTERN.fullmatch(suffix) is None:
        raise ValueError("压缩文件后缀不合法")
    pattern = re.compile(r"smartfox\.log\.(" + _TS + r")" + re.escape(suffix) + r"\.tar\.gz")
    m = pattern.fullmatch(name)
    if m is None:
        return None
    return _timed(name, FileKind.ARCHIVE, m.group(1), tz)


def current_period(files: Iterable[LogFile], now: int) -> tuple[int | None, int]:
    """Current_Log_File 的 Covered_Period：[同类型 Rotated 与 Archive 中最晚的终点, now)。

    files 为同一 Log_Type 已识别的文件（Current 自身会被忽略）；没有带时间戳的文件时起点为 None（不限）。
    """
    ends = [f.end for f in files if f.kind is not FileKind.CURRENT and f.end is not None]
    return (max(ends) if ends else None), now


# ---------------------------------------------------------------------------
# 文件选取与扫描上限
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ListedFile:
    """① 列出的一条记录：文件名、磁盘大小（字节）与 inode（只有非压缩目录的记录有）。"""

    name: str
    size: int
    inode: int | None = None


@dataclass(frozen=True)
class SelectedFile:
    """选中的一项，对应 ② 脚本中的一个序号（F[i] 与 Z[i]）。

    - plain：非压缩目录中的文件名（只在 ziplogs 中的时段为 None）。
    - archive：同一 TS 的压缩文件名（Current 与 ziplogs 中没有该 TS 时为 None）。
    - inode：Current 的 inode，② 按它定位改名后的文件（2.8）；其他项为 None。
    - ts、start、end：TS 与 Covered_Period；Current 的 ts 为 None，start 可为 None（起点不限），end 为 now。
    - size：计入扫描上限的字节数（配对项为 Rotated 的大小）。
    """

    plain: str | None
    archive: str | None
    inode: int | None
    ts: str | None
    start: int | None
    end: int
    size: int

    @property
    def is_current(self) -> bool:
        return self.plain == CURRENT_FILE_NAME

    @property
    def name(self) -> str:
        """摘要中使用的文件名：优先读取的那个文件。"""
        name = self.plain if self.plain is not None else self.archive
        assert name is not None
        return name


@dataclass(frozen=True)
class AvailableRange:
    """该类型现有日志的时间范围（Unix 秒）：earliest 为 None 表示起点不限（只有 Current）。"""

    earliest: int | None
    latest: int


@dataclass(frozen=True)
class Selection:
    """select_files 的结果。

    - files：要读取的项，按起点排序、Current 最后（Current 若被选中且未被截断，必为最后一项）。
    - skipped：因扫描上限未读的项的文件名（SelectedFile.name），顺序与 files 相接。
    - scan_limited：是否因 Max_Scan_Bytes 截断（摘要的 truncated 为 scan_limit）。
    - available_range：没有任何文件与 Time_Range 相交时给出，否则为 None。
    """

    files: tuple[SelectedFile, ...]
    skipped: tuple[str, ...]
    scan_limited: bool
    available_range: AvailableRange | None


def _check_int(name: str, value: object, *, minimum: int | None = None) -> int:
    if type(value) is not int or (minimum is not None and value < minimum):
        raise ValueError(f"{name} 不合法")
    return value


def _check_listed(entry: ListedFile) -> ListedFile:
    # 任何带 name、size（与 inode）属性的记录都可以，不要求是 ListedFile 实例
    _check_int("文件大小", entry.size, minimum=0)
    return entry


def select_files(
    plain: Iterable[ListedFile],
    archives: Iterable[ListedFile],
    *,
    start: int,
    end: int,
    now: int,
    max_scan_bytes: int,
    suffix: str,
    tz: timezone,
) -> Selection:
    """按 Time_Range [start, end) 从 ① 的两个目录列举结果中选取文件（纯函数）。

    plain 为非压缩目录的记录（Current 的记录须含 inode），archives 为 ziplogs 的记录；记录可以是
    ListedFile 或任何带 name、size、inode 属性的对象。名称不符 2.2 的忽略，同名记录只取第一条。suffix 为请求 Log_Type 的压缩文件后缀，tz 为 Server_Timezone，
    now 为当前时刻（Unix 秒，Current 的终点）。
    """
    start = _check_int("start", start)
    end = _check_int("end", end)
    now = _check_int("now", now)
    max_scan_bytes = _check_int("max_scan_bytes", max_scan_bytes, minimum=0)

    current: tuple[LogFile, ListedFile] | None = None
    rotated: dict[str, tuple[LogFile, ListedFile]] = {}
    archived: dict[str, tuple[LogFile, ListedFile]] = {}
    seen_plain: set[str] = set()
    seen_archive: set[str] = set()

    for entry in plain:
        parsed = parse_plain_name(entry.name, tz)
        if parsed is None or entry.name in seen_plain:
            continue
        entry = _check_listed(entry)
        seen_plain.add(entry.name)
        if parsed.kind is FileKind.CURRENT:
            _check_int("inode", entry.inode, minimum=0)  # ② 按 inode 定位 Current（2.8）
            current = (parsed, entry)
        else:
            assert parsed.ts is not None
            rotated[parsed.ts] = (parsed, entry)

    for entry in archives:
        parsed = parse_archive_name(entry.name, suffix, tz)
        if parsed is None or entry.name in seen_archive:
            continue
        entry = _check_listed(entry)
        seen_archive.add(entry.name)
        assert parsed.ts is not None
        archived[parsed.ts] = (parsed, entry)

    # 同一 TS 的 Rotated 与 Archive 配成一项；读 Rotated，大小按 Rotated 计。
    timed: list[SelectedFile] = []
    for ts in rotated.keys() | archived.keys():
        r = rotated.get(ts)
        a = archived.get(ts)
        info, listed = r if r is not None else a  # type: ignore[misc]
        assert info.start is not None and info.end is not None
        timed.append(SelectedFile(
            plain=r[0].name if r is not None else None,
            archive=a[0].name if a is not None else None,
            inode=None, ts=ts, start=info.start, end=info.end, size=listed.size,
        ))
    timed.sort(key=lambda f: (f.start, f.end, f.ts))

    candidates = list(timed)
    if current is not None:
        cur_start, cur_end = current_period(
            [info for info, _ in rotated.values()] + [info for info, _ in archived.values()], now,
        )
        candidates.append(SelectedFile(
            plain=CURRENT_FILE_NAME, archive=None, inode=current[1].inode,
            ts=None, start=cur_start, end=cur_end, size=current[1].size,
        ))

    # 半开区间相交：max(起点) < min(终点)。同类型文件的终点晚于 now 时（时钟回拨等），
    # Current 的 Covered_Period 为空，与任何 Time_Range 都不相交。
    window_end = end + END_SLACK_SECONDS
    hits = [
        f for f in candidates
        if (start if f.start is None else max(f.start, start)) < min(f.end, window_end)
    ]

    available: AvailableRange | None = None
    if not hits and candidates:
        earliest = candidates[0].start  # 只有 Current 时为 None
        latest = max(f.end for f in candidates)
        available = AvailableRange(earliest=earliest, latest=latest)

    selected: list[SelectedFile] = []
    total = 0
    for i, f in enumerate(hits):
        if total + f.size > max_scan_bytes:
            return Selection(
                files=tuple(selected),
                skipped=tuple(g.name for g in hits[i:]),
                scan_limited=True,
                available_range=None,
            )
        total += f.size
        selected.append(f)
    return Selection(files=tuple(selected), skipped=(), scan_limited=False, available_range=available)
