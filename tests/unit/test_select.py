"""文件名识别与 Covered_Period 的单元测试（任务 3.1，需求 2.2、2.3）。"""

from datetime import datetime, timedelta, timezone

import pytest

from log_viewer_mcp.server.select import (
    FileKind,
    Granularity,
    LogFile,
    archive_file_name,
    current_period,
    format_timestamp,
    parse_archive_name,
    parse_plain_name,
    parse_timestamp,
    rotated_file_name,
)

GMT_MINUS_2 = timezone(timedelta(hours=-2))


def unix(*args):
    return int(datetime(*args, tzinfo=GMT_MINUS_2).timestamp())


# 服务器时间 2026-10-06 21:13:00 GMT-2 = 2026-10-06T23:13:00Z
MINUTE_START = 1791328380


def test_minute_timestamp_is_server_time():
    assert parse_timestamp("2026-10-06-21-13", GMT_MINUS_2) == (MINUTE_START, Granularity.MINUTE)


@pytest.mark.parametrize(
    "ts, start, granularity",
    [
        ("2026-10-06-21-13", unix(2026, 10, 6, 21, 13), Granularity.MINUTE),
        ("2026-10-06-21", unix(2026, 10, 6, 21), Granularity.HOUR),
        ("2026-10-06", unix(2026, 10, 6), Granularity.DAY),
    ],
)
def test_rotated_covered_period(ts, start, granularity):
    f = parse_plain_name("smartfox.log." + ts, GMT_MINUS_2)
    assert f == LogFile(
        name="smartfox.log." + ts, kind=FileKind.ROTATED, ts=ts,
        granularity=granularity, start=start, end=start + granularity.seconds,
    )


def test_current_file():
    f = parse_plain_name("smartfox.log", GMT_MINUS_2)
    assert f is not None and f.kind is FileKind.CURRENT and f.start is None and f.end is None


@pytest.mark.parametrize(
    "suffix, name",
    [
        ("", "smartfox.log.2026-10-06-21-13.tar.gz"),
        (".logpushinit", "smartfox.log.2026-10-06-21-13.logpushinit.tar.gz"),
        (".loganalys", "smartfox.log.2026-10-06-21-13.loganalys.tar.gz"),
        (".polsarlog", "smartfox.log.2026-10-06-21-13.polsarlog.tar.gz"),
    ],
)
def test_archive_with_suffix(suffix, name):
    f = parse_archive_name(name, suffix, GMT_MINUS_2)
    assert f is not None
    assert (f.kind, f.ts, f.start, f.end) == (
        FileKind.ARCHIVE, "2026-10-06-21-13", MINUTE_START, MINUTE_START + 60,
    )


@pytest.mark.parametrize(
    "name, suffix",
    [
        # 其他类型的压缩文件不属于 normal，反之亦然
        ("smartfox.log.2026-10-06-21-13.logpushinit.tar.gz", ""),
        ("smartfox.log.2026-10-06-21-13.tar.gz", ".logpushinit"),
        ("smartfox.log.2026-10-06-21-13.loganalys.tar.gz", ".logpushinit"),
        # ziplogs 中的非压缩名称与 Current 都不认
        ("smartfox.log.2026-10-06-21-13", ""),
        ("smartfox.log", ""),
        ("smartfox.log.2026-10-06-21-13.tar.gz.bak", ""),
        ("smartfox.log.2026-10-06-21-13.gz", ""),
    ],
)
def test_archive_rejects_other_names(name, suffix):
    assert parse_archive_name(name, suffix, GMT_MINUS_2) is None


@pytest.mark.parametrize(
    "name",
    [
        "gc.log",
        "boot",
        "smartfox.log.",
        "smartfox.log ",
        "smartfox.log\n",
        "smartfox.log.2026-10-06-21-13\n",
        "smartfox.log.2026-10-06-21-13.tar.gz",  # 压缩文件不在非压缩目录中识别
        "smartfox.log.2026-10-06-21-13-05",  # 秒级粒度不存在
        "smartfox.log.2026-10-6",
        "smartfox.log.2026-02-30",  # 非法日期
        "smartfox.log.2026-10-06-24",  # 非法小时
        "smartfox.log.2026-10-06-21-60",  # 非法分钟
        "smartfox.log.0000-01-01",
        "smartfox.log.２０２６-10-06",  # 全角数字
        "smartfox.log.٢٠٢٦-10-06",  # 阿拉伯-印度数字
        "smartfox.log.2026_10_06",
        "Smartfox.log",
        "smartfox.log.1",
    ],
)
def test_plain_rejects_other_names(name):
    assert parse_plain_name(name, GMT_MINUS_2) is None


def test_non_str_names_are_ignored():
    assert parse_plain_name(None, GMT_MINUS_2) is None
    assert parse_plain_name(b"smartfox.log", GMT_MINUS_2) is None
    assert parse_archive_name(None, "", GMT_MINUS_2) is None


def test_invalid_suffix_is_a_programming_error():
    with pytest.raises(ValueError):
        parse_archive_name("smartfox.log.2026-10-06.tar.gz", "*", GMT_MINUS_2)


@pytest.mark.parametrize("granularity", list(Granularity))
def test_format_and_name_round_trip(granularity):
    ts = format_timestamp(MINUTE_START - MINUTE_START % granularity.seconds, granularity, timezone.utc)
    assert parse_timestamp(ts, timezone.utc)[1] is granularity
    ts = format_timestamp(unix(2026, 10, 6), granularity, GMT_MINUS_2)
    assert parse_timestamp(ts, GMT_MINUS_2) == (unix(2026, 10, 6), granularity)
    assert parse_plain_name(rotated_file_name(ts), GMT_MINUS_2).ts == ts
    assert parse_archive_name(archive_file_name(ts, ".polsarlog"), ".polsarlog", GMT_MINUS_2).ts == ts


def test_day_boundary_follows_server_timezone():
    # 服务器时间的 2026-10-06 一整天 = UTC 2026-10-06 02:00 至 2026-10-07 02:00
    f = parse_plain_name("smartfox.log.2026-10-06", GMT_MINUS_2)
    utc_start = int(datetime(2026, 10, 6, 2, tzinfo=timezone.utc).timestamp())
    assert (f.start, f.end) == (utc_start, utc_start + 86400)


def test_current_period():
    files = [
        parse_plain_name("smartfox.log", GMT_MINUS_2),
        parse_plain_name("smartfox.log.2026-10-06-21-12", GMT_MINUS_2),
        parse_archive_name("smartfox.log.2026-10-06-21-13.tar.gz", "", GMT_MINUS_2),
    ]
    assert current_period(files, MINUTE_START + 3600) == (MINUTE_START + 60, MINUTE_START + 3600)
    assert current_period(files[:1], 123) == (None, 123)
    assert current_period([], 123) == (None, 123)


# ---------------------------------------------------------------------------
# 文件选取与扫描上限（任务 3.3，需求 1.6、2.3、2.4、2.5、2.6、9.5）
# ---------------------------------------------------------------------------

from log_viewer_mcp.server.select import (  # noqa: E402
    END_SLACK_SECONDS,
    AvailableRange,
    ListedFile,
    SelectedFile,
    select_files,
)

T = unix(2026, 10, 6, 21, 0)  # 服务器时间 2026-10-06 21:00
BIG = 10**12


def minute(m):
    return unix(2026, 10, 6, 21, m)


def rot(m, size=100, inode=None):
    return ListedFile("smartfox.log.2026-10-06-21-%02d" % m, size, inode if inode is not None else 1000 + m)


def arc(m, size=10, suffix=""):
    return ListedFile("smartfox.log.2026-10-06-21-%02d%s.tar.gz" % (m, suffix), size)


def cur(size=100, inode=42):
    return ListedFile("smartfox.log", size, inode)


def select(plain, archives=(), *, start, end, now=None, max_scan_bytes=BIG, suffix=""):
    return select_files(
        plain, archives, start=start, end=end, now=now if now is not None else minute(30),
        max_scan_bytes=max_scan_bytes, suffix=suffix, tz=GMT_MINUS_2,
    )


def names(selection):
    return [f.name for f in selection.files]


def test_selects_files_intersecting_range_with_one_minute_slack():
    plain = [rot(m) for m in range(10, 16)] + [cur()]
    s = select(plain, start=minute(12), end=minute(14))
    # [21:12, 21:14 + 1 分钟) 与 21:12、21:13、21:14 三个文件相交；21:15 的起点等于窗口终点，不相交
    assert names(s) == [rot(m).name for m in (12, 13, 14)]
    assert END_SLACK_SECONDS == 60
    assert (s.skipped, s.scan_limited, s.available_range) == ((), False, None)


def test_pairs_rotated_and_archive_of_same_timestamp():
    plain = [rot(12, size=300), cur(size=7, inode=99)]
    archives = [arc(11, size=5), arc(12, size=30), arc(12, suffix=".loganalys")]
    s = select(plain, archives, start=minute(11), end=minute(30))
    assert s.files == (
        SelectedFile(plain=None, archive=arc(11).name, inode=None, ts="2026-10-06-21-11",
                     start=minute(11), end=minute(12), size=5),
        # 同一 TS 两处都有：读 Rotated，Archive 作后备，大小按 Rotated 计
        SelectedFile(plain=rot(12).name, archive=arc(12).name, inode=None, ts="2026-10-06-21-12",
                     start=minute(12), end=minute(13), size=300),
        # Current 最后，起点为最晚文件的终点，终点为 now，带 inode
        SelectedFile(plain="smartfox.log", archive=None, inode=99, ts=None,
                     start=minute(13), end=minute(30), size=7),
    )
    assert [f.is_current for f in s.files] == [False, False, True]
    assert names(s) == [arc(11).name, rot(12).name, "smartfox.log"]


def test_ignores_unrecognized_names_and_other_log_types():
    plain = [ListedFile("gc.log", 1, 1), ListedFile("smartfox.log.2026-10-06-21-12.tar.gz", 1, 2),
             ListedFile("smartfox.log.2026-02-30", 1, 3), rot(12)]
    archives = [arc(13, suffix=".logpushinit"), arc(14), ListedFile("smartfox.log.2026-10-06-21-15", 1)]
    s = select(plain, archives, start=minute(10), end=minute(20))
    assert names(s) == [rot(12).name, arc(14).name]


def test_archive_suffix_selects_its_log_type():
    archives = [arc(12), arc(12, suffix=".logpushinit"), arc(13, suffix=".logpushinit")]
    s = select([], archives, start=minute(10), end=minute(20), suffix=".logpushinit")
    assert names(s) == [arc(12, suffix=".logpushinit").name, arc(13, suffix=".logpushinit").name]


def test_sorted_by_start_across_granularities_current_last():
    plain = [cur(), rot(20), ListedFile("smartfox.log.2026-10-06-20", 5, 7), rot(5)]
    archives = [ListedFile("smartfox.log.2026-10-05.tar.gz", 9)]
    s = select(plain, archives, start=unix(2026, 10, 5, 12), end=minute(31))
    assert names(s) == [
        "smartfox.log.2026-10-05.tar.gz", "smartfox.log.2026-10-06-20",
        rot(5).name, rot(20).name, "smartfox.log",
    ]


def test_hourly_rolling_logs():
    # 按小时滚动（logback 的 %d{yyyy-MM-dd-HH}）：每个文件覆盖 1 小时，Current 从最晚文件的终点开始
    def hourly(h, size=100):
        return ListedFile("smartfox.log.2026-10-06-%02d" % h, size, 2000 + h)

    plain = [hourly(h) for h in range(16, 21)] + [cur()]
    archives = [ListedFile("smartfox.log.2026-10-06-15.loganalys.tar.gz", 9)]
    s = select(plain, archives, start=unix(2026, 10, 6, 15, 30), end=unix(2026, 10, 6, 17, 10),
               now=minute(30), suffix=".loganalys")
    assert names(s) == ["smartfox.log.2026-10-06-15.loganalys.tar.gz",
                        "smartfox.log.2026-10-06-16", "smartfox.log.2026-10-06-17"]
    assert [(f.start, f.end) for f in s.files] == [
        (unix(2026, 10, 6, 15), unix(2026, 10, 6, 16)),
        (unix(2026, 10, 6, 16), unix(2026, 10, 6, 17)),
        (unix(2026, 10, 6, 17), unix(2026, 10, 6, 18)),
    ]
    latest = select(plain, start=minute(5), end=minute(20), now=minute(30))
    assert names(latest) == ["smartfox.log"] and latest.files[0].start == unix(2026, 10, 6, 21)


def test_current_only_has_unbounded_start():
    s = select([cur()], start=minute(0), end=minute(5), now=minute(30))
    assert len(s.files) == 1 and s.files[0].is_current and s.files[0].start is None
    assert s.files[0].end == minute(30)


def test_scan_limit_stops_at_first_file_that_would_exceed():
    plain = [rot(10, size=10), rot(11, size=15), rot(12, size=20), rot(13, size=1), cur(size=1)]
    s = select(plain, start=minute(10), end=minute(30), max_scan_bytes=25)
    # 10 + 15 = 25 恰好不超过；加入 21:12 将超过，其后的小文件也不再读取
    assert names(s) == [rot(10).name, rot(11).name]
    assert s.skipped == (rot(12).name, rot(13).name, "smartfox.log")
    assert s.scan_limited is True and s.available_range is None


def test_scan_limit_counts_paired_entry_by_rotated_size():
    s = select([rot(12, size=50)], [arc(12, size=5)], start=minute(12), end=minute(13), max_scan_bytes=49)
    assert s.files == () and s.skipped == (rot(12).name,) and s.scan_limited
    assert s.available_range is None  # Time_Range 内有文件，只是被截断


def test_zero_scan_limit_still_reads_empty_files():
    s = select([rot(12, size=0), rot(13, size=1)], start=minute(12), end=minute(14), max_scan_bytes=0)
    assert names(s) == [rot(12).name] and s.skipped == (rot(13).name,)


def test_available_range_when_nothing_intersects():
    plain = [rot(10), rot(11)]
    archives = [arc(5), arc(6)]
    s = select(plain, archives, start=unix(2026, 10, 7, 0), end=unix(2026, 10, 7, 1))
    assert s.files == () and s.skipped == () and not s.scan_limited
    assert s.available_range == AvailableRange(earliest=minute(5), latest=minute(12))
    # 有 Current 时最晚为 now
    s = select(plain + [cur()], archives, start=minute(40), end=minute(50), now=minute(30))
    assert s.available_range == AvailableRange(earliest=minute(5), latest=minute(30))
    # 只有 Current 时起点不限
    s = select([cur()], start=minute(40), end=minute(50), now=minute(30))
    assert s.available_range == AvailableRange(earliest=None, latest=minute(30))


def test_no_files_at_all():
    s = select([ListedFile("gc.log", 1, 1)], [], start=minute(0), end=minute(10))
    assert (s.files, s.skipped, s.scan_limited, s.available_range) == ((), (), False, None)


def test_duplicate_records_are_taken_once():
    s = select([rot(12, size=1), rot(12, size=2)], [arc(12), arc(12)], start=minute(12), end=minute(13))
    assert len(s.files) == 1 and s.files[0].size == 1


def test_programming_errors_are_rejected():
    with pytest.raises(ValueError):
        select([ListedFile("smartfox.log", 1, None)], start=minute(0), end=minute(10))
    with pytest.raises(ValueError):
        select([rot(12, size=-1)], start=minute(0), end=minute(10))
    with pytest.raises(ValueError):
        select([], start=minute(0), end=minute(10), max_scan_bytes=-1)
    with pytest.raises(ValueError):
        select([], start=True, end=minute(10))
