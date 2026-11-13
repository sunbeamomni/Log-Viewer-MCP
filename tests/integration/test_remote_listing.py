"""① 列目录与探测的集成测试（任务 6.2）：在 gs 与 gs-noionice 上执行固定命令行与 ① 脚本。"""

from __future__ import annotations

import posixpath

import pytest

from log_viewer_mcp.common.errors import ErrorCode, LogViewerError
from log_viewer_mcp.server import remote
from log_viewer_mcp.server.remote import SshConnector, build_listing_script, is_safe_file_name, run_listing
from tests.integration import sample_logs
from tests.integration.itenv import ItEnv

SECONDS = 30


def expected_files(samples: sample_logs.SampleSet, directory: str) -> dict[str, int]:
    """directory 下直接位于其中、名称安全的普通文件：名称 → 大小（符号链接与子目录不算）。"""
    result = {}
    for e in samples.entries:
        if e.type == "file" and posixpath.dirname(e.path) == directory:
            name = posixpath.basename(e.path)
            if is_safe_file_name(name):
                result[name] = len(e.data)
    return result


def unsafe_count(samples: sample_logs.SampleSet, directory: str) -> int:
    return sum(1 for e in samples.entries if e.type == "file" and posixpath.dirname(e.path) == directory
               and not is_safe_file_name(posixpath.basename(e.path)))


async def listing_for(it_env: ItEnv, service_files, host: str, plain_dir: str, zip_dir: str = sample_logs.ZIP_DIR):
    connector = SshConnector(service_files.load().ssh)
    async with await connector.connect(host) as conn:
        return await run_listing(conn, build_listing_script(plain_dir, zip_dir), seconds=SECONDS)


@pytest.mark.asyncio
@pytest.mark.parametrize("log_type", sorted(sample_logs.LOG_TYPES))
async def test_listing_on_gs_matches_samples(it_env: ItEnv, service_files, samples, game_servers, log_type) -> None:
    plain_dir = sample_logs.LOG_TYPES[log_type].dir
    async with game_servers.assert_unchanged(it_env.gs_ip):
        listing = await listing_for(it_env, service_files, it_env.gs_ip, plain_dir)
    assert listing.ionice is True
    # 只列直接位于目录下的普通文件：符号链接、子目录及其中的文件都不出现；名称不安全的被丢弃
    assert {e.name: e.size for e in listing.plain} == expected_files(samples, plain_dir)
    assert {e.name: e.size for e in listing.archives} == expected_files(samples, sample_logs.ZIP_DIR)
    assert listing.ignored == unsafe_count(samples, plain_dir) + unsafe_count(samples, sample_logs.ZIP_DIR)
    assert len({e.inode for e in listing.plain}) == len(listing.plain)


@pytest.mark.asyncio
async def test_listing_excludes_symlinks_subdirs_and_weird_names(it_env: ItEnv, service_files, samples) -> None:
    logs = sample_logs.LOG_TYPES["normal"].dir
    listing = await listing_for(it_env, service_files, it_env.gs_ip, logs)
    names = {e.name for e in listing.plain} | {e.name for e in listing.archives}
    symlinks = {posixpath.basename(e.path) for e in samples.entries if e.type == "symlink"}
    subdirs = {posixpath.basename(e.path) for e in samples.entries if e.type == "dir"}
    assert "smartfox.log.2026-09-16-13" in symlinks and "smartfox.log.2026-09-17-16-05" in subdirs
    # 名称与符号链接或子目录相同的条目只可能来自另一个目录中的普通文件
    plain_names = {e.name for e in listing.plain}
    assert not plain_names & {posixpath.basename(e.path) for e in samples.entries
                              if e.type in ("symlink", "dir") and posixpath.dirname(e.path) == logs}
    assert "boot" not in plain_names and "gc.log" in plain_names and "smartfox.log" in plain_names
    assert "-rf" in plain_names and "--help" in plain_names  # 字符安全，交给 select.py 忽略
    assert all(is_safe_file_name(n) for n in names)
    assert all(sample_logs.IGNORED_MARKER not in n for n in names)


@pytest.mark.asyncio
async def test_current_inode_matches_remote_stat(it_env: ItEnv, service_files, game_servers) -> None:
    logs = sample_logs.LOG_TYPES["normal"].dir
    listing = await listing_for(it_env, service_files, it_env.gs_ip, logs)
    current = next(e for e in listing.plain if e.name == "smartfox.log")
    out = await game_servers.run_ok(it_env.gs_ip, f"stat -c '%i %s' -- {logs}/smartfox.log </dev/null\n")
    assert out.decode().split() == [str(current.inode), str(current.size)]


@pytest.mark.asyncio
async def test_noionice_server_reports_no_ionice(it_env: ItEnv, service_files, samples) -> None:
    logs = sample_logs.LOG_TYPES["normal"].dir
    listing = await listing_for(it_env, service_files, it_env.gs_noionice_ip, logs)
    assert listing.ionice is False
    assert {e.name: e.size for e in listing.plain} == expected_files(samples, logs)


@pytest.mark.asyncio
async def test_nonexistent_plain_dir_is_log_dir_unavailable(it_env: ItEnv, service_files) -> None:
    with pytest.raises(LogViewerError) as ei:
        await listing_for(it_env, service_files, it_env.gs_ip, "/usr/local/ds/SFS2X/nonexistent")
    assert ei.value.code is ErrorCode.LOG_DIR_UNAVAILABLE


@pytest.mark.asyncio
@pytest.mark.parametrize("zip_dir", ["/usr/local/ds/SFS2X/nonexistent", None])
async def test_missing_or_unconfigured_zip_dir_has_no_archives(it_env: ItEnv, service_files, samples, zip_dir) -> None:
    # 游戏服上没有压缩目录，或配置中没有 zip_dir：只列非压缩目录，不是 LOG_DIR_UNAVAILABLE
    logs = sample_logs.LOG_TYPES["normal"].dir
    listing = await listing_for(it_env, service_files, it_env.gs_ip, logs, zip_dir)
    assert listing.archives == ()
    assert {e.name: e.size for e in listing.plain} == expected_files(samples, logs)


@pytest.mark.asyncio
async def test_empty_dir_is_ok(it_env: ItEnv, service_files) -> None:
    # 存在但没有普通文件的目录（SFS2X 根目录下只有子目录）：find 返回 0，结果为空，不是 LOG_DIR_UNAVAILABLE
    listing = await listing_for(it_env, service_files, it_env.gs_ip, sample_logs.SFS2X_ROOT)
    assert listing.plain == ()


@pytest.mark.asyncio
async def test_fixed_command_hides_login_shell_environment(it_env: ItEnv, service_files) -> None:
    """登录 Shell 设置的 BASH_ENV、GREP_OPTIONS 等对固定命令行无效，~/.bashrc 的输出在 NUL 之前被丢弃。"""
    connector = SshConnector(service_files.load().ssh)
    script = "{ printf '\\0'; printf '%s|%s|%s\\0' \"${BASH_ENV-unset}\" \"${GREP_OPTIONS-unset}\" \"$(nice)\"; } </dev/null\n"
    async with await connector.connect(it_env.gs_ip) as conn:
        proc = await conn.start(remote.build_command(SECONDS, ionice=True), script)
        out = b"".join([chunk async for chunk in proc])
        ex = await proc.wait()
    head, sep, body = out.partition(b"\0")
    assert sep and b"Welcome" in head
    assert body == b"unset|unset|19\0" and b"BASH_ENV_SOURCED_MARKER" not in out
    assert ex.exit_status == 0
