"""② 查询脚本的集成冒烟测试（任务 6.3）：在 gs 上执行生成的脚本，检查原始输出与只读快照。

完整的分帧解析与查询执行见 6.4、6.6；这里只按行检查帧的顺序、各文件读取的分支与内容。
"""

from __future__ import annotations

import re

import pytest

from log_viewer_mcp.server.remote import SshConnector, build_listing_script, build_query_script, run_listing
from log_viewer_mcp.server.select import SelectedFile, select_files
from tests.integration import sample_logs
from tests.integration.itenv import ItEnv

SECONDS = 30
LOGS = sample_logs.LOG_TYPES["normal"].dir
MARK = sample_logs.MARKER_PREFIX


async def run_query(it_env: ItEnv, service_files, files, **kwargs):
    """在 gs 上依次执行 ① 与 ②；返回 (QueryScript, READY 行之后按行切分的输出, 退出码)。files 为 None 时按 ① 的结果选取。"""
    connector = SshConnector(service_files.load().ssh)
    async with await connector.connect(it_env.gs_ip) as conn:
        listing = await run_listing(conn, build_listing_script(LOGS, sample_logs.ZIP_DIR), seconds=SECONDS)
        if files is None:
            files = select_files(
                listing.plain, listing.archives,
                start=sample_logs.server_time(2026, 9, 17, 16, 0), end=sample_logs.server_time(2026, 9, 17, 16, 12),
                now=sample_logs.GENERATED_AT, max_scan_bytes=1 << 40, suffix="", tz=sample_logs.SERVER_TZ,
            ).files
        q = build_query_script(files, plain_dir=LOGS, zip_dir=sample_logs.ZIP_DIR,
                               seconds=SECONDS, ionice=listing.ionice, **kwargs)
        proc = await conn.start(q.command, q.script)
        out = b"".join([chunk async for chunk in proc])
        ex = await proc.wait()
    # ~/.bashrc 的输出在 READY 行之前，丢弃
    ready = re.search(rb"(?m)^" + q.boundary.encode() + rb" READY ([0-9]+)\n", out)
    assert ready is not None
    assert int(ready.group(1)) > 1
    return q, out[ready.end():].splitlines(keepends=True), ex


def frames(q, lines: list[bytes]) -> list[tuple[int, bytes, str]]:
    """按 FILE、END、DONE 帧切分：[(序号, 内容, 结束状态)]，并检查帧的顺序。"""
    b = q.boundary.encode()
    result = []
    i = 0
    for index in range(len(q.files)):
        assert lines[i] == b + b" FILE %d\n" % index
        i += 1
        body = b""
        while not lines[i].startswith(b + b" "):
            body += lines[i]
            i += 1
        m = re.fullmatch(re.escape(b) + rb" END ([0-9]+) ([A-Z0-9 ]+)\n", lines[i])
        assert m is not None and int(m.group(1)) == index
        result.append((index, body, m.group(2).decode()))
        i += 1
    assert lines[i:] == [b + b" DONE\n"]
    return result


def markers(content: bytes) -> bytes:
    return b"".join(line for line in content.splitlines(keepends=True) if MARK.encode() in line)


@pytest.mark.asyncio
async def test_query_script_reads_selected_files(it_env: ItEnv, service_files, samples, game_servers) -> None:
    async with game_servers.assert_unchanged(it_env.gs_ip):
        q, lines, ex = await run_query(it_env, service_files, None, include_patterns=[MARK])
    assert ex.exit_status == 0
    assert "ionice -c 3" in q.command
    got = frames(q, lines)
    # 16:00–16:12（终点多取 1 分钟）：16-00 至 16-09 各分钟文件与 Current
    expected_ts = ["2026-09-17-16-%02d" % m for m in (0, 1, 2, 3, 4, 6, 7, 8, 9)]
    assert [f.source.ts for f in q.files] == expected_ts + [None]
    assert q.files[-1].is_current
    for f, (_, body, status) in zip(q.files, got):
        if f.is_current:
            content, branch = samples.current("normal").content, "F"
        else:
            period = samples.period("normal", f.source.ts)
            content, branch = period.content, ("F" if period.rotated else "Z")
        assert status == f"{branch} 0 0", f.name
        assert body == markers(content) and body.count(b"\n") == 1, f.name


@pytest.mark.asyncio
async def test_query_script_fallback_symlink_and_corrupt_archive(it_env: ItEnv, service_files, samples,
                                                                 game_servers) -> None:
    files = (
        # 损坏的压缩包：gzip 非 0
        SelectedFile(plain=None, archive="smartfox.log.2026-09-16-09.tar.gz", inode=None,
                     ts="2026-09-16-09", start=0, end=1, size=0),
        # 非压缩目录中同名的是符号链接，没有压缩文件：N
        SelectedFile(plain="smartfox.log.2026-09-16-13", archive=None, inode=None,
                     ts="2026-09-16-13", start=0, end=1, size=0),
        # 两处都有：读非压缩文件；排除段生效（排除标记行后什么都不剩）
        SelectedFile(plain="smartfox.log.2026-09-16-10", archive="smartfox.log.2026-09-16-10.tar.gz", inode=None,
                     ts="2026-09-16-10", start=0, end=1, size=0),
        # 非压缩目录中同名的是子目录，ziplogs 中同名的是指向目录之外的符号链接：N
        SelectedFile(plain=f"smartfox.log.{sample_logs.GAP_MINUTE}",
                     archive=f"smartfox.log.{sample_logs.GAP_MINUTE}.tar.gz", inode=None,
                     ts=sample_logs.GAP_MINUTE, start=0, end=1, size=0),
    )
    async with game_servers.assert_unchanged(it_env.gs_ip):
        q, lines, ex = await run_query(it_env, service_files, files, include_patterns=[MARK],
                                       exclude_patterns=["SAMPLE_MARK"], ignore_case=True)
    assert ex.exit_status == 0
    got = frames(q, lines)
    corrupt_status = got[0][2].split()
    assert corrupt_status[0] == "Z" and corrupt_status[1] != "0" and len(corrupt_status) == 4
    assert sample_logs.IGNORED_MARKER.encode() not in got[1][1]
    assert got[1] == (1, b"", "N")
    assert got[2][2].split()[0] == "F" and got[2][1] == b""
    assert got[3] == (3, b"", "N")
