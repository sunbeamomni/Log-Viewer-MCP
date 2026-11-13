"""Property 9：本地路径限定（任务 10.4，需求 4.7）。

把跳板机的 search_logs 摘要交给 ``LocalMCP._handle_result``，/results/ 由 httpx.MockTransport 应答：
服务端"不可信"，对任何 GET 都返回 200 与摘要所说的字节数，对 DELETE 返回 204，并记录每个请求。

摘要中的 Result_Handle 与各 svr_id 的结果文件名混合生成：合法值（按 common/formats.py 的正则生成），
在合法值中插入 "/"、"\\"、".."、盘符、换行、NUL、全角数字等片段的变异，整串的对抗值（空串、"."、".."、
指向临时目录之外的绝对路径与盘符路径、非 ASCII 字符），任意文本与非字符串。字节数大多合法，少数不合法。

每个例子新建一个临时目录 root，Local_Result_Dir 为 root/query_log（有时预先创建），root 下另放一个
outside 目录与若干诱饵文件。运行前后对比 Local_Result_Dir 之外的目录树与诱饵文件内容，应完全相同；
handle、文件名与字节数都合法的 svr_id 下载到 <Local_Result_Dir>/<handle>/<文件名>（realpath 在其内），
其余标 DOWNLOAD_FAILED，且不发请求、不在 Local_Result_Dir 中留下任何文件。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import anyio
import httpx
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

from log_viewer_mcp.common.formats import (
    RESULT_FILE_PATTERN,
    RESULT_HANDLE_PATTERN,
    is_valid_result_file_name,
    is_valid_result_handle,
)
from log_viewer_mcp.local.main import LocalMCP
from tests.unit.test_local import payload
from tests.unit.test_local import settings as local_settings
from tests.unit.test_local_download import FAILED_ENTRY, summary

SETTINGS = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)

# root 下 Local_Result_Dir 之外的诱饵：名字与合法结果文件相同，若被写入或删除即可发现。
DECOYS = {
    "outside/1001_normal.log": b"outside decoy\n",
    "outside/keep.txt": b"keep\n",
    "1001_normal.log": b"root decoy\n",
    "query_log.txt": b"sibling decoy\n",
}

# 插入合法值中的危险片段。
FRAGMENTS = ["/", "\\", "..", "../", "..\\", "/..", "\\..", "C:", "C:\\", "\\\\?\\", "\n", "\r", "\x00", "\t", " ",
             ":", "*", "?", "%2e%2e%2f", "中", "１", "é", "\u200b", "\ufeff"]

valid_handles = st.from_regex(RESULT_HANDLE_PATTERN, fullmatch=True)
valid_file_names = st.from_regex(RESULT_FILE_PATTERN, fullmatch=True)
non_strings = st.sampled_from([None, 0, 123, 1.5, True, [], {}, ["1001_normal.log"]])


def body_of(size: int) -> bytes:
    return (b"0123456789abcdef\n" * (size // 17 + 1))[:size]


@st.composite
def mutated(draw: st.DrawFn, base: st.SearchStrategy[str]) -> str:
    """在合法值的任意位置插入一个危险片段。"""
    value = draw(base)
    pos = draw(st.integers(0, len(value)))
    return value[:pos] + draw(st.sampled_from(FRAGMENTS)) + value[pos:]


def adversarial(root: Path, valid: st.SearchStrategy[str]) -> st.SearchStrategy[Any]:
    """整串的对抗值：空串、相对与绝对路径（含指向 outside 的路径与盘符）、非 ASCII 字符与任意文本。"""
    name = "1001_normal.log"  # 与诱饵同名
    fixed = [
        "", ".", "..", "/", "\\", "C:", "C:\\", "D:x", "\\\\server\\share\\x", "~", "../outside", "..\\outside",
        f"../{name}", f"..\\{name}", f"../outside/{name}", f"..\\..\\{name}", f"/{name}", f"C:{name}",
        str(root), str(root / "outside"), str(root / "outside" / name), str(root / name),
        str(root / "outside" / name).replace("\\", "/"), "１００１_normal.log", "1001_ｎormal.log", "日志_normal.log",
    ]
    return st.one_of(
        st.sampled_from(fixed),
        valid.map(lambda v: v + "\n"),
        valid.map(lambda v: "../" + v),
        valid.map(lambda v: str(root / "outside" / v)),
        st.text(max_size=40),
        non_strings,
    )


@st.composite
def mixed(draw: st.DrawFn, root: Path, valid: st.SearchStrategy[str]) -> Any:
    """约一半取合法值，其余为插入片段的变异或整串的对抗值（先选种类，避免 one_of 展开后合法值占比过低）。"""
    kind = draw(st.sampled_from(["valid", "valid", "mutated", "adversarial"]))
    if kind == "valid":
        return draw(valid)
    if kind == "mutated":
        return draw(mutated(valid))
    return draw(adversarial(root, valid))


def any_handle(root: Path) -> st.SearchStrategy[Any]:
    return mixed(root, valid_handles)


def any_file_name(root: Path) -> st.SearchStrategy[Any]:
    return mixed(root, valid_file_names)


byte_counts = st.sampled_from(["ok"] * 5 + ["bad"]).flatmap(
    lambda kind: st.integers(0, 64) if kind == "ok" else st.sampled_from([-1, 1.0, True, "3", None]))


def ok_entry(svr_id: int, file: Any, size: Any) -> dict:
    return {"svr_id": svr_id, "status": "ok", "error": None, "file": file, "bytes": size, "lines": 1,
            "files_read": ["smartfox.log"], "files_skipped": [], "truncated": "none", "available_range": None}


class UntrustedResults:
    """不可信的 /results/：任何 GET 都返回 200，内容长度取 sizes 中该文件名对应的字节数（没有时 8 字节）；
    DELETE 返回 204。记录每个请求。"""

    def __init__(self, sizes: dict[str, int]) -> None:
        self.sizes = sizes
        self.requests: list[tuple[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        if request.method == "DELETE":
            return httpx.Response(204)
        name = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, content=body_of(self.sizes.get(name, 8)))


def snapshot(root: Path, result_dir: Path) -> dict[str, bytes | None]:
    """root 下 Local_Result_Dir 之外的全部条目（不跟随符号链接）；文件记内容，目录与链接记 None。"""
    out: dict[str, bytes | None] = {}
    skip = os.path.normcase(str(result_dir))
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if os.path.normcase(os.path.join(dirpath, d)) != skip]
        for d in dirnames:
            out[os.path.relpath(os.path.join(dirpath, d), root)] = None
        for f in filenames:
            path = os.path.join(dirpath, f)
            rel = os.path.relpath(path, root)
            out[rel] = None if os.path.islink(path) else Path(path).read_bytes()
    return out


def result_tree(result_dir: Path) -> set[str]:
    if not result_dir.exists():
        return set()
    out: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(result_dir, followlinks=False):
        for name in dirnames + filenames:
            out.add(os.path.relpath(os.path.join(dirpath, name), result_dir))
    return out


def run(local: LocalMCP, result: Any) -> Any:
    async def go() -> Any:
        return await local._handle_result("search_logs", result, anyio.current_time() + 10.0)

    return asyncio.run(go())


# Feature: log-viewer-mcp, Property 9: 本地路径限定
@SETTINGS
@given(data=st.data(), precreate=st.booleans(), with_failed=st.booleans())
def test_local_writes_only_validated_paths_inside_result_dir(tmp_path_factory, data, precreate, with_failed) -> None:
    """**Validates: Requirements 4.7**"""
    root = tmp_path_factory.mktemp("p9")
    for rel, content in DECOYS.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    result_dir = root / "query_log"
    if precreate:
        result_dir.mkdir()

    handle = data.draw(any_handle(root), label="handle")
    files = data.draw(st.lists(st.tuples(any_file_name(root), byte_counts), min_size=1, max_size=4,
                               unique_by=lambda t: repr(t[0])), label="files")
    servers: list[dict] = [ok_entry(1001 + i, f, size) for i, (f, size) in enumerate(files)]
    if with_failed:
        servers.insert(data.draw(st.integers(0, len(servers)), label="failed_pos"), FAILED_ENTRY)

    fake = UntrustedResults({f: size for f, size in files if type(f) is str and type(size) is int and size >= 0})
    local = LocalMCP(local_settings(root), transport=httpx.MockTransport(fake))
    before_outside = snapshot(root, result_dir)
    before_inside = result_tree(result_dir)

    out = payload(run(local, summary(servers, handle=handle)))["servers"]

    # Local_Result_Dir 之外没有任何新增、删除或改动。
    assert snapshot(root, result_dir) == before_outside

    handle_ok = is_valid_result_handle(handle)
    base = os.path.realpath(result_dir)
    expected_files: set[str] = set()
    expected_gets: list[tuple[str, str]] = []
    assert len(out) == len(servers)
    for entry, sent in zip(out, servers):
        assert entry["svr_id"] == sent["svr_id"] and "file" not in entry
        if sent["status"] != "ok":
            assert entry["status"] == "failed" and entry["error"]["code"] == "SSH_FAILED"
            assert entry["local_path"] is None
            continue
        size = sent["bytes"]
        valid = handle_ok and is_valid_result_file_name(sent["file"]) and type(size) is int and size >= 0
        if valid:
            assert entry["status"] == "ok", entry
            local_path = entry["local_path"]
            assert local_path == os.path.join(str(result_dir), handle, sent["file"])
            real = os.path.realpath(local_path)
            assert os.path.normcase(os.path.commonpath([base, real])) == os.path.normcase(base) and real != base
            assert Path(local_path).read_bytes() == body_of(size)
            expected_files.add(os.path.join(handle, sent["file"]))
            expected_gets.append(("GET", f"/results/{handle}/{sent['file']}"))
        else:
            assert entry["status"] == "failed", entry
            assert entry["error"]["code"] == "DOWNLOAD_FAILED"
            assert entry["local_path"] is None
            assert entry["bytes"] == 0 and entry["lines"] == 0

    event("handle 合法" if handle_ok else "handle 不合法")
    event("有合法文件被下载" if expected_files else "没有文件被下载")
    if handle_ok and len(expected_files) < len(files):
        event("handle 合法且有文件被拒")

    # Local_Result_Dir 之内只多出合法的 <handle>/<文件名>（没有 .part 残留）；不合法的值不触碰文件系统。
    created = result_tree(result_dir) - before_inside
    if expected_files:
        assert created == expected_files | {handle}
    else:
        assert created == set()
    if not precreate and not expected_files:
        assert not result_dir.exists()

    # 只为合法的文件发 GET；handle 不合法时不发任何请求，否则最后 DELETE 该 handle。
    if handle_ok:
        assert fake.requests == expected_gets + [("DELETE", f"/results/{handle}")]
    else:
        assert fake.requests == []
