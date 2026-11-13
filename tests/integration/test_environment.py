"""集成测试环境的冒烟测试（任务 5.2）：样例日志已就位、服务配置可加载、只读快照能发现改动。"""

from __future__ import annotations

import hashlib

import pytest

from tests.integration import sample_logs
from tests.integration.itenv import GameServers, ItEnv, ServiceFiles, diff_snapshots


def _portable(manifest: dict) -> dict:
    """去掉压缩文件的字节级字段（zlib 版本不同时压缩结果可能不同），其余应完全相同。"""
    m = dict(manifest)
    m["entries"] = [{k: v for k, v in e.items() if not (e.get("compressed") and k in ("size", "sha256"))}
                    for e in manifest["entries"]]
    m["log_files"] = [{k: v for k, v in f.items() if not (f["kind"] == "archive" and k == "size")}
                      for f in manifest["log_files"]]
    return m


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["gs", "gs-noionice", "gs-grepspy"])
async def test_samples_on_game_server_match_manifest(name: str, it_env: ItEnv, game_servers: GameServers,
                                                     samples: sample_logs.SampleSet) -> None:
    host = it_env.game_servers[name]
    remote = await game_servers.manifest(host)
    assert _portable(remote) == _portable(samples.manifest())

    snap = await game_servers.snapshot(host)
    for e in remote["entries"]:
        if not e["path"].startswith(sample_logs.SFS2X_ROOT + "/"):
            continue
        got = snap.entries.get(e["path"])
        assert got is not None, e["path"]
        assert got.type == {"file": "f", "dir": "d", "symlink": "l"}[e["type"]], e["path"]
        assert (got.uid, got.gid) == (0, 0), e["path"]
        if e["type"] != "symlink":
            assert got.mode == "777", e["path"]
            assert float(got.mtime) == e["mtime"], e["path"]
        if e["type"] == "file":
            assert (got.size, got.sha256) == (e["size"], e["sha256"]), e["path"]
        if e["type"] == "symlink":
            assert got.link == e["target"], e["path"]
    # 日志目录中除样例之外没有别的条目
    under = lambda p: p.startswith(sample_logs.SFS2X_ROOT + "/")  # noqa: E731
    assert {p for p in snap.entries if under(p)} == {e["path"] for e in remote["entries"] if under(e["path"])}


@pytest.mark.asyncio
async def test_archives_decompress_to_rotated_content(it_env: ItEnv, game_servers: GameServers,
                                                      samples: sample_logs.SampleSet) -> None:
    # 与 ② 相同的读取方式 gzip -dcf：两处都有的时段内容一致，损坏的压缩包以非 0 退出
    both = next(p for p in samples.periods if p.sources == "both")
    corrupt = next(f for f in samples.log_files if f.corrupt)
    script = (
        "{ gzip -dcf -- '%s' | sha256sum; gzip -dcf -- '%s' >/dev/null 2>&1; printf 'corrupt=%%d\\n' \"$?\"; } </dev/null\n"
        % (both.archive.path, corrupt.path)
    )
    out = (await game_servers.run_ok(it_env.gs_ip, script)).decode()
    assert out.split()[0] == hashlib.sha256(both.rotated.content).hexdigest()
    assert "corrupt=1" in out


def test_service_config_loads(service_files: ServiceFiles, it_env: ItEnv) -> None:
    config = service_files.load()
    assert set(config.log_types) == set(sample_logs.LOG_TYPES)
    assert config.zip_dir == sample_logs.ZIP_DIR
    assert config.timezone_label == "GMT-2"
    assert config.admin_db.password == it_env.db_password
    active = [t.sha256 for t in config.active_tokens]
    assert active == [hashlib.sha256(service_files.token.encode()).hexdigest()]
    assert config.ssh.known_hosts == str(service_files.known_hosts)


def test_service_config_inline_password_without_known_hosts(make_service_files, it_env: ItEnv) -> None:
    files: ServiceFiles = make_service_files(known_hosts=None, inline_password=True)
    config = files.load()
    assert files.password_file is None and files.known_hosts is None
    assert config.admin_db.password == it_env.db_password
    assert config.admin_db.password_file is None
    assert config.ssh.known_hosts is None


@pytest.mark.asyncio
async def test_snapshot_detects_changes_and_reset_restores(it_env: ItEnv, game_servers: GameServers,
                                                           samples: sample_logs.SampleSet, restore_samples) -> None:
    host = it_env.gs_ip
    restore_samples(host)
    before = await game_servers.snapshot(host)
    assert diff_snapshots(before, await game_servers.snapshot(host)) == []

    rotated = samples.log_file("normal", "rotated", "2026-09-17-16-04").path
    current = samples.current("normal").path
    await game_servers.run_ok(host, (
        "{ chmod 0644 '%s'; touch -m -d @1 '%s'; : > /root/lvm-it-probe; } </dev/null\n" % (rotated, current)
    ))
    changed = await game_servers.snapshot(host)
    diffs = diff_snapshots(before, changed)
    assert any(d.startswith("修改 " + repr(rotated)) and "mode" in d for d in diffs)
    assert any(d.startswith("修改 " + repr(current)) and "mtime" in d for d in diffs)
    assert any(d.startswith("新增 '/root/lvm-it-probe'") for d in diffs)

    await game_servers.run_ok(host, "rm -f /root/lvm-it-probe </dev/null\n")
    await game_servers.reset_samples(host)
    after = await game_servers.snapshot(host)
    # 恢复后日志目录与快照前完全相同（/root 的 mtime 因新建与删除探测文件而变化）
    assert [d for d in diff_snapshots(before, after) if "'/root'" not in d] == []
