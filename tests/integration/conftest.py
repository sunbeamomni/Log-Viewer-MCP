"""集成测试夹具（任务 5.2）。

集成测试只在 compose 的测试运行容器中运行（README.md）：未设置 LVM_IT=1 时，本目录下的测试全部跳过。

夹具：
- it_env：LVM_IT_* 环境变量（ItEnv）。
- samples：样例日志的内存模型（sample_logs.build()，与游戏服镜像中的内容相同）。
- game_servers：以 root 登录游戏服执行测试脚本、拍只读快照、取 manifest、恢复样例（GameServers）。
- service_files：生成好的服务配置与凭据副本（ServiceFiles）；make_service_files 可按需改 limits、
  known_hosts（"wrong" 用于 HOST_KEY_MISMATCH，None 表示不配置）、inline_password 等。
- restore_samples：测试结束后在指定游戏服上恢复样例（用于会改动样例的测试）。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from tests.integration import sample_logs
from tests.integration.itenv import GameServers, ItEnv, ServiceFiles, it_enabled, run_sync, write_service_files

_HERE = Path(__file__).resolve().parent


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if it_enabled():
        return
    skip = pytest.mark.skip(reason="集成测试只在 compose 的测试运行容器中运行（未设置 LVM_IT=1，见 tests/integration/README.md）")
    for item in items:
        if _HERE in Path(str(item.path)).resolve().parents:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def it_env() -> ItEnv:
    if not it_enabled():
        pytest.skip("未设置 LVM_IT=1")
    return ItEnv.from_environ()


@pytest.fixture(scope="session")
def samples() -> sample_logs.SampleSet:
    return sample_logs.build()


@pytest.fixture(scope="session")
def game_servers(it_env: ItEnv) -> GameServers:
    return GameServers(it_env)


@pytest.fixture
def make_service_files(it_env: ItEnv, tmp_path_factory: pytest.TempPathFactory) -> Callable[..., ServiceFiles]:
    def make(**kwargs) -> ServiceFiles:
        return write_service_files(tmp_path_factory.mktemp("service"), it_env, **kwargs)

    return make


@pytest.fixture
def service_files(make_service_files: Callable[..., ServiceFiles]) -> ServiceFiles:
    return make_service_files()


@pytest.fixture
def restore_samples(game_servers: GameServers) -> Iterator[Callable[[str], None]]:
    """登记要在测试结束后恢复样例的游戏服：``restore_samples(ip)``。"""
    hosts: list[str] = []
    yield hosts.append
    for host in dict.fromkeys(hosts):
        run_sync(game_servers.reset_samples(host))
