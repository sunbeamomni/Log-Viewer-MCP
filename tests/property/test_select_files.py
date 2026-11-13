"""Property 7：文件选取（任务 3.4，需求 2.3、2.4、9.5）。

把 select_files 的结果与本文件中逐个文件判断的参照实现对比。参照实现独立于被测模块：
文件名按前缀、后缀切分，TS 按 "-" 切分并逐字符比对 ASCII 数字，Covered_Period 用 datetime 计算，
相交按「两个半开区间 max(起点) < min(终点)」判断，不复用 select.py 的正则与内部函数。

生成的目录列举结果混合：天、小时、分钟三种粒度（含互相重叠的时段）、同一 TS 只在一处或两处都有、
其他 Log_Type 后缀的压缩文件、放错目录的文件、非法日期、任意字符串、重复记录，以及有或没有
Rotated 的 Current。
"""

from datetime import datetime, timedelta, timezone

from hypothesis import given
from hypothesis import strategies as st

from log_viewer_mcp.server.select import (
    AvailableRange,
    ListedFile,
    SelectedFile,
    select_files,
)

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_ASCII_DIGITS = "0123456789"
_PREFIX = "smartfox.log."
_CURRENT = "smartfox.log"
_SLACK = 60
_GRANULARITY_SECONDS = {3: 86400, 4: 3600, 5: 60}

# 生成的时间都落在这段服务器时间内，使文件之间、文件与 Time_Range 之间经常重叠。
_BASE = datetime(2026, 9, 16, 0, 0)
_WINDOW_MINUTES = 3 * 24 * 60

_SUFFIXES = ["", ".logpushinit", ".loganalys", ".polsarlog"]

# ---------------------------------------------------------------------------
# 参照实现
# ---------------------------------------------------------------------------


def _ref_period(ts: str, tz: timezone) -> tuple[int, int] | None:
    """TS 合法时返回 Covered_Period [start, end)（Unix 秒），否则 None。"""
    parts = ts.split("-")
    if len(parts) not in _GRANULARITY_SECONDS:
        return None
    if [len(p) for p in parts] != [4, 2, 2, 2, 2][: len(parts)]:
        return None
    if not all(c in _ASCII_DIGITS for p in parts for c in p):
        return None
    values = [int(p) for p in parts] + [0] * (5 - len(parts))
    try:
        local = datetime(*values, tzinfo=tz)
    except ValueError:
        return None
    start = int((local - _EPOCH).total_seconds())
    return start, start + _GRANULARITY_SECONDS[len(parts)]


def _ref_rotated_ts(name: str) -> str | None:
    return name[len(_PREFIX):] if name.startswith(_PREFIX) else None


def _ref_archive_ts(name: str, suffix: str) -> str | None:
    tail = suffix + ".tar.gz"
    if len(name) < len(_PREFIX) + len(tail) or not name.startswith(_PREFIX) or not name.endswith(tail):
        return None
    return name[len(_PREFIX): len(name) - len(tail)]


def _intersects(a_start, a_end, b_start, b_end) -> bool:
    """半开区间相交；起点 None 表示不限。"""
    lo = b_start if a_start is None else max(a_start, b_start)
    return lo < min(a_end, b_end)


def _reference(plain, archives, *, start, end, now, max_scan_bytes, suffix, tz):
    """逐个文件判断的参照实现，返回 (files, skipped, scan_limited, available_range)。"""
    current = None
    rotated: dict[str, tuple[str, int, tuple[int, int]]] = {}
    archived: dict[str, tuple[str, int, tuple[int, int]]] = {}

    for entry in plain:  # 同名记录只取第一条
        if entry.name == _CURRENT:
            if current is None:
                current = entry
            continue
        ts = _ref_rotated_ts(entry.name)
        period = None if ts is None else _ref_period(ts, tz)
        if period is not None and ts not in rotated:
            rotated[ts] = (entry.name, entry.size, period)

    for entry in archives:
        ts = _ref_archive_ts(entry.name, suffix)
        period = None if ts is None else _ref_period(ts, tz)
        if period is not None and ts not in archived:
            archived[ts] = (entry.name, entry.size, period)

    # 每个时段一项：有非压缩文件时读它并按它的大小计，压缩文件只作后备。
    items = []
    for ts in set(rotated) | set(archived):
        r, a = rotated.get(ts), archived.get(ts)
        name, size, (p_start, p_end) = r if r is not None else a
        items.append(SelectedFile(
            plain=r[0] if r else None, archive=a[0] if a else None, inode=None,
            ts=ts, start=p_start, end=p_end, size=size,
        ))
    items.sort(key=lambda f: (f.start, f.end))  # 同一 (start, end) 只可能是同一 TS
    if current is not None:
        cur_start = max((f.end for f in items), default=None)
        items.append(SelectedFile(
            plain=_CURRENT, archive=None, inode=current.inode,
            ts=None, start=cur_start, end=now, size=current.size,
        ))

    hits = [f for f in items if _intersects(f.start, f.end, start, end + _SLACK)]

    available = None
    if items and not hits:
        starts = [f.start for f in items]
        earliest = None if None in starts else min(starts)
        available = AvailableRange(earliest=earliest, latest=max(f.end for f in items))

    files, total = [], 0
    for i, f in enumerate(hits):
        if total + f.size > max_scan_bytes:
            return files, [g.plain or g.archive for g in hits[i:]], True, None
        total += f.size
        files.append(f)
    return files, [], False, available


# ---------------------------------------------------------------------------
# 生成器
# ---------------------------------------------------------------------------

timezones = st.one_of(
    st.just(timezone(timedelta(hours=-2))),
    st.integers(min_value=-12 * 60, max_value=14 * 60).map(lambda m: timezone(timedelta(minutes=m))),
)


@st.composite
def timestamps(draw) -> str:
    """窗口内任意时刻按天、小时或分钟粒度格式化的 TS（不同粒度的时段互相重叠）。"""
    local = _BASE + timedelta(minutes=draw(st.integers(0, _WINDOW_MINUTES)))
    fields = [local.year, local.month, local.day, local.hour, local.minute]
    fields = fields[: draw(st.sampled_from([3, 4, 5]))]
    return "%04d" % fields[0] + "".join("-%02d" % f for f in fields[1:])


bad_timestamps = st.sampled_from([
    "2026-02-30", "2026-09-17-24", "2026-09-17-10-60", "0000-01-01", "2026-9-17",
    "2026-09-17-1", "2026-09-17-10-00-00", "2026-09-17 10", "２026-09-17", "2026-09-17\n",
])
junk = st.one_of(
    st.text(max_size=30),
    st.sampled_from(["gc.log", "boot", "smartfox.log.", "smartfox.log.1", "smartfox.logx", ".smartfox.log"]),
)
sizes = st.integers(min_value=0, max_value=1000)
inodes = st.integers(min_value=0, max_value=2**40)


@st.composite
def scenarios(draw):
    tz = draw(timezones)
    suffix = draw(st.sampled_from(_SUFFIXES))
    other_suffixes = [s for s in _SUFFIXES if s != suffix] + [".x"]
    pool = draw(st.lists(timestamps(), max_size=10, unique=True))

    plain_names = []
    archive_names = []
    for ts in pool:  # 每个 TS 只在非压缩目录、只在 ziplogs，或两处都有
        where = draw(st.sampled_from(["plain", "archive", "both"]))
        if where != "archive":
            plain_names.append(_PREFIX + ts)
        if where != "plain":
            archive_names.append(_PREFIX + ts + suffix + ".tar.gz")
    if draw(st.booleans()):
        plain_names.append(_CURRENT)

    any_ts = st.one_of(st.sampled_from(pool), timestamps()) if pool else timestamps()
    plain_noise = st.one_of(
        junk,
        st.builds(lambda ts: _PREFIX + ts, bad_timestamps),
        st.builds(lambda ts: _PREFIX + ts + suffix + ".tar.gz", any_ts),  # 压缩文件放在非压缩目录
    )
    archive_noise = st.one_of(
        junk,
        st.just(_CURRENT),
        st.builds(lambda ts: _PREFIX + ts, any_ts),  # 非压缩文件放在 ziplogs
        st.builds(lambda ts, s: _PREFIX + ts + s + ".tar.gz", any_ts, st.sampled_from(other_suffixes)),
        st.builds(lambda ts: _PREFIX + ts + suffix + ".tar.gz", bad_timestamps),
    )
    plain_names += draw(st.lists(plain_noise, max_size=4))
    archive_names += draw(st.lists(archive_noise, max_size=4))

    # 重复记录（同名只取第一条），然后打乱顺序
    plain_names += draw(st.lists(st.sampled_from(plain_names), max_size=2)) if plain_names else []
    archive_names += draw(st.lists(st.sampled_from(archive_names), max_size=2)) if archive_names else []
    plain_names = draw(st.permutations(plain_names))
    archive_names = draw(st.permutations(archive_names))

    plain = [ListedFile(n, draw(sizes), draw(inodes)) for n in plain_names]
    archives = [ListedFile(n, draw(sizes)) for n in archive_names]

    base = int((_BASE.replace(tzinfo=tz) - _EPOCH).total_seconds())
    span = (_WINDOW_MINUTES + 2 * 24 * 60) * 60
    start = base - 86400 + draw(st.integers(0, span))
    if pool and draw(st.booleans()):  # Time_Range 贴近某个文件，多覆盖相交的边界
        start = _ref_period(draw(st.sampled_from(pool)), tz)[0] + draw(st.integers(-7200, 7200))
    end = start + draw(st.one_of(st.integers(1, 600), st.integers(1, 2 * 86400)))
    now = base - 86400 + draw(st.integers(0, span))
    if pool and draw(st.booleans()):
        # now 贴近某个文件的终点：Current 的 Covered_Period 很短或为空（终点晚于 now，如时钟回拨）
        now = _ref_period(draw(st.sampled_from(pool)), tz)[1] + draw(st.integers(-120, 120))
    total = sum(f.size for f in plain) + sum(f.size for f in archives)
    max_scan_bytes = draw(st.integers(min_value=0, max_value=total + 1))
    return dict(
        plain=plain, archives=archives, start=start, end=end, now=now,
        max_scan_bytes=max_scan_bytes, suffix=suffix, tz=tz,
    )


# Feature: log-viewer-mcp, Property 7: 文件选取
@given(scenario=scenarios())
def test_select_files_matches_reference(scenario):
    """**Validates: Requirements 2.3, 2.4, 9.5**"""
    result = select_files(**scenario)
    files, skipped, scan_limited, available = _reference(**scenario)

    assert list(result.files) == files
    assert list(result.skipped) == skipped
    assert result.scan_limited is scan_limited
    assert result.available_range == available

    # 与参照无关的直接检查：累计大小不超过上限，同一 TS 只出现一次且有非压缩文件时读它。
    assert sum(f.size for f in result.files) <= scenario["max_scan_bytes"]
    timed = [f for f in result.files if f.ts is not None]
    assert len({f.ts for f in timed}) == len(timed)
    plain_names = {e.name for e in scenario["plain"]}
    for f in timed:
        assert f.plain == (_PREFIX + f.ts if _PREFIX + f.ts in plain_names else None)
