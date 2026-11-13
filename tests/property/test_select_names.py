"""Property 6：文件名解析与 Covered_Period（任务 3.2，需求 2.2、2.3）。

格式化与解析的往返，加不符合 2.2 三种格式的名称的拒绝。拒绝部分用本文件中独立写的参照判断
（按 "-" 切分、逐字符比对 ASCII 数字、再用 datetime 校验日期），不复用被测模块的正则。
"""

from datetime import datetime, timedelta, timezone

from hypothesis import assume, given
from hypothesis import strategies as st

from log_viewer_mcp.server.select import (
    CURRENT_FILE_NAME,
    FileKind,
    Granularity,
    archive_file_name,
    format_timestamp,
    parse_archive_name,
    parse_plain_name,
    parse_timestamp,
    rotated_file_name,
)

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_ASCII_DIGITS = "0123456789"
_FIELD_LENGTHS = {Granularity.DAY: 3, Granularity.HOUR: 4, Granularity.MINUTE: 5}

# Server_Timezone：固定偏移，含部署用的 GMT-2。
timezones = st.one_of(
    st.just(timezone(timedelta(hours=-2))),
    st.integers(min_value=-12 * 60, max_value=14 * 60).map(lambda m: timezone(timedelta(minutes=m))),
)

# Log_Type 的 suffix：设计中的四种取值，加任意符合 config.SUFFIX_PATTERN 的后缀。
suffixes = st.one_of(
    st.sampled_from(["", ".logpushinit", ".loganalys", ".polsarlog"]),
    st.from_regex(r"\.[A-Za-z][A-Za-z0-9_-]{0,31}", fullmatch=True),
)

# 非 ASCII 的十进制数字（Unicode 类别 Nd），如全角、阿拉伯-印度数字。
non_ascii_digits = st.characters(categories=["Nd"]).filter(lambda c: c not in _ASCII_DIGITS)


def _format_ts(local: datetime, granularity: Granularity) -> str:
    fields = [local.year, local.month, local.day, local.hour, local.minute]
    fields = fields[: _FIELD_LENGTHS[granularity]]
    return "%04d" % fields[0] + "".join("-%02d" % f for f in fields[1:])


def _truncate(local: datetime, granularity: Granularity) -> datetime:
    if granularity is Granularity.DAY:
        return local.replace(hour=0, minute=0)
    if granularity is Granularity.HOUR:
        return local.replace(minute=0)
    return local


def _reference_ts_valid(ts: str) -> bool:
    """参照判断：TS 为 YYYY-MM-DD[-HH[-mm]]（只含 ASCII 数字）且是合法的日期时间。"""
    parts = ts.split("-")
    if len(parts) not in (3, 4, 5):
        return False
    if [len(p) for p in parts] != [4, 2, 2, 2, 2][: len(parts)]:
        return False
    if not all(c in _ASCII_DIGITS for p in parts for c in p):
        return False
    values = [int(p) for p in parts] + [0] * (5 - len(parts))
    try:
        datetime(*values)
    except ValueError:
        return False
    return True


def _reference_plain(name: str) -> bool:
    prefix = CURRENT_FILE_NAME + "."
    return name == CURRENT_FILE_NAME or (
        name.startswith(prefix) and _reference_ts_valid(name[len(prefix):])
    )


def _reference_archive(name: str, suffix: str) -> bool:
    prefix = CURRENT_FILE_NAME + "."
    tail = suffix + ".tar.gz"
    return (
        len(name) >= len(prefix) + len(tail)
        and name.startswith(prefix)
        and name.endswith(tail)
        and _reference_ts_valid(name[len(prefix): len(name) - len(tail)])
    )


# 偏向「接近合法」的名称，使随机字符串既有被识别的也有被忽略的。
_near_alphabet = st.sampled_from(list(_ASCII_DIGITS + "-._ \n") + ["٢", "２", "\x00", "é"])
_near_ts = st.text(alphabet=_near_alphabet, max_size=20)
names = st.one_of(
    st.text(max_size=40),
    st.builds(lambda ts: CURRENT_FILE_NAME + "." + ts, _near_ts),
    st.builds(lambda ts, tail: CURRENT_FILE_NAME + "." + ts + tail, _near_ts, st.text(max_size=20)),
    st.builds(
        lambda ts, sfx: archive_file_name(ts, sfx), _near_ts,
        st.sampled_from(["", ".logpushinit", ".loganalys", ".polsarlog", ".x"]),
    ),
)


# Feature: log-viewer-mcp, Property 6: 文件名解析与 Covered_Period
@given(
    local=st.datetimes(min_value=datetime(1, 1, 1), max_value=datetime(9999, 12, 31, 23, 59)),
    granularity=st.sampled_from(list(Granularity)),
    tz=timezones,
    suffix=suffixes,
    other_suffix=suffixes,
)
def test_formatted_names_round_trip(local, granularity, tz, suffix, other_suffix):
    """**Validates: Requirements 2.2, 2.3**"""
    local = _truncate(local.replace(second=0, microsecond=0), granularity)
    ts = _format_ts(local, granularity)
    start = (local.replace(tzinfo=tz) - _EPOCH) // timedelta(seconds=1)
    end = start + granularity.seconds

    assert parse_timestamp(ts, tz) == (start, granularity)
    assert format_timestamp(start, granularity, tz) == ts

    rotated = parse_plain_name(rotated_file_name(ts), tz)
    assert rotated is not None
    assert (rotated.name, rotated.kind, rotated.ts, rotated.granularity, rotated.start, rotated.end) == (
        rotated_file_name(ts), FileKind.ROTATED, ts, granularity, start, end,
    )

    name = archive_file_name(ts, suffix)
    archive = parse_archive_name(name, suffix, tz)
    assert archive is not None
    assert (archive.name, archive.kind, archive.ts, archive.granularity, archive.start, archive.end) == (
        name, FileKind.ARCHIVE, ts, granularity, start, end,
    )

    # 压缩文件不在非压缩目录中识别，非压缩文件也不在 ziplogs 中识别；其他类型的后缀不属于本类型。
    assert parse_plain_name(name, tz) is None
    assert parse_archive_name(rotated_file_name(ts), suffix, tz) is None
    if other_suffix != suffix:
        assert parse_archive_name(archive_file_name(ts, other_suffix), suffix, tz) is None

    current = parse_plain_name(CURRENT_FILE_NAME, tz)
    assert current is not None and current.kind is FileKind.CURRENT


# Feature: log-viewer-mcp, Property 6: 文件名解析与 Covered_Period
@given(
    year=st.integers(min_value=0, max_value=9999),
    month=st.integers(min_value=0, max_value=99),
    day=st.integers(min_value=0, max_value=99),
    hour=st.integers(min_value=0, max_value=99),
    minute=st.integers(min_value=0, max_value=99),
    granularity=st.sampled_from(list(Granularity)),
    tz=timezones,
    suffix=suffixes,
)
def test_invalid_dates_are_ignored(year, month, day, hour, minute, granularity, tz, suffix):
    """**Validates: Requirements 2.2**"""
    fields = [year, month, day, hour, minute][: _FIELD_LENGTHS[granularity]]
    padded = fields + [0] * (5 - len(fields))
    try:
        datetime(*padded)
    except ValueError:
        pass
    else:
        assume(False)
    ts = "%04d" % fields[0] + "".join("-%02d" % f for f in fields[1:])

    assert parse_timestamp(ts, tz) is None
    assert parse_plain_name(rotated_file_name(ts), tz) is None
    assert parse_archive_name(archive_file_name(ts, suffix), suffix, tz) is None


# Feature: log-viewer-mcp, Property 6: 文件名解析与 Covered_Period
@given(
    local=st.datetimes(min_value=datetime(1, 1, 1), max_value=datetime(9999, 12, 31, 23, 59)),
    granularity=st.sampled_from(list(Granularity)),
    tz=timezones,
    suffix=suffixes,
    data=st.data(),
)
def test_mutated_names_are_ignored(local, granularity, tz, suffix, data):
    """多余字符与非 ASCII 数字：在合法名称中任意位置插入一个字符，或把 TS 中的一个数字换成非 ASCII 数字。

    **Validates: Requirements 2.2**
    """
    ts = _format_ts(local, granularity)
    valid = [CURRENT_FILE_NAME, rotated_file_name(ts), archive_file_name(ts, suffix)]
    index = data.draw(st.integers(min_value=0, max_value=2), label="kind")
    name = valid[index]

    # 插入一个任意字符：三种格式的各段长度与分隔符固定，插入一个字符必然不再合法。
    pos = data.draw(st.integers(min_value=0, max_value=len(name)), label="pos")
    char = data.draw(st.characters(), label="char")
    inserted = name[:pos] + char + name[pos:]
    assert parse_plain_name(inserted, tz) is None
    assert parse_archive_name(inserted, suffix, tz) is None

    # 把 TS 中的一个 ASCII 数字换成非 ASCII 数字。
    if index > 0:
        ts_start = len(CURRENT_FILE_NAME) + 1
        digit_positions = [ts_start + i for i, c in enumerate(ts) if c in _ASCII_DIGITS]
        pos = data.draw(st.sampled_from(digit_positions), label="digit_pos")
        replaced = name[:pos] + data.draw(non_ascii_digits, label="digit") + name[pos + 1:]
        assert parse_plain_name(replaced, tz) is None
        assert parse_archive_name(replaced, suffix, tz) is None


# Feature: log-viewer-mcp, Property 6: 文件名解析与 Covered_Period
@given(name=names, tz=timezones, suffix=suffixes)
def test_random_names_match_reference(name, tz, suffix):
    """任意名称：被识别当且仅当符合 2.2 的格式（参照判断），识别结果与名称一致。

    **Validates: Requirements 2.2, 2.3**
    """
    plain = parse_plain_name(name, tz)
    assert (plain is not None) == _reference_plain(name)
    if plain is not None:
        assert plain.name == name
        if plain.kind is FileKind.ROTATED:
            assert name == rotated_file_name(plain.ts)
            assert (plain.start, plain.granularity) == parse_timestamp(plain.ts, tz)
            assert plain.end == plain.start + plain.granularity.seconds
        else:
            assert plain.kind is FileKind.CURRENT and name == CURRENT_FILE_NAME

    archive = parse_archive_name(name, suffix, tz)
    assert (archive is not None) == _reference_archive(name, suffix)
    if archive is not None:
        assert archive.kind is FileKind.ARCHIVE
        assert archive.name == name == archive_file_name(archive.ts, suffix)
        assert (archive.start, archive.granularity) == parse_timestamp(archive.ts, tz)
        assert archive.end == archive.start + archive.granularity.seconds
