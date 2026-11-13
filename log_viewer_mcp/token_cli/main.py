"""Token_CLI：生成、列出与吊销 Access_Token（需求 6.2；design.md「Token_CLI」）。

tokens.json 只保存 token 的 SHA-256 摘要。写入时在同一目录建临时文件（权限 0600），
写完 fsync 后用 os.replace 替换，读取方不会看到写了一半的文件。替换已有文件时沿用
原文件的属主与属组。

读写都经 server/config.py 的 parse_tokens 校验，写出的内容在落盘前再解析一次，保证
Log_MCP_Server 能接受。Log_MCP_Server 在 tokens.json 变化后的下一个请求时重新读取，改动立即生效
（需求 6.6）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import stat
import sys
import tempfile
import unicodedata
from collections.abc import Sequence
from dataclasses import asdict, replace
from datetime import datetime
from typing import TextIO

from log_viewer_mcp.server.config import (
    ConfigError,
    TokenRecord,
    parse_tokens,
    read_regular_file,
)

DEFAULT_TOKENS_FILE = "/etc/log-viewer-mcp/tokens.json"
EFFECT_HINT = "改动立即生效：Log_MCP_Server 在收到下一个请求时重新读取登记文件。"


class CliError(Exception):
    """命令执行失败；说明写到标准错误。"""


# ---------------------------------------------------------------------------
# tokens.json 的读写
# ---------------------------------------------------------------------------


def load_tokens(path: str, *, missing_ok: bool) -> list[TokenRecord]:
    """读取并校验 tokens.json；文件不存在且 missing_ok 时返回空列表。"""
    if missing_ok and not os.path.lexists(path):
        return []
    try:
        return list(parse_tokens(read_regular_file(path, "token 登记文件"), path))
    except ConfigError as e:
        raise CliError(str(e)) from None


def serialize_tokens(records: Sequence[TokenRecord]) -> bytes:
    """序列化为 tokens.json 的内容，并用 parse_tokens 自检。"""
    data = (
        json.dumps([asdict(r) for r in records], ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    try:
        parse_tokens(data)
    except ConfigError as e:
        raise CliError(str(e)) from None
    return data


def save_tokens(path: str, records: Sequence[TokenRecord]) -> None:
    """原子写入：同目录临时文件（0600）→ fsync → os.replace。"""
    data = serialize_tokens(records)
    directory = os.path.dirname(os.path.abspath(path))
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        st = None
    except OSError as e:
        raise CliError(f"token 登记文件 {path} 不可访问：{e.strerror}") from None
    if st is not None and not stat.S_ISREG(st.st_mode):
        # 符号链接也拒绝：os.replace 会替换链接本身而不是它指向的文件。
        raise CliError(f"token 登记文件 {path} 不是普通文件")

    try:
        fd, tmp = tempfile.mkstemp(prefix=".tokens.", suffix=".tmp", dir=directory)
    except OSError as e:
        raise CliError(f"无法在 {directory} 中创建临时文件：{e.strerror}") from None
    try:
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            if st is not None and hasattr(os, "fchown"):
                fs = os.fstat(fd)
                if (fs.st_uid, fs.st_gid) != (st.st_uid, st.st_gid):
                    os.fchown(fd, st.st_uid, st.st_gid)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise CliError(f"写入 token 登记文件 {path} 失败：{e.strerror}") from None
    _fsync_dir(directory)


def _fsync_dir(directory: str) -> None:
    """POSIX 上 fsync 目录，使改名持久化；Windows 不支持打开目录，跳过。"""
    if os.name != "posix":
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def hash_token(token: str) -> str:
    """token 的 SHA-256 摘要（64 位小写十六进制），与服务端认证的算法一致。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------


def cmd_create(path: str, user: str, out: TextIO) -> int:
    records = load_tokens(path, missing_ok=True)
    used = {r.token_id for r in records}
    token_id = secrets.token_hex(4)
    while token_id in used:
        token_id = secrets.token_hex(4)
    token = secrets.token_urlsafe(32)
    record = TokenRecord(
        token_id=token_id,
        user=user,
        sha256=hash_token(token),
        created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        revoked=False,
    )
    try:
        serialize_tokens([record])  # 与服务端同一套规则校验使用者名称
    except CliError:
        raise CliError("使用者名称必须是不含控制字符的非空字符串") from None
    save_tokens(path, [*records, record])
    out.write(
        f"已为 {user} 创建 token（token_id {token_id}）。\n"
        f"token：{token}\n"
        "明文只显示这一次，登记文件中只保存摘要，请立即交给使用者。\n"
        f"{EFFECT_HINT}\n"
    )
    return 0


def cmd_list(path: str, out: TextIO) -> int:
    records = load_tokens(path, missing_ok=False)
    if not records:
        out.write("没有登记任何 token。\n")
        return 0
    header = ("token_id", "user", "created_at", "revoked")
    rows = [header] + [
        (r.token_id, r.user, r.created_at, "yes" if r.revoked else "no") for r in records
    ]
    widths = [max(_width(row[i]) for row in rows) for i in range(len(header))]
    for row in rows:
        cells = [cell + " " * (w - _width(cell)) for cell, w in zip(row, widths)]
        out.write("  ".join(cells).rstrip() + "\n")
    return 0


def cmd_revoke(path: str, token_id: str, out: TextIO) -> int:
    records = load_tokens(path, missing_ok=False)
    i = next((n for n, rec in enumerate(records) if rec.token_id == token_id), None)
    if i is None:
        raise CliError(f"找不到 token_id {token_id}")
    r = records[i]
    if r.revoked:
        out.write(f"token_id {token_id}（{r.user}）已是吊销状态，未做改动。\n")
        return 0
    records[i] = replace(r, revoked=True)
    save_tokens(path, records)
    out.write(f"已吊销 token_id {token_id}（{r.user}）。\n{EFFECT_HINT}\n")
    return 0


def _width(text: str) -> int:
    """终端显示宽度：东亚宽字符记 2 列。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="log-viewer-mcp-token",
        description="管理 Log_MCP_Server 的访问 token。" + EFFECT_HINT,
    )
    parser.add_argument(
        "-f",
        "--tokens-file",
        default=DEFAULT_TOKENS_FILE,
        help=f"token 登记文件路径（默认 {DEFAULT_TOKENS_FILE}）",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="{create,list,revoke}")
    p_create = sub.add_parser("create", help="为使用者生成 token，明文只打印一次")
    p_create.add_argument("user", help="使用者名称")
    sub.add_parser("list", help="列出 token_id、使用者、创建时间与是否已吊销")
    p_revoke = sub.add_parser("revoke", help="吊销指定的 token_id")
    p_revoke.add_argument("token_id")
    return parser


def main(argv: list[str] | None = None) -> int:
    """`log-viewer-mcp-token`（或 `python -m log_viewer_mcp.token_cli`）的入口；成功返回 0，失败返回 1，用法错误返回 2。"""
    args = build_parser().parse_args(argv)
    out = sys.stdout
    try:
        if args.command == "create":
            return cmd_create(args.tokens_file, args.user, out)
        if args.command == "list":
            return cmd_list(args.tokens_file, out)
        return cmd_revoke(args.tokens_file, args.token_id, out)
    except CliError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1
