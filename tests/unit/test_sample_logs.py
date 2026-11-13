"""集成测试样例日志生成器（tests/integration/sample_logs.py）的自检：确定性、行格式与清单的一致性。"""

from __future__ import annotations

import gzip
import io
import re
import tarfile

import pytest

from tests.integration import sample_logs as sl

# %d | %-5level | %thread | %logger{40} | %msg
LINE_RE = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3}) \| (INFO |WARN |ERROR|DEBUG) \| [^|]+ \| (\S{1,40}) \| .*")
ROTATED_RE = re.compile(r"smartfox\.log\.[0-9]{4}-[0-9]{2}-[0-9]{2}(-[0-9]{2}(-[0-9]{2})?)?")


@pytest.fixture(scope="module")
def samples() -> sl.SampleSet:
    return sl.build()


def test_build_is_deterministic(samples: sl.SampleSet) -> None:
    again = sl.build()
    assert again.tar_bytes() == samples.tar_bytes()
    assert again.manifest_json() == samples.manifest_json()


def test_log_lines_follow_logback_pattern(samples: sl.SampleSet) -> None:
    for f in samples.log_files:
        lines = f.content.decode("utf-8").split("\n")
        assert lines[-1] == "", f.path  # 每行以 LF 结尾
        lo = (f.covered_start - 1) * 1000
        hi = (f.covered_end if f.covered_end is not None else sl.GENERATED_AT) * 1000
        last = 0
        continuation = 0
        for line in lines[:-1]:
            m = LINE_RE.fullmatch(line)
            if m is None:
                continuation += 1  # 异常堆栈或多行消息的后续行，不带时间
                assert last, f"{f.path} 的第一行不是日志行"
                continue
            ms = sl.server_time(*map(int, re.split(r"[- :]", m.group(1)))) * 1000 + int(m.group(2))
            assert lo <= ms < hi and ms >= last, (f.path, line[:40])
            last = ms
        feats = sl.FEATURES.get((f.log_type, f.label), {})
        assert (continuation > 0) == ("stack" in feats or "inject" in feats), f.path
        assert f.content.count(f.marker.encode() + b"\n") == 1, f.path
        assert sl.IGNORED_MARKER.encode() not in f.content


def test_special_content_present(samples: sl.SampleSet) -> None:
    longest = max(len(line) for f in samples.log_files for line in f.content.split(b"\n"))
    assert longest >= 30_000
    for lt in sl.LOG_TYPES:
        # 每个文件都有中文
        assert all(re.search("[\u4e00-\u9fff]", f.content.decode()) for f in samples.log_files if f.log_type == lt)
    assert any(b"\n\tat " in f.content for f in samples.log_files)


def test_archives_like_cleanup_script(samples: sl.SampleSet) -> None:
    for p in samples.periods:
        if p.archive is None:
            continue
        assert p.archive.path == f"{sl.ZIP_DIR}/smartfox.log.{p.ts}{sl.LOG_TYPES[p.log_type].suffix}.tar.gz"
        if p.archive.corrupt:
            with pytest.raises(EOFError):
                gzip.decompress(p.archive.stored)
            continue
        assert gzip.decompress(p.archive.stored) == p.content
        # gzip -c 在头中记下原文件名（FLG.FNAME）
        assert p.archive.stored[3] & 0x08 and f"smartfox.log.{p.ts}\0".encode() in p.archive.stored[:64]
        if p.rotated is not None:
            assert p.rotated.content == p.content


def test_periods_and_tar_layout(samples: sl.SampleSet) -> None:
    for p in samples.periods:
        g, start, end = sl.parse_ts(p.ts)
        assert (p.granularity, p.start, p.end) == (g, start, end)
        if p.rotated:
            assert ROTATED_RE.fullmatch(p.rotated.name)
    for lt in sl.LOG_TYPES:
        ps = samples.periods_of(lt)
        assert all(a.end <= b.start for a, b in zip(ps, ps[1:]))  # 时段互不重叠
        assert samples.current(lt).covered_start == max(p.end for p in ps)
        assert {p.sources for p in ps} == {"rotated", "archive", "both"}
        assert {p.granularity for p in ps} == {"day", "hour", "minute"}

    with tarfile.open(fileobj=io.BytesIO(samples.tar_bytes())) as tf:
        members = tf.getmembers()
    by_path = {"/" + m.name: m for m in members}
    assert set(by_path) == {e.path for e in samples.entries}
    for path, m in by_path.items():
        assert (m.uid, m.gid, m.uname) == (0, 0, "root")
        if path.startswith(sl.SFS2X_ROOT + "/") and not m.issym():
            assert m.mode == 0o777, path
    kinds = {e.type for e in samples.entries if e.path.startswith(sl.LOG_TYPES["normal"].dir + "/")}
    assert kinds == {"file", "dir", "symlink"}
    assert sl.LOG_TYPES["normal"].dir + "/gc.log" in by_path
    assert by_path[sl.LOG_TYPES["normal"].dir + "/boot"].isdir()
