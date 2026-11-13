"""server/audit.py 的单元测试（任务 7.5；需求 6.3、6.4、6.5）。

覆盖记录字段与服务器时区、掩码、保留期清理、写入失败与并发写入不交错（design.md「Testing Strategy」）。
符号链接与权限位相关的测试在不支持的系统上跳过，在测试运行容器（Linux）中执行。
"""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import stat
import time
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from log_viewer_mcp.common.errors import ErrorCode
from log_viewer_mcp.server import audit
from log_viewer_mcp.server.audit import (
    MASK,
    AuditLog,
    AuditTarget,
    AuditWriteError,
    audit_file_name,
    format_time,
    mask_secrets,
    serialize_record,
)
from log_viewer_mcp.server.config import ConfigError

TZ = timezone(timedelta(hours=-2))
# UTC 2026-09-18 01:00:00 = 服务器时间 2026-09-17 23:00:00-02:00：UTC 日期与服务器日期不同。
TS = datetime(2026, 9, 18, 1, 0, 0, tzinfo=timezone.utc).timestamp()

# 含引号、反斜杠与中文的密钥：JSON 转义后与原文不同，序列化后再替换会漏掉。
PASSWORD = 'p"w\\d密码'
TOKEN = 'tk\\"令牌_AbC'

posix_only = pytest.mark.skipif(not hasattr(os, "geteuid"), reason="权限位只在 POSIX 系统上检查")

FIELDS = ["time", "user", "tool", "result_handle", "params", "targets", "status", "duration_ms"]


class Clock:
    def __init__(self, ts: float = TS) -> None:
        self.ts = ts

    def __call__(self) -> float:
        return self.ts


def make_log(directory, *, clock=None, retention_days=90, db_password=PASSWORD) -> AuditLog:
    return AuditLog(str(directory), TZ, retention_days, db_password=db_password, clock=clock or Clock())


def read_lines(path) -> list[dict]:
    data = path.read_bytes()
    assert data == b"" or data.endswith(b"\n")
    return [json.loads(line) for line in data.decode("utf-8").splitlines()]


def try_symlink(src, dst, *, target_is_directory=False) -> None:
    try:
        os.symlink(src, dst, target_is_directory=target_is_directory)
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"无法创建符号链接：{e}")


async def write_one(log: AuditLog, **overrides) -> None:
    kwargs = dict(user="zhangsan", tool="search_logs", params={"svr_ids": [1001]}, status="ok", duration_ms=12)
    kwargs.update(overrides)
    await log.write(**kwargs)


# ---------------------------------------------------------------------------
# 记录字段、时间与文件名（6.3）
# ---------------------------------------------------------------------------


def test_format_time_uses_server_timezone_with_second_precision() -> None:
    assert format_time(TS + 0.987, TZ) == "2026-09-17T23:00:00-02:00"
    assert format_time(TS, timezone.utc) == "2026-09-18T01:00:00+00:00"


def test_audit_file_name() -> None:
    assert audit_file_name(date(2026, 9, 7)) == "audit-2026-09-07.jsonl"


@pytest.mark.asyncio
async def test_record_fields_order_and_values(tmp_path) -> None:
    log = make_log(tmp_path)
    params = {"svr_ids": [1001, 1002], "log_type": "normal", "at_time": 1789671000, "include_patterns": ["CMD"]}
    await log.write(
        user="zhangsan",
        tool="search_logs",
        params=params,
        status="partial",
        duration_ms=3120,
        result_handle="q3Zr0V2m1yX8kD5sWb7aLg",
        targets=[
            AuditTarget(1001, "10.0.1.11", ["{ printf '\\0' …", "{ B=… "], "ok"),
            AuditTarget(1002, None, status="SERVER_NOT_FOUND"),
            {"svr_id": 1003, "ip_inner": "10.0.1.13", "scripts": [], "status": "TIMEOUT"},
        ],
    )
    path = tmp_path / "audit-2026-09-17.jsonl"
    assert os.listdir(tmp_path) == [path.name]  # 文件按服务器日期命名，不是 UTC 日期
    data = path.read_bytes()
    assert data.count(b"\n") == 1 and data.endswith(b"\n")
    record = json.loads(data)
    assert list(record) == FIELDS
    assert record == {
        "time": "2026-09-17T23:00:00-02:00",
        "user": "zhangsan",
        "tool": "search_logs",
        "result_handle": "q3Zr0V2m1yX8kD5sWb7aLg",
        "params": params,
        "targets": [
            {"svr_id": 1001, "ip_inner": "10.0.1.11", "scripts": ["{ printf '\\0' …", "{ B=… "], "status": "ok"},
            {"svr_id": 1002, "ip_inner": None, "scripts": [], "status": "SERVER_NOT_FOUND"},
            {"svr_id": 1003, "ip_inner": "10.0.1.13", "scripts": [], "status": "TIMEOUT"},
        ],
        "status": "partial",
        "duration_ms": 3120,
    }
    assert list(record["targets"][0]) == ["svr_id", "ip_inner", "scripts", "status"]
    assert "…".encode("utf-8") in data  # 非 ASCII 字符按 UTF-8 原样输出


@pytest.mark.asyncio
async def test_rejected_call_record_has_null_handle_and_no_targets(tmp_path) -> None:
    log = make_log(tmp_path)
    await log.write(user="lisi", tool="search_logs", params={"path": "/etc"}, status="INVALID_ARGUMENT", duration_ms=0)
    (record,) = read_lines(tmp_path / "audit-2026-09-17.jsonl")
    assert record["result_handle"] is None
    assert record["targets"] == []
    assert record["status"] == "INVALID_ARGUMENT"


@pytest.mark.asyncio
async def test_records_append_and_roll_over_by_server_day(tmp_path) -> None:
    clock = Clock()
    log = make_log(tmp_path, clock=clock)
    await write_one(log, tool="list_servers")
    clock.ts = TS + 30 * 60  # 23:30-02:00，同一天
    await write_one(log, tool="search_logs")
    clock.ts = TS + 60 * 60  # 次日 00:00-02:00
    await write_one(log, tool="search_logs")
    first = read_lines(tmp_path / "audit-2026-09-17.jsonl")
    second = read_lines(tmp_path / "audit-2026-09-18.jsonl")
    assert [r["time"] for r in first] == ["2026-09-17T23:00:00-02:00", "2026-09-17T23:30:00-02:00"]
    assert [r["tool"] for r in first] == ["list_servers", "search_logs"]
    assert [r["time"] for r in second] == ["2026-09-18T00:00:00-02:00"]


@pytest.mark.asyncio
async def test_time_is_taken_at_write(tmp_path) -> None:
    clock = Clock()
    log = make_log(tmp_path, clock=clock)
    clock.ts = TS + 5
    await write_one(log)
    (record,) = read_lines(tmp_path / "audit-2026-09-17.jsonl")
    assert record["time"] == "2026-09-17T23:00:05-02:00"


@posix_only
@pytest.mark.asyncio
async def test_prepare_and_write_modes(tmp_path) -> None:
    directory = tmp_path / "a" / "audit"
    log = make_log(directory)
    log.prepare()
    await write_one(log)
    assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(directory / "audit-2026-09-17.jsonl").st_mode) == 0o600


def test_prepare_rejects_file_and_symlink(tmp_path) -> None:
    not_dir = tmp_path / "file"
    not_dir.write_text("x")
    with pytest.raises(ConfigError):
        make_log(not_dir).prepare()
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try_symlink(real, link, target_is_directory=True)
    with pytest.raises(ConfigError):
        make_log(link).prepare()


@pytest.mark.parametrize("bad", [0, -1, True, "90", 1.5])
def test_retention_days_must_be_positive_int(tmp_path, bad) -> None:
    with pytest.raises(ValueError):
        AuditLog(str(tmp_path), TZ, bad)


@pytest.mark.asyncio
async def test_from_config_uses_dir_tz_retention_and_password(tmp_path) -> None:
    config = SimpleNamespace(
        tz=TZ,
        audit=SimpleNamespace(dir=str(tmp_path), retention_days=7),
        admin_db=SimpleNamespace(password=PASSWORD),
    )
    log = AuditLog.from_config(config, clock=Clock())
    assert (log.directory, log.tz, log.retention_days) == (str(tmp_path), TZ, 7)
    await write_one(log, params={"pw": PASSWORD})
    (record,) = read_lines(tmp_path / "audit-2026-09-17.jsonl")
    assert record["params"] == {"pw": MASK}


# ---------------------------------------------------------------------------
# 掩码（6.4）
# ---------------------------------------------------------------------------


def _secret_forms(secret: str) -> list[bytes]:
    """原文及其在 JSON 中可能出现的转义形式。"""
    return [
        secret.encode("utf-8"),
        json.dumps(secret, ensure_ascii=False)[1:-1].encode("utf-8"),
        json.dumps(secret)[1:-1].encode("ascii"),
    ]


@pytest.mark.asyncio
async def test_password_and_token_masked_in_values_keys_and_nested(tmp_path) -> None:
    log = make_log(tmp_path)
    await log.write(
        user="zhangsan",
        tool="search_logs",
        params={
            "include_patterns": [f"pre{PASSWORD}post", TOKEN],
            f"k{TOKEN}": {"deep": [[f"x{PASSWORD}{TOKEN}y"]]},
            PASSWORD: ("tuple", TOKEN),
        },
        status="ok",
        duration_ms=1,
        result_handle=f"h{TOKEN}",
        targets=[AuditTarget(1001, "10.0.1.11", [f"grep -e '{PASSWORD}'"], "ok")],
        token=TOKEN,
    )
    path = tmp_path / "audit-2026-09-17.jsonl"
    data = path.read_bytes()
    for secret in (PASSWORD, TOKEN):
        for form in _secret_forms(secret):
            assert form not in data
    (record,) = read_lines(path)
    assert record["params"] == {
        "include_patterns": [f"pre{MASK}post", MASK],
        f"k{MASK}": {"deep": [[f"x{MASK}{MASK}y"]]},
        MASK: ["tuple", MASK],
    }
    assert record["result_handle"] == f"h{MASK}"
    assert record["targets"][0]["scripts"] == [f"grep -e '{MASK}'"]


@pytest.mark.asyncio
async def test_token_masked_only_when_given(tmp_path) -> None:
    log = make_log(tmp_path)
    await write_one(log, params={"p": TOKEN}, token=None)
    await write_one(log, params={"p": TOKEN}, token=TOKEN)
    records = read_lines(tmp_path / "audit-2026-09-17.jsonl")
    assert [r["params"]["p"] for r in records] == [TOKEN, MASK]


def test_longer_secret_masked_first_when_overlapping() -> None:
    # 若先替换短的 "abc"，"abcdef" 会剩下 "***def"。
    assert mask_secrets(["xabcdefx", "abc"], ["abc", "abcdef"]) == [f"x{MASK}x", MASK]


@pytest.mark.parametrize("empty", [None, ""])
def test_empty_secrets_do_not_change_text(empty) -> None:
    value = {"a": ["text", "中文"], "n": 1}
    assert mask_secrets(value, [empty, None]) == value


def test_mask_non_json_values_converted_and_masked() -> None:
    class Weird:
        def __str__(self) -> str:
            return f"weird-{PASSWORD}"

    masked = mask_secrets(
        {"b": PASSWORD.encode("utf-8"), "o": Weird(), 7: "int-key", "nan": float("nan"), "inf": float("-inf"),
         "ok": [True, None, 1.5, 3]},
        [PASSWORD],
    )
    assert masked["o"] == f"weird-{MASK}"
    assert isinstance(masked["b"], str)  # bytes 先转字符串再掩码
    assert masked["7"] == "int-key"
    assert masked["nan"] == "nan" and masked["inf"] == "-inf"
    assert masked["ok"] == [True, None, 1.5, 3]
    json.dumps(masked, allow_nan=False)


def test_serialize_lone_surrogate_falls_back_to_ascii_escape() -> None:
    line = serialize_record({"params": {"p": "a\ud800b中", "pw": PASSWORD}}, [PASSWORD])
    assert line.endswith(b"\n") and line.count(b"\n") == 1
    line.decode("ascii")
    assert json.loads(line) == {"params": {"p": "a\ud800b中", "pw": MASK}}


def test_serialize_escapes_newlines_into_one_line() -> None:
    line = serialize_record({"params": {"p": "a\nb\r\nc\u2028d"}}, [])
    assert line.count(b"\n") == 1 and b"\r" not in line
    assert json.loads(line)["params"]["p"] == "a\nb\r\nc\u2028d"


# ---------------------------------------------------------------------------
# 写入失败（6.5）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unwritable_directory_raises_audit_failed(tmp_path) -> None:
    not_dir = tmp_path / "file"
    not_dir.write_text("x")
    log = make_log(not_dir)
    with pytest.raises(AuditWriteError) as ei:
        await write_one(log)
    assert ei.value.code is ErrorCode.AUDIT_FAILED
    assert ei.value.__cause__ is None


@pytest.mark.asyncio
async def test_audit_path_is_directory_raises_audit_failed(tmp_path) -> None:
    (tmp_path / "audit-2026-09-17.jsonl").mkdir()
    with pytest.raises(AuditWriteError):
        await write_one(make_log(tmp_path))


@pytest.mark.asyncio
async def test_serialization_failure_raises_audit_failed(tmp_path) -> None:
    class Broken:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    with pytest.raises(AuditWriteError):
        await write_one(make_log(tmp_path), params={"x": Broken()})
    assert os.listdir(tmp_path) == []


@pytest.mark.asyncio
async def test_partial_write_failure_truncates_to_complete_lines(tmp_path, monkeypatch, caplog) -> None:
    log = make_log(tmp_path)
    await write_one(log, params={"n": 1})
    path = tmp_path / "audit-2026-09-17.jsonl"
    before = path.read_bytes()

    real_write = os.write
    calls = []

    def failing_write(fd, data):
        calls.append(len(data))
        if len(calls) == 1:
            return real_write(fd, bytes(data[: len(data) // 2]))  # 先写入半行
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(audit.os, "write", failing_write)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(AuditWriteError) as ei:
        await write_one(log, params={"n": 2, "pw": PASSWORD, "big": "x" * 1000}, token=TOKEN)
    assert ei.value.code is ErrorCode.AUDIT_FAILED
    assert len(calls) == 2
    assert path.read_bytes() == before  # 截回写入前的长度，不留半行
    # 运行日志不含记录内容与密钥（6.4）
    assert PASSWORD not in caplog.text and TOKEN not in caplog.text and "x" * 100 not in caplog.text

    monkeypatch.setattr(audit.os, "write", real_write)
    await write_one(log, params={"n": 3})
    assert [r["params"]["n"] for r in read_lines(path)] == [1, 3]


@pytest.mark.asyncio
async def test_short_writes_are_continued(tmp_path, monkeypatch) -> None:
    real_write = os.write
    monkeypatch.setattr(audit.os, "write", lambda fd, data: real_write(fd, bytes(data[:7])))
    await write_one(make_log(tmp_path), params={"s": "中文" * 50})
    (record,) = read_lines(tmp_path / "audit-2026-09-17.jsonl")
    assert record["params"] == {"s": "中文" * 50}


@pytest.mark.asyncio
async def test_zero_byte_write_is_failure(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(audit.os, "write", lambda fd, data: 0)
    with pytest.raises(AuditWriteError):
        await write_one(make_log(tmp_path))
    assert (tmp_path / "audit-2026-09-17.jsonl").read_bytes() == b""


@pytest.mark.asyncio
async def test_does_not_follow_symlinked_audit_file(tmp_path) -> None:
    if not getattr(os, "O_NOFOLLOW", 0):
        pytest.skip("系统不支持 O_NOFOLLOW")
    victim = tmp_path / "victim"
    victim.write_bytes(b"keep\n")
    directory = tmp_path / "audit"
    directory.mkdir()
    try_symlink(victim, directory / "audit-2026-09-17.jsonl")
    with pytest.raises(AuditWriteError):
        await write_one(make_log(directory))
    assert victim.read_bytes() == b"keep\n"


# ---------------------------------------------------------------------------
# 并发写入不交错（6.3）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_writes_do_not_interleave(tmp_path, monkeypatch) -> None:
    real_write = os.write

    def slow_chunked_write(fd, data):
        # 每次只写 4 KiB 并让出 CPU：若没有锁，各线程的写入会交错。
        n = real_write(fd, bytes(data[:4096]))
        time.sleep(0)
        return n

    monkeypatch.setattr(audit.os, "write", slow_chunked_write)
    log = make_log(tmp_path)
    count = 20
    await asyncio.gather(
        *(write_one(log, params={"i": i, "blob": chr(ord("a") + i) * 50_000}, duration_ms=i) for i in range(count))
    )
    records = read_lines(tmp_path / "audit-2026-09-17.jsonl")
    assert sorted(r["params"]["i"] for r in records) == list(range(count))
    for r in records:
        assert r["params"]["blob"] == chr(ord("a") + r["params"]["i"]) * 50_000


# ---------------------------------------------------------------------------
# 保留期清理（6.4）
# ---------------------------------------------------------------------------


def _day_name(day: date) -> str:
    return audit_file_name(day)


def test_cleanup_deletes_only_valid_expired_regular_files(tmp_path) -> None:
    today = date(2026, 9, 17)  # TS 在服务器时区的日期
    log = make_log(tmp_path, retention_days=90)
    expired = [_day_name(today - timedelta(days=91)), _day_name(today - timedelta(days=400))]
    kept = [
        _day_name(today - timedelta(days=90)),  # 等于 today − retention，保留
        _day_name(today - timedelta(days=1)),
        _day_name(today),
        _day_name(today + timedelta(days=1)),
        "audit-2020-02-30.jsonl",  # 日期不合法
        "audit-2020-1-01.jsonl",
        "audit-2020-01-01.jsonl.bak",
        "audit-2020-01-01.json",
        "xaudit-2020-01-01.jsonl",
        "other.txt",
    ]
    for name in expired + kept:
        (tmp_path / name).write_text("{}\n")
    old_dir = tmp_path / "audit-2020-01-02.jsonl"
    old_dir.mkdir()

    removed = log.cleanup(TS)
    assert removed == sorted(expired)
    assert sorted(os.listdir(tmp_path)) == sorted(kept + [old_dir.name])
    assert log.cleanup(TS) == []


def test_cleanup_uses_server_date_and_clock(tmp_path) -> None:
    # 服务器日期 2026-09-17（UTC 已是 09-18）；retention=1 时只删 09-15 及更早。
    log = make_log(tmp_path, retention_days=1)
    for d in (15, 16, 17):
        (tmp_path / _day_name(date(2026, 9, d))).write_text("")
    assert log.cleanup() == [_day_name(date(2026, 9, 15))]
    assert sorted(os.listdir(tmp_path)) == [_day_name(date(2026, 9, 16)), _day_name(date(2026, 9, 17))]


def test_cleanup_does_not_delete_symlinks_or_targets(tmp_path) -> None:
    directory = tmp_path / "audit"
    directory.mkdir()
    target = tmp_path / "precious.txt"
    target.write_text("keep")
    link = directory / "audit-2020-01-03.jsonl"
    try_symlink(target, link)
    dangling = directory / "audit-2020-01-04.jsonl"
    try_symlink(tmp_path / "missing", dangling)
    assert make_log(directory).cleanup(TS) == []
    assert os.path.islink(link) and os.path.islink(dangling)
    assert target.read_text() == "keep"


def test_cleanup_missing_directory_returns_empty(tmp_path) -> None:
    assert make_log(tmp_path / "missing").cleanup(TS) == []


@pytest.mark.asyncio
async def test_run_cleanup_runs_immediately_and_stops_on_cancel(tmp_path) -> None:
    old = tmp_path / "audit-2020-01-01.jsonl"
    old.write_text("")
    task = asyncio.create_task(make_log(tmp_path).run_cleanup(interval_s=3600))
    for _ in range(200):
        if not old.exists():
            break
        await asyncio.sleep(0.01)
    assert not old.exists()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
