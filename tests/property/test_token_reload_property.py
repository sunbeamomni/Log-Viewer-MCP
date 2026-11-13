"""Property 10：token 集合跟随登记文件（需求 6.1、6.6、6.7；任务 14.6）。

Hypothesis 生成对 tokens.json 的操作序列：写入合法的登记内容（任意使用者与吊销状态，同一 token 可登记多次）、
写入非法内容（截断的 JSON、顶层不是数组、缺字段、多字段、revoked 不是布尔值、sha256 格式不符、token_id 重复、
非 UTF-8、空文件）、删除文件。写入与 Token_CLI 相同：同目录 mkstemp 建临时文件，再 os.replace 替换。每个例子在新的
临时目录中进行；第一步之后才构造认证中间件（覆盖构造时的读取），之后每一步之后对 token 池中的每个 token
（已登记未吊销、已吊销、从未登记）调用中间件的 authenticate，与简单模型比较：

- 模型记住"最后一次成功读取的登记内容"，初始为空（构造时传入的集合为空）；
- 合法内容：替换为这次的内容；非法内容：不变；删除：清空（文件不存在时不接受任何 token，之后再写入非法内容
  也仍为空，与 TokenSet 的文档一致）；
- token 被接受当且仅当模型中有以它为摘要且未吊销的记录，返回的使用者为文件顺序中第一条这样的记录的使用者。

文件状态的检测：TokenSet 比较 (st_mtime_ns, st_size, st_ino)。本测试每一步之后都会认证，所以上次记录的状态
总是属于当时仍在目录中的那个文件（或"不存在"）。下一次写入的临时文件是在该文件仍被链接时新建的，inode（Windows
上为文件 ID）必然不同，os.replace 之后状态的三元组一定变化，不依赖 mtime 的精度与内容大小；删除之后记录的是
"不存在"，重新出现的任何文件都与之不同。所以不需要让相邻两步的内容大小不同。只有在"删除后重建"之间没有任何
认证时，旧 inode 才可能被复用并与记录的状态相同，这不在 6.6 的语义之内（变化在下一个请求认证之前生效），本测试
不生成这种情形。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from log_viewer_mcp.server.app import BearerAuthMiddleware
from log_viewer_mcp.server.config import ConfigError, parse_tokens
from log_viewer_mcp.server.tokens import TokenSet

# 写入登记文件的 token（可见 ASCII，不含空白）；NEVER 中的 token 从不写入。
POOL = ("tok-A1_b2c3", "Zz9!~#$%&*", "x", "tok-dup-0000", "QWERTYuiop-1234567890")
NEVER = ("never-registered-1", "tok-A1_b2c")  # 后者是 POOL[0] 的前缀
USERS = ("zhangsan", "lisi", "张三", "wang wu", "ops-team")
CREATED_AT = "2026-09-01T00:00:00-02:00"

INVALID_KINDS = (
    "truncated", "not_array", "missing_key", "extra_key", "bad_revoked",
    "bad_sha256", "duplicate_id", "not_utf8", "empty",
)


async def never_called(scope, receive, send) -> None:  # pragma: no cover - 只调用 authenticate
    raise AssertionError("下游应用不应被调用")


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Entry:
    token: str
    user: str
    revoked: bool


def items_of(entries: list[Entry]) -> list[dict[str, Any]]:
    return [
        {"token_id": f"id-{i}", "user": e.user, "sha256": digest(e.token), "created_at": CREATED_AT,
         "revoked": e.revoked}
        for i, e in enumerate(entries)
    ]


def valid_content(entries: list[Entry], indent: int | None) -> bytes:
    return json.dumps(items_of(entries), ensure_ascii=False, indent=indent).encode("utf-8")


def invalid_content(kind: str, entries: list[Entry], cut: int) -> bytes:
    """按 kind 构造一定无法通过 parse_tokens 的内容。"""
    items = items_of(entries or [Entry(POOL[0], USERS[0], False)])
    if kind == "truncated":
        text = json.dumps(items_of(entries)).encode("utf-8").rstrip()
        return text[: cut % (len(text) - 1)]  # 严格短于去掉结尾 "]" 的长度：缺少右括号
    if kind == "not_array":
        return json.dumps({"tokens": items}).encode("utf-8")
    if kind == "missing_key":
        del items[cut % len(items)]["created_at"]
    elif kind == "extra_key":
        items[cut % len(items)]["note"] = "x"
    elif kind == "bad_revoked":
        items[cut % len(items)]["revoked"] = "false"
    elif kind == "bad_sha256":
        items[cut % len(items)]["sha256"] = items[cut % len(items)]["sha256"][:63]
    elif kind == "duplicate_id":
        items.append(dict(items[cut % len(items)]))
    elif kind == "not_utf8":
        return b"\xff\xfe" + json.dumps(items).encode("utf-8")
    elif kind == "empty":
        return b""
    return json.dumps(items, ensure_ascii=False).encode("utf-8")


def replace_file(path: str, data: bytes) -> None:
    """与 Token_CLI 相同：同目录临时文件，写完后 os.replace。"""
    fd, tmp = tempfile.mkstemp(prefix=".tokens.", suffix=".tmp", dir=os.path.dirname(path))
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.replace(tmp, path)


entries_st = st.lists(
    st.builds(Entry, st.sampled_from(POOL), st.sampled_from(USERS), st.booleans()),
    max_size=6,
)
op_st = st.one_of(
    st.tuples(st.just("valid"), entries_st, st.sampled_from((None, 2))),
    st.tuples(st.just("invalid"), entries_st, st.sampled_from(INVALID_KINDS), st.integers(0, 10_000)),
    st.tuples(st.just("delete")),
)


def expected(model: list[Entry], token: str) -> tuple[str, str] | None:
    for e in model:
        if e.token == token and not e.revoked:
            return e.user, token
    return None


def headers(token: str) -> list[tuple[bytes, bytes]]:
    return [(b"authorization", f"Bearer {token}".encode("ascii"))]


# Feature: log-viewer-mcp, Property 10: token 集合跟随登记文件
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(ops=st.lists(op_st, min_size=1, max_size=12))
def test_token_set_follows_tokens_file(ops: list[tuple]) -> None:
    """**Validates: Requirements 6.1, 6.6, 6.7**"""
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "tokens.json")
        model: list[Entry] = []
        middleware: BearerAuthMiddleware | None = None

        for op in ops:
            if op[0] == "valid":
                _, entries, indent = op
                replace_file(path, valid_content(entries, indent))
                model = list(entries)
            elif op[0] == "invalid":
                _, entries, kind, cut = op
                data = invalid_content(kind, entries, cut)
                with pytest.raises(ConfigError):  # 生成器自检：内容确实非法
                    parse_tokens(data)
                replace_file(path, data)
            else:
                with contextlib.suppress(FileNotFoundError):
                    os.remove(path)
                model = []

            if middleware is None:
                middleware = BearerAuthMiddleware(never_called, TokenSet(path, ()))

            for token in POOL + NEVER:
                assert middleware.authenticate(headers(token)) == expected(model, token), (op[0], token)
