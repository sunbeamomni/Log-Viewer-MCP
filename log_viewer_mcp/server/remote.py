"""游戏服上的远端执行（design.md「Remote Execution」）。

本节（任务 6.1）是 SSH 连接层，后续的固定命令行与 ① 脚本（6.2）、② 脚本（6.3）、
分帧解析（6.4）与查询执行和终止（6.6）都建在它上面：

- ``SshConnector``：启动时导入私钥（RSA、ECDSA 或 Ed25519，OpenSSH 或 PEM 格式，无口令）与配置了的
  known_hosts（失败抛出 ConfigError；文件的属主与权限位不作要求），``connect(ip_inner)`` 建立 SSH 连接。
  配置了 known_hosts 时严格校验，主机公钥未登记或不符时不登录，抛出 LogViewerError(HOST_KEY_MISMATCH)；
  未配置时给 asyncssh 传 known_hosts=None，不校验主机公钥。其余连接或认证失败抛出 LogViewerError(SSH_FAILED)。
  不用认证代理、不读 ssh_config 与 ~/.ssh 下的任何文件、不请求伪终端与任何转发（8.8）。
- ``RemoteConnection.start(command, script)``：在同一连接上新开一个会话执行固定命令行，
  把脚本写入标准输入后随即关闭（8.3）；返回 ``RemoteProcess``。
- ``RemoteProcess``：``read()`` 以流的方式读标准输出（不整段缓存）；标准错误由后台任务单独读取，
  只保留开头 STDERR_LOG_LIMIT 字节，结束时写入运行日志（10.3）；``wait()`` 返回退出码或信号。

ip_inner 只作为连接参数交给 asyncssh，不经过任何 Shell。私钥只在内存中交给 asyncssh，
不写入任何日志；对外的错误只有错误码与中文说明，原始错误文本只写运行日志（10.3）。

任务 6.2 的固定命令行与 ① 列目录和探测：

- ``build_command(seconds, ionice=…)``：``env -i PATH=/usr/bin:/bin LC_ALL=C timeout -s KILL <秒>
  nice -n 19 [ionice -c 3] bash --noprofile --norc -s``，不含任何请求值（8.3、9.1、9.4）；
  ``remote_seconds(remaining)`` 算出 <秒>（剩余时长向上取整再加 5）。
- ``build_listing_script(plain_dir, zip_dir)``：① 脚本，目录来自配置，再次校验格式后 shlex.quote（8.2）；
  zip_dir 可为 None（未配置压缩目录），配置了但游戏服上不存在时当作没有压缩文件。
- ``ListingParser``：按块解析 ① 的 NUL 记录（纯逻辑），``finish`` 映射 REMOTE_TOOL_UNAVAILABLE、
  LOG_DIR_UNAVAILABLE 与 SSH_FAILED；``run_listing`` 在连接上执行 ① 并返回 ``Listing``。

任务 6.3 的 ② 查询脚本：

- ``build_query_script(files, …)``：按 design.md ② 的模板生成脚本与固定命令行（按 ① 的探测结果
  决定是否加 ionice），返回 ``QueryScript``（脚本、边界串 B、各序号的 ``QueryFile`` 与命令行）。
  动态值只有：模式（再次校验后单引号转义）、配置中的目录与 ① 列出并经选取的文件名（再次校验后
  单引号转义）、按 %d 格式化的整数（含 Current 的 inode）与只含 [0-9a-f] 的边界串（8.5）。

任务 6.4 的分帧解析：

- ``FrameParser(boundary, file_count)``：按块解析 ② 的输出（纯逻辑），``feed`` 返回事件
  （FrameReady、FileStart、FileData、FileEnd、FrameDone）；READY 之前的内容丢弃，之后只认行首的
  "B 加空格加帧关键字" 为帧行，其余原样归当前文件（8.7）；帧序号或顺序不符时抛出
  LogViewerError(INTERNAL_ERROR)。``parse_file_status`` 把 END 的结束状态解析为 ``FileStatus``（2.7）。

任务 6.6 的查询执行与终止：

- ``run_query(conn, query, sink, deadline=…)``：执行 ②，按分帧结果调用 ``ResultSink``（begin_file、write、
  end_file、interrupt_file），返回 ``QueryOutcome``（各序号的 ``FileState``、是否收到 DONE、是否超时、
  进程组号与退出情况）。
- 超时、取消或停机：``terminate_query`` 在同一连接上新开会话执行 ``build_kill_command(pgid)``
  （``kill -KILL -- -<pgid>``，限时 KILL_TIMEOUT_S），再关闭连接；还没收到 READY 时只关闭连接（9.3、10.5、10.6）。
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import secrets
import shlex
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

import asyncssh

from log_viewer_mcp.common.errors import ErrorCode, LogViewerError
from log_viewer_mcp.server.config import (
    REMOTE_DIR_PATTERN,
    Config,
    ConfigError,
    SshConfig,
    read_regular_file,
)
from log_viewer_mcp.server.db import is_valid_ipv4

__all__ = [
    "KILL_TIMEOUT_S",
    "MAX_REMOTE_SECONDS",
    "READ_CHUNK",
    "REMOTE_TIMEOUT_MARGIN_S",
    "STDERR_LOG_LIMIT",
    "ArchiveEntry",
    "FileData",
    "FileEnd",
    "FileOutcome",
    "FileStart",
    "FileState",
    "FileStatus",
    "FrameDone",
    "FrameEvent",
    "FrameParser",
    "FrameReady",
    "Listing",
    "ListingParser",
    "PlainEntry",
    "QueryFile",
    "QueryOutcome",
    "QueryScript",
    "RemoteConnection",
    "RemoteExit",
    "RemoteProcess",
    "ResultSink",
    "SshConnector",
    "build_command",
    "build_kill_command",
    "build_listing_script",
    "build_query_script",
    "check_ssh_credentials",
    "is_fixed_command",
    "is_safe_file_name",
    "parse_file_status",
    "remote_seconds",
    "run_listing",
    "run_query",
    "terminate_query",
]

logger = logging.getLogger(__name__)

# 每次从标准输出读取的最大字节数。
READ_CHUNK = 64 * 1024

# 标准错误写入运行日志时保留的字节数，其余只计数。
STDERR_LOG_LIMIT = 4096

# 关闭连接时等待对端确认的上限（秒），超过即直接断开。
CLOSE_TIMEOUT_S = 5

# 固定命令行允许的字符：命令行由程序写定（design.md「固定命令行」），只含这些字符；
# 引号、分号、$、反引号、重定向与换行等 Shell 元字符一律不允许（防御：请求值不可能混入）。
_FIXED_COMMAND_PATTERN = re.compile(r"[A-Za-z0-9 /:=._-]{1,1024}")


def is_fixed_command(command: Any) -> bool:
    """command 是否只含固定命令行允许的字符。"""
    return type(command) is str and _FIXED_COMMAND_PATTERN.fullmatch(command) is not None


def _describe(e: BaseException) -> str:
    """运行日志用的异常说明：类型与文本（asyncssh 与 OSError 的文本不含凭据）。"""
    text = str(e)
    return f"{type(e).__name__}: {text}" if text else type(e).__name__


@dataclass(frozen=True)
class RemoteExit:
    """远端会话的结束情况。

    exit_status：远端程序的退出码；被信号终止或没有收到退出码（例如连接中断）时为 None。
    exit_signal：终止信号名（例如 "KILL"），否则为 None。
    stderr_bytes：标准错误的总字节数（内容只写运行日志）。
    """

    exit_status: int | None
    exit_signal: str | None
    stderr_bytes: int


class SshConnector:
    """按配置建立到游戏服的 SSH 连接；私钥与配置了的 known_hosts 在构造时读入内存（8.8、10.2）。

    私钥：asyncssh 能导入的 RSA、ECDSA 或 Ed25519 私钥，OpenSSH 或 PEM 格式，不能有口令。
    known_hosts：配置了就导入为 SSHKnownHosts 交给 asyncssh 严格校验；未配置时为 None，
    asyncssh 2.24.1 收到 known_hosts=None 时不建立受信主机公钥集合，也就不校验主机公钥。
    读取这些文件时不检查属主与权限位。
    """

    def __init__(self, ssh: SshConfig) -> None:
        self._user = ssh.user
        self._port = ssh.port
        self._connect_timeout_s = ssh.connect_timeout_s
        try:
            self._client_key = asyncssh.import_private_key(ssh.private_key)
        except (ValueError, asyncssh.Error) as e:
            # 不带异常文本：它可能引用私钥内容
            raise ConfigError(
                f"SSH 私钥文件 {ssh.key_file} 无法导入（{type(e).__name__}）：支持 RSA、ECDSA、Ed25519 私钥，"
                "OpenSSH 或 PEM 格式，不能设口令"
            ) from None
        self._known_hosts: asyncssh.SSHKnownHosts | None = None
        if ssh.known_hosts is None:
            # 未配置 known_hosts：不校验主机公钥（云内网，不考虑中间人）
            return
        data = read_regular_file(ssh.known_hosts, "known_hosts 文件")
        try:
            known_hosts = asyncssh.import_known_hosts(data.decode("utf-8"))
        except (ValueError, asyncssh.Error) as e:
            raise ConfigError(f"known_hosts 文件 {ssh.known_hosts} 格式不正确：{_describe(e)}") from None
        # asyncssh 2.24.1 的 SSHKnownHosts.load 会跳过无法解析的公钥行，只含这类行（或只有注释）的文件
        # 导入后为空，所有主机都会被拒绝；按 10.2 视为无法解析，拒绝启动。条目存放在这两个内部属性中。
        if not known_hosts._exact_entries and not known_hosts._pattern_entries:
            raise ConfigError(f"known_hosts 文件 {ssh.known_hosts} 中没有可用的主机公钥")
        self._known_hosts = known_hosts

    def _options(self) -> dict[str, Any]:
        """交给 asyncssh.connect 的选项（参数名按 asyncssh 2.24.1）。"""
        return {
            "port": self._port,
            "username": self._user,
            # 不读取 ~/.ssh/config 等 OpenSSH 配置文件
            "config": None,
            # 配置了 known_hosts 时按它严格校验主机公钥；未配置时为 None（不校验）
            "known_hosts": self._known_hosts,
            "host_key_alias": None,
            # 只用配置的私钥做公钥认证：不用认证代理、PKCS#11、主机认证、GSSAPI、密码与键盘交互
            "client_keys": [self._client_key],
            "agent_path": None,
            "agent_identities": None,
            "pkcs11_provider": None,
            "client_host_keysign": False,
            "client_host_keys": None,
            "password": None,
            "preferred_auth": "publickey",
            "host_based_auth": False,
            "kbdint_auth": False,
            "password_auth": False,
            "gss_host": None,
            "gss_auth": False,
            "gss_kex": False,
            # 不读取 ~/.ssh/ca-bundle.crt 与 ~/.ssh/crt
            "x509_trusted_certs": None,
            "x509_trusted_cert_paths": [],
            "proxy_command": None,
            # 不转发认证代理与 X11，不请求伪终端，不发送本地环境变量
            "agent_forwarding": False,
            "x11_forwarding": False,
            "request_pty": False,
            "term_type": None,
            "env": None,
            "send_env": None,
            # 标准输入输出按原始字节传输
            "encoding": None,
            "connect_timeout": self._connect_timeout_s,
        }

    async def connect(self, ip_inner: str) -> RemoteConnection:
        """连接到 ip_inner（须为合法的点分十进制 IPv4，只作连接参数）。

        主机公钥未登记或不符：LogViewerError(HOST_KEY_MISMATCH)；其余连接与认证失败：
        LogViewerError(SSH_FAILED)。被取消时 CancelledError 原样抛出。
        """
        if not is_valid_ipv4(ip_inner):
            raise LogViewerError(ErrorCode.INVALID_SERVER_ADDRESS)
        try:
            conn = await asyncssh.connect(ip_inner, **self._options())
        except asyncssh.HostKeyNotVerifiable as e:
            logger.warning("游戏服 %s 的主机公钥未登记或与 known_hosts 不符，已拒绝登录：%s", ip_inner, _describe(e))
            raise LogViewerError(ErrorCode.HOST_KEY_MISMATCH) from None
        except (asyncssh.Error, OSError, TimeoutError) as e:
            # OSError 含连接被拒、主机不可达与名称解析失败；TimeoutError 为 connect_timeout 到期
            logger.warning("SSH 连接游戏服 %s 失败：%s", ip_inner, _describe(e))
            raise LogViewerError(ErrorCode.SSH_FAILED) from None
        return RemoteConnection(conn, ip_inner)


class RemoteConnection:
    """一个已登录的 SSH 连接；可在其上依次或同时开多个会话。用完须 close()（或用 async with）。"""

    def __init__(self, conn: asyncssh.SSHClientConnection, host: str) -> None:
        self._conn = conn
        self.host = host
        self._closed = False

    async def __aenter__(self) -> RemoteConnection:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def start(self, command: str, script: str | bytes = b"") -> RemoteProcess:
        """新开会话执行固定命令行 command，把 script 写入标准输入后关闭标准输入。

        command 只能含固定命令行允许的字符（is_fixed_command），否则抛出 ValueError（程序错误）。
        script 为 str 时按 UTF-8 编码。会话打开或写入失败时抛出 LogViewerError(SSH_FAILED)。
        """
        if not is_fixed_command(command):
            raise ValueError("命令行含固定命令行之外的字符")
        data = script.encode("utf-8") if isinstance(script, str) else bytes(script)
        if self._closed:
            raise LogViewerError(ErrorCode.SSH_FAILED)
        try:
            process = await self._conn.create_process(
                command, encoding=None, request_pty=False, term_type=None, x11_forwarding=False,
                env=None, send_env=None,
            )
        except (asyncssh.Error, OSError) as e:
            logger.warning("在游戏服 %s 上打开会话失败：%s", self.host, _describe(e))
            raise LogViewerError(ErrorCode.SSH_FAILED) from None
        remote = RemoteProcess(process, self.host)
        try:
            if data:
                process.stdin.write(data)
            process.stdin.write_eof()
        except (asyncssh.Error, OSError) as e:
            logger.warning("向游戏服 %s 写入脚本失败：%s", self.host, _describe(e))
            remote.close()
            raise LogViewerError(ErrorCode.SSH_FAILED) from None
        return remote

    async def close(self) -> None:
        """关闭连接（可重复调用）；对端在 CLOSE_TIMEOUT_S 内没有确认时直接断开。"""
        if self._closed:
            return
        self._closed = True
        self._conn.close()
        try:
            async with asyncio.timeout(CLOSE_TIMEOUT_S):
                await self._conn.wait_closed()
        except TimeoutError:
            self._conn.abort()

    @property
    def closed(self) -> bool:
        return self._closed


class RemoteProcess:
    """远端会话：标准输出以流的方式读取，标准错误由后台任务单独读取并写入运行日志。"""

    def __init__(self, process: asyncssh.SSHClientProcess, host: str) -> None:
        self._process = process
        self.host = host
        self._stderr_head = bytearray()
        self._stderr_total = 0
        self._stderr_logged = False
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        # 必须持续读取：asyncssh 按全部未读数据控制流量，标准错误堆积会让标准输出也停下来
        try:
            while True:
                chunk = await self._process.stderr.read(READ_CHUNK)
                if not chunk:
                    return
                self._stderr_total += len(chunk)
                room = STDERR_LOG_LIMIT - len(self._stderr_head)
                if room > 0:
                    self._stderr_head += chunk[:room]
        except (asyncssh.Error, OSError):
            return  # 连接中断由标准输出一侧报告

    def _log_stderr(self) -> None:
        if self._stderr_logged:
            return
        self._stderr_logged = True
        if self._stderr_total == 0:
            return
        text = bytes(self._stderr_head).decode("utf-8", "backslashreplace")
        note = "，已截断" if self._stderr_total > len(self._stderr_head) else ""
        logger.warning("游戏服 %s 的标准错误（共 %d 字节%s）：%r", self.host, self._stderr_total, note, text)

    async def read(self, n: int = READ_CHUNK) -> bytes:
        """读取至多 n 字节的标准输出；读完返回 b""。连接中断时抛出 LogViewerError(SSH_FAILED)。"""
        if n <= 0:
            raise ValueError("n 须为正数")
        try:
            return await self._process.stdout.read(n)
        except (asyncssh.Error, OSError) as e:
            logger.warning("读取游戏服 %s 的输出时连接中断：%s", self.host, _describe(e))
            raise LogViewerError(ErrorCode.SSH_FAILED) from None

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while chunk := await self.read():
            yield chunk

    async def wait(self) -> RemoteExit:
        """等待会话结束（应先把标准输出读完），写出标准错误的运行日志，返回结束情况。"""
        try:
            await self._process.wait_closed()
            await self._stderr_task
        finally:
            self._log_stderr()
        signal = self._process.exit_signal
        status = self._process.exit_status
        if signal is not None:
            return RemoteExit(None, signal[0], self._stderr_total)
        return RemoteExit(status if status is not None and status >= 0 else None, None, self._stderr_total)

    def close(self) -> None:
        """关闭本会话的通道（不终止远端进程，终止见 6.6），写出已读到的标准错误。"""
        self._process.close()
        if not self._stderr_task.done():
            self._stderr_task.cancel()
        self._log_stderr()


def check_ssh_credentials(config: Config) -> None:
    """``load_config_or_exit(extra_checks=[…])`` 用的启动检查：私钥与配置了的 known_hosts 能被导入（10.2）。"""
    SshConnector(config.ssh)


# ---------------------------------------------------------------------------
# 固定命令行（design.md「固定命令行」）
# ---------------------------------------------------------------------------

# timeout 的秒数 = 剩余时长向上取整再加 REMOTE_TIMEOUT_MARGIN_S（9.4：SSH 断开或服务退出后远端自行结束）。
REMOTE_TIMEOUT_MARGIN_S = 5

# 秒数的上限（防御：程序算出的值不应超过一天）。
MAX_REMOTE_SECONDS = 86400

_COMMAND_HEAD = "env -i PATH=/usr/bin:/bin LC_ALL=C timeout -s KILL %d nice -n 19"
_COMMAND_TAIL = " bash --noprofile --norc -s"


def remote_seconds(remaining: float) -> int:
    """timeout 的秒数：剩余时长（秒）向上取整再加 5；剩余时长为负时按 0 计。"""
    if type(remaining) not in (int, float) or not math.isfinite(remaining):
        raise ValueError("剩余时长须为有限的数值")
    seconds = math.ceil(max(remaining, 0)) + REMOTE_TIMEOUT_MARGIN_S
    return min(seconds, MAX_REMOTE_SECONDS)


def build_command(seconds: int, *, ionice: bool = False) -> str:
    """SSH 执行的固定命令行；只含程序算出的整数 seconds，不含任何请求值（8.3）。

    ① 不加 ionice（ionice 缺失会让整行失败）；② 按 ① 的探测结果传入 ionice=True（9.1）。
    """
    if type(seconds) is not int or not 1 <= seconds <= MAX_REMOTE_SECONDS:
        raise ValueError("timeout 秒数须为 1 至 %d 的整数" % MAX_REMOTE_SECONDS)
    if type(ionice) is not bool:
        raise ValueError("ionice 须为布尔值")
    command = _COMMAND_HEAD % seconds + (" ionice -c 3" if ionice else "") + _COMMAND_TAIL
    if not is_fixed_command(command):  # 防御：生成的命令行必须只含固定字符
        raise ValueError("命令行含固定命令行之外的字符")
    return command


# ---------------------------------------------------------------------------
# ① 列目录与探测
# ---------------------------------------------------------------------------

# 远端所需的工具；缺少任一个即 REMOTE_TOOL_UNAVAILABLE。
REQUIRED_TOOLS = ("find", "grep", "gzip")


def _quote_dir(path: str) -> str:
    """配置中的目录：再次校验为绝对路径且只含 [A-Za-z0-9/._-]，然后 shlex.quote。"""
    if type(path) is not str or REMOTE_DIR_PATTERN.fullmatch(path) is None:
        raise ValueError("游戏服目录须为绝对路径且只含字母、数字与 /._-")
    return shlex.quote(path)


def build_listing_script(plain_dir: str, zip_dir: str | None) -> str:
    """① 列目录与探测脚本（经标准输入交给 bash）。

    plain_dir 为请求 Log_Type 的非压缩目录，zip_dir 为该类型的压缩目录，二者都来自配置（8.2）。
    zip_dir 为 None（未配置）时不列压缩目录；配置了但游戏服上不存在该目录时也当作没有压缩文件，
    两种情况都直接输出 ``EXIT Z 0``。压缩目录存在但不可列举时仍是 LOG_DIR_UNAVAILABLE。
    所有记录以 NUL 结尾；开头的 NUL 之前是登录 Shell 的输出，一律丢弃。``-type f`` 只列普通文件，
    符号链接与子目录都不列（2.1）；非压缩目录的记录另含 inode，供 ② 定位 Current_Log_File（2.8）。
    """
    plain = _quote_dir(plain_dir)
    tools = " ".join(REQUIRED_TOOLS)
    lines = [
        "{ printf '\\0'",
        f"  for t in {tools}; do command -v \"$t\" >/dev/null || printf 'MISSING %s\\0' \"$t\"; done",
        "  ionice -c 3 true && printf 'IONICE\\0'",
        f"  find -H {plain} -mindepth 1 -maxdepth 1 -type f -printf 'L %i %s %f\\0'",
        "  printf 'EXIT L %d\\0' \"$?\"",
    ]
    if zip_dir is None:
        lines.append("  printf 'EXIT Z 0\\0'")
    else:
        zipped = _quote_dir(zip_dir)
        lines += [
            f"  if [ -e {zipped} ] || [ -L {zipped} ]; then",
            f"    find -H {zipped} -mindepth 1 -maxdepth 1 -type f -printf 'Z %s %f\\0'",
            "    printf 'EXIT Z %d\\0' \"$?\"",
            "  else printf 'EXIT Z 0\\0'; fi",
        ]
    lines.append("} </dev/null")
    return "\n".join(lines) + "\n"


# 文件名只保留只含 [A-Za-z0-9._-] 的（8.2）；是否符合 2.2 的三种格式由 select.py 判断。
_SAFE_NAME_PATTERN = re.compile(rb"[A-Za-z0-9._-]{1,255}")
_INODE_PATTERN = re.compile(rb"[0-9]{1,20}")
_SIZE_PATTERN = re.compile(rb"[0-9]{1,20}")
_EXIT_PATTERN = re.compile(rb"[0-9]{1,3}")

# 单条记录的长度上限（文件名不超过 255 字节，加上类型、inode 与大小）；超过即视为输出不符。
MAX_RECORD_BYTES = 1024

# 一个目录的记录条数上限（防御：避免异常目录占满内存）。
MAX_LISTING_RECORDS = 2_000_000


def is_safe_file_name(name: object) -> bool:
    """文件名是否只含 [A-Za-z0-9._-]（且不是 "." 或 ".."）。"""
    if type(name) is str:
        try:
            name = name.encode("ascii")
        except UnicodeEncodeError:
            return False
    if type(name) is not bytes:
        return False
    return _SAFE_NAME_PATTERN.fullmatch(name) is not None and name not in (b".", b"..")


@dataclass(frozen=True)
class PlainEntry:
    """非压缩目录中的一个普通文件：文件名、inode 与磁盘大小（字节）。"""

    name: str
    inode: int
    size: int


@dataclass(frozen=True)
class ArchiveEntry:
    """ziplogs 中的一个普通文件：文件名与磁盘大小（字节）。"""

    name: str
    size: int


@dataclass(frozen=True)
class Listing:
    """① 的结果。

    plain、archives 按列举顺序给出名称安全的普通文件（是否符合 2.2 由 select.py 判断）；
    ionice 表示游戏服能以 ionice idle 运行（② 据此决定是否加 ionice，9.1）；
    ignored 为因名称含其他字符而丢弃的记录数。
    """

    plain: tuple[PlainEntry, ...]
    archives: tuple[ArchiveEntry, ...]
    ionice: bool
    ignored: int = 0


class _Malformed(Exception):
    pass


class ListingParser:
    """按块解析 ① 的输出（不访问网络与文件系统）：``feed`` 逐块喂入，``finish`` 得到 Listing。

    记录顺序须为：MISSING*、IONICE?、L*、EXIT L、Z*、EXIT Z；顺序或格式不符时 ``feed`` 抛出
    LogViewerError(SSH_FAILED)。
    """

    _PROBE, _PLAIN, _ZIP, _DONE = range(4)

    def __init__(self, host: str = "") -> None:
        self._host = host
        self._started = False
        self._noise_bytes = 0
        self._buf = b""
        self._phase = self._PROBE
        self._missing: list[str] = []
        self._ionice = False
        self._plain: list[PlainEntry] = []
        self._archives: list[ArchiveEntry] = []
        self._plain_records = 0
        self._zip_records = 0
        self._ignored = 0
        self._exit_plain: int | None = None
        self._exit_zip: int | None = None

    # ---- 喂入 ----

    def feed(self, data: bytes) -> None:
        if not data:
            return
        if not self._started:
            i = data.find(b"\0")
            if i < 0:
                self._noise_bytes += len(data)
                return
            self._noise_bytes += i
            self._started = True
            data = data[i + 1:]
        parts = (self._buf + data).split(b"\0")
        self._buf = parts.pop()
        try:
            for record in parts:
                self._record(record)
            if len(self._buf) > MAX_RECORD_BYTES:
                raise _Malformed("记录过长")
        except _Malformed as e:
            logger.warning("游戏服 %s 的目录列举输出不符合格式：%s", self._host, e)
            raise LogViewerError(ErrorCode.SSH_FAILED) from None

    def _record(self, record: bytes) -> None:
        if len(record) > MAX_RECORD_BYTES:
            raise _Malformed("记录过长")
        if self._phase == self._DONE:
            raise _Malformed("EXIT Z 之后还有记录")
        if record.startswith(b"L "):
            self._plain_entry(record)
        elif record.startswith(b"Z "):
            self._zip_entry(record)
        elif record.startswith(b"EXIT "):
            self._exit(record)
        elif record.startswith(b"MISSING "):
            tool = record[len(b"MISSING "):].decode("ascii", "replace")
            if self._phase != self._PROBE or self._ionice or tool not in REQUIRED_TOOLS or tool in self._missing:
                raise _Malformed(f"意外的 MISSING 记录 {record[:64]!r}")
            self._missing.append(tool)
        elif record == b"IONICE":
            if self._phase != self._PROBE or self._ionice:
                raise _Malformed("意外的 IONICE 记录")
            self._ionice = True
        else:
            raise _Malformed(f"未知记录 {record[:64]!r}")

    def _count(self, current: int) -> int:
        if current >= MAX_LISTING_RECORDS:
            raise _Malformed("目录记录过多")
        return current + 1

    def _plain_entry(self, record: bytes) -> None:
        if self._phase not in (self._PROBE, self._PLAIN):
            raise _Malformed("意外的 L 记录")
        self._phase = self._PLAIN
        fields = record.split(b" ", 3)
        if len(fields) != 4 or not _INODE_PATTERN.fullmatch(fields[1]) or not _SIZE_PATTERN.fullmatch(fields[2]):
            raise _Malformed(f"L 记录格式不符 {record[:64]!r}")
        self._plain_records = self._count(self._plain_records)
        name = fields[3]
        if not is_safe_file_name(name):
            self._ignored += 1
            return
        self._plain.append(PlainEntry(name.decode("ascii"), int(fields[1]), int(fields[2])))

    def _zip_entry(self, record: bytes) -> None:
        if self._phase != self._ZIP:
            raise _Malformed("意外的 Z 记录")
        fields = record.split(b" ", 2)
        if len(fields) != 3 or not _SIZE_PATTERN.fullmatch(fields[1]):
            raise _Malformed(f"Z 记录格式不符 {record[:64]!r}")
        self._zip_records = self._count(self._zip_records)
        name = fields[2]
        if not is_safe_file_name(name):
            self._ignored += 1
            return
        self._archives.append(ArchiveEntry(name.decode("ascii"), int(fields[1])))

    def _exit(self, record: bytes) -> None:
        fields = record.split(b" ")
        if len(fields) != 3 or not _EXIT_PATTERN.fullmatch(fields[2]):
            raise _Malformed(f"EXIT 记录格式不符 {record[:64]!r}")
        status = int(fields[2])
        if fields[1] == b"L" and self._phase in (self._PROBE, self._PLAIN):
            self._exit_plain = status
            self._phase = self._ZIP
        elif fields[1] == b"Z" and self._phase == self._ZIP:
            self._exit_zip = status
            self._phase = self._DONE
        else:
            raise _Malformed(f"意外的 EXIT 记录 {record[:64]!r}")

    # ---- 结束 ----

    def finish(self, exit: RemoteExit) -> Listing:
        """会话结束后得出结果或错误码（LogViewerError）。"""
        if not self._started:
            if exit.exit_status == 127:
                # timeout、nice 或 bash 缺失：没有任何脚本输出
                logger.warning("游戏服 %s 上固定命令行中的程序缺失（退出码 127，没有脚本输出）", self._host)
                raise LogViewerError(ErrorCode.REMOTE_TOOL_UNAVAILABLE)
            logger.warning("游戏服 %s 没有输出目录列举的起始标记（退出码 %s，信号 %s）",
                           self._host, exit.exit_status, exit.exit_signal)
            raise LogViewerError(ErrorCode.SSH_FAILED)
        if self._missing:
            logger.warning("游戏服 %s 缺少命令：%s", self._host, "、".join(self._missing))
            raise LogViewerError(ErrorCode.REMOTE_TOOL_UNAVAILABLE)
        if self._phase != self._DONE or self._buf:
            logger.warning("游戏服 %s 的目录列举输出不完整（退出码 %s，信号 %s）",
                           self._host, exit.exit_status, exit.exit_signal)
            raise LogViewerError(ErrorCode.SSH_FAILED)
        # find 因文件在列举中被删除而返回 1 是正常的；只有退出码非 0 且该目录一条记录都没有时才算不可列举
        for which, status, count in (("非压缩目录", self._exit_plain, self._plain_records),
                                     ("ziplogs", self._exit_zip, self._zip_records)):
            if status != 0 and count == 0:
                logger.warning("游戏服 %s 上的%s不可列举（find 退出码 %s）", self._host, which, status)
                raise LogViewerError(ErrorCode.LOG_DIR_UNAVAILABLE)
        return Listing(tuple(self._plain), tuple(self._archives), self._ionice, self._ignored)


async def run_listing(conn: RemoteConnection, script: str, *, seconds: int) -> Listing:
    """在 conn 上以固定命令行（不加 ionice）执行 ① 脚本 script（由 build_listing_script 生成）。

    失败时抛出 LogViewerError（REMOTE_TOOL_UNAVAILABLE、LOG_DIR_UNAVAILABLE、SSH_FAILED）；
    被取消或出错时关闭本会话，远端进程由 timeout 兜底结束。
    """
    command = build_command(seconds, ionice=False)
    process = await conn.start(command, script)
    parser = ListingParser(conn.host)
    finished = False
    try:
        async for chunk in process:
            parser.feed(chunk)
        exit = await process.wait()
        finished = True
    finally:
        if not finished:
            process.close()
    return parser.finish(exit)


# ---------------------------------------------------------------------------
# ② 查询脚本
# ---------------------------------------------------------------------------

# 边界串 B：secrets.token_hex(16)，只含 [0-9a-f]（8.7：日志内容无法预知，也就无法伪造帧行）。
BOUNDARY_PATTERN = re.compile(r"[0-9a-f]{32}")

# Unicode Cc 类字符（含 NUL、TAB、LF、CR）；与 validate.py 的规则相同，这里再次检查（防御）。
_PATTERN_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")

# 文件名再次校验（防御，不依赖 Server_Timezone）：只接受 2.2 的三种格式。
_NAME_TS = r"([0-9]{4}-[0-9]{2}-[0-9]{2}(?:-[0-9]{2}(?:-[0-9]{2})?)?)"
_CURRENT_NAME = "smartfox.log"
_ROTATED_NAME_PATTERN = re.compile(r"smartfox\.log\." + _NAME_TS)
_ARCHIVE_NAME_PATTERN = re.compile(r"smartfox\.log\." + _NAME_TS + r"(?:\.[A-Za-z][A-Za-z0-9_-]{0,31})?\.tar\.gz")

# 上下文行数与 inode 的上限（防御：validate.py 已按 Max_Context_Lines 校验；inode 须 fullmatch [0-9]{1,20}）。
MAX_CONTEXT_ARG = 1_000_000
MAX_INODE = 10**20 - 1


@dataclass(frozen=True)
class QueryFile:
    """② 脚本中的一个序号 i（F[i] 与 Z[i]），供摘要的 files_read、files_skipped 与 6.4 的分帧使用。

    - plain、archive：非压缩文件名与同一 TS 的压缩文件名（没有时为 None）。
    - plain_path、archive_path：写进脚本的完整路径（没有时为 None）。
    - inode：Current 的 inode（② 按它定位改名后的文件，2.8），其他项为 None。
    - source：对应的选取结果（select.SelectedFile）。
    """

    index: int
    plain: str | None
    archive: str | None
    plain_path: str | None
    archive_path: str | None
    inode: int | None
    source: Any

    @property
    def is_current(self) -> bool:
        return self.plain == _CURRENT_NAME

    @property
    def name(self) -> str:
        """摘要中使用的文件名：优先读取的那个文件。"""
        name = self.plain if self.plain is not None else self.archive
        assert name is not None
        return name


@dataclass(frozen=True)
class QueryScript:
    """build_query_script 的结果：脚本文本、边界串 B、按序号排列的文件与固定命令行。

    grep_stages：管道中 grep 的段数（有排除模式时为 2，否则为 1），交给 FrameParser 校验 END 的退出码个数；
    为 None 时不校验个数。
    """

    script: str
    boundary: str
    files: tuple[QueryFile, ...]
    command: str
    grep_stages: int | None = None


def _single_quote(value: str) -> str:
    """shlex.quote 的单引号转义，对只含安全字符的值也加引号，使每个值在脚本中都是一个单引号词。"""
    quoted = shlex.quote(value)
    return quoted if quoted.startswith("'") else "'" + quoted + "'"


def _check_patterns(name: str, patterns: Any) -> tuple[str, ...]:
    """模式再次校验（防御）：非空的 str，可编码为 UTF-8，不含 Cc 类字符（LF 会让 grep 拆成多个模式）。"""
    if isinstance(patterns, (str, bytes)) or not isinstance(patterns, Sequence):
        raise ValueError(f"{name} 须为字符串序列")
    result = []
    for p in patterns:
        if type(p) is not str or not p or _PATTERN_CONTROL_CHARS.search(p):
            raise ValueError(f"{name} 中的模式须为不含控制字符的非空字符串")
        try:
            p.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError(f"{name} 中的模式须能编码为 UTF-8") from None
        result.append(p)
    return tuple(result)


def _check_bool(name: str, value: Any) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{name} 须为布尔值")
    return value


def _check_count(name: str, value: Any, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"{name} 须为 0 至 {maximum} 的整数")
    return value


def _join(directory: str, name: str) -> str:
    return directory.rstrip("/") + "/" + name


def _query_files(files: Any, plain_dir: str, zip_dir: str | None) -> tuple[QueryFile, ...]:
    """把选取结果转成 QueryFile，并再次校验文件名、inode 与 Current 的位置。"""
    result: list[QueryFile] = []
    for index, f in enumerate(files):
        plain, archive, inode = f.plain, f.archive, f.inode
        if plain is None and archive is None:
            raise ValueError("选取的文件须至少有非压缩文件或压缩文件之一")
        if archive is not None and zip_dir is None:
            raise ValueError("没有配置压缩目录时不能选取压缩文件")
        for n in (plain, archive):
            if n is not None and not is_safe_file_name(n):
                raise ValueError("文件名只能含字母、数字与 ._-")
        if plain == _CURRENT_NAME:
            if archive is not None:
                raise ValueError("Current_Log_File 没有对应的压缩文件")
            if type(inode) is not int or not 0 <= inode <= MAX_INODE:
                raise ValueError("Current_Log_File 须带合法的 inode")
            if any(q.is_current for q in result):
                raise ValueError("Current_Log_File 只能出现一次")
        else:
            if inode is not None:
                raise ValueError("只有 Current_Log_File 带 inode")
            ts_plain = ts_archive = None
            if plain is not None:
                m = _ROTATED_NAME_PATTERN.fullmatch(plain)
                if m is None:
                    raise ValueError("非压缩文件名不符合 smartfox.log.<时间戳>")
                ts_plain = m.group(1)
            if archive is not None:
                m = _ARCHIVE_NAME_PATTERN.fullmatch(archive)
                if m is None:
                    raise ValueError("压缩文件名不符合 smartfox.log.<时间戳><后缀>.tar.gz")
                ts_archive = m.group(1)
            if ts_plain is not None and ts_archive is not None and ts_plain != ts_archive:
                raise ValueError("同一项的非压缩文件与压缩文件须为同一时间戳")
        result.append(QueryFile(
            index=index, plain=plain, archive=archive,
            plain_path=_join(plain_dir, plain) if plain is not None else None,
            archive_path=_join(zip_dir, archive) if archive is not None and zip_dir is not None else None,
            inode=inode if plain == _CURRENT_NAME else None, source=f,
        ))
    return tuple(result)


def build_query_script(
    files: Sequence[Any],
    *,
    plain_dir: str,
    zip_dir: str | None,
    include_patterns: Sequence[str] = (),
    exclude_patterns: Sequence[str] = (),
    regex: bool = False,
    ignore_case: bool = False,
    context_before: int = 0,
    context_after: int = 0,
    seconds: int,
    ionice: bool,
    boundary: str | None = None,
) -> QueryScript:
    """② 查询脚本（design.md「固定命令行」中 ② 的模板）与固定命令行。

    files 为 select_files 选出的项（select.SelectedFile，或任何带 plain、archive、inode 属性的对象），
    依次对应脚本中的序号；plain_dir 为请求 Log_Type 的非压缩目录，zip_dir 为该类型的压缩目录（都来自配置，
    未配置压缩目录时为 None，此时选取结果中不能有压缩文件）。
    ionice 取 ① 的探测结果（9.1），seconds 为 timeout 的秒数（remote_seconds）。boundary 默认每次
    用 secrets.token_hex(16) 生成，只有测试才传入。

    普通文件存在且不是符号链接时读它，否则读同一 TS 的压缩文件（2.4、2.6）；两者都用 gzip -dcf 读出
    交给 grep：先排除、再包含（含上下文）（1.4）；读到文件末尾即结束，不跟踪新写入（9.6）。
    Current_Log_File 按 ① 列出的 inode 定位（2.8）。请求值只作为单引号转义后的 grep -e 参数，
    位于固定选项之后并以 "--" 结束选项（8.5）；脚本只用 find、grep、gzip 与 bash 内建命令，
    重定向只有 </dev/null，没有 eval 与 here-doc（8.4、8.6）。
    """
    _quote_dir(plain_dir)  # 再次校验目录格式（不符抛出 ValueError）
    if zip_dir is not None:
        _quote_dir(zip_dir)
    plain_q = _single_quote(plain_dir)
    includes = _check_patterns("include_patterns", include_patterns)
    excludes = _check_patterns("exclude_patterns", exclude_patterns)
    regex = _check_bool("regex", regex)
    ignore_case = _check_bool("ignore_case", ignore_case)
    context_before = _check_count("context_before", context_before, MAX_CONTEXT_ARG)
    context_after = _check_count("context_after", context_after, MAX_CONTEXT_ARG)
    if boundary is None:
        boundary = secrets.token_hex(16)
    if type(boundary) is not str or BOUNDARY_PATTERN.fullmatch(boundary) is None:
        raise ValueError("边界串须为 32 个 [0-9a-f] 字符")
    command = build_command(seconds, ionice=ionice)
    query_files = _query_files(files, plain_dir, zip_dir)

    current = next((q for q in query_files if q.is_current), None)
    ck = current.index if current is not None else -1
    ci = current.inode if current is not None else 0

    mode = "-E" if regex else "-F"
    common = "grep -a " + mode + (" -i" if ignore_case else "")
    include_opts = ""
    if context_before > 0:
        include_opts += " -B %d" % context_before
    if context_after > 0:
        include_opts += " -A %d" % context_after
    include_stage = common + include_opts + ' "${I[@]}" --'
    if excludes:
        pipeline = 'gzip -dcf -- "$p" | ' + common + ' -v "${X[@]}" -- | ' + include_stage
    else:
        pipeline = 'gzip -dcf -- "$p" | ' + include_stage

    def words(values: Sequence[str]) -> str:
        return " ".join("-e " + _single_quote(v) for v in values)

    def array(values: Sequence[str | None]) -> str:
        return " ".join(_single_quote(v) if v is not None else "''" for v in values)

    lines = ["{ B=" + boundary]
    if excludes:
        lines.append("  X=(" + words(excludes) + ")")
    lines.append("  I=(" + (words(includes) if includes else "-e ''") + ")")
    lines.append("  F=(" + array([q.plain_path for q in query_files]) + ")")
    lines.append("  Z=(" + array([q.archive_path for q in query_files]) + ")")
    lines.append("  D=%s CK=%d CI=%d" % (plain_q, ck, ci))
    lines += [
        "  printf '%s READY %s\\n' \"$B\" \"$PPID\"",
        '  for i in "${!F[@]}"; do',
        "    f=${F[i]} z=${Z[i]}",
        '    if [ "$i" = "$CK" ]; then n=$(find -H "$D" -mindepth 1 -maxdepth 1 -type f -inum "$CI" -printf \'%f\' -quit)',
        "      case $n in smartfox.log|smartfox.log.[0-9]*) f=$D/$n ;; *) f= ;; esac",
        "    fi",
        "    printf '%s FILE %d\\n' \"$B\" \"$i\"",
        '    if [ -f "$f" ] && [ ! -L "$f" ]; then p=$f k=F',
        '    elif [ -f "$z" ] && [ ! -L "$z" ]; then p=$z k=Z',
        "    else p= k=N; fi",
        '    if [ -n "$p" ]; then',
        "      " + pipeline + '; s="$k ${PIPESTATUS[*]}"',
        "    else s=N; fi",
        "    printf '%s END %d %s\\n' \"$B\" \"$i\" \"$s\"",
        "  done",
        "  printf '%s DONE\\n' \"$B\"",
        "} </dev/null",
    ]
    return QueryScript("\n".join(lines) + "\n", boundary, query_files, command, 2 if excludes else 1)


# ---------------------------------------------------------------------------
# ② 输出的分帧解析（design.md「终止、分帧解析与 SSH」）
# ---------------------------------------------------------------------------

# 帧关键字；帧行 = 行首的 "B 加空格加帧关键字"，之后到 LF 为止是该帧的参数。
FRAME_KEYWORDS = (b"READY", b"FILE", b"END", b"DONE")

# 帧行在关键字之后、LF 之前的最大字节数（最长的是 END：" <序号> <分支> <最多 3 个退出码>"）。
MAX_FRAME_ARGS = 128

_READY_ARGS = re.compile(rb" ([0-9]{1,10})")
_FILE_ARGS = re.compile(rb" (0|[1-9][0-9]{0,9})")
_END_ARGS = re.compile(rb" (0|[1-9][0-9]{0,9}) (.*)", re.DOTALL)
_STATUS_RAN = re.compile(rb"([FZ]) ([0-9]{1,3})((?: [0-9]{1,3}){1,2})")


@dataclass(frozen=True)
class FileStatus:
    """一个序号的结束状态（END 帧）。

    branch：实际读取的分支，F 为非压缩文件，Z 为压缩文件，N 为两者都不存在。
    gzip：gzip -dcf 的退出码（N 时为 None）；grep：各段 grep 的退出码（先排除段、后包含段；N 时为空）。
    ok：读取成功，即 gzip 为 0 且每段 grep 为 0 或 1；否则该文件失败，记入 files_skipped 并按偏移截回（2.7）。
    """

    branch: str
    gzip: int | None
    grep: tuple[int, ...]

    @property
    def ok(self) -> bool:
        return self.branch != "N" and self.gzip == 0 and all(c in (0, 1) for c in self.grep)


@dataclass(frozen=True)
class FrameReady:
    """收到 READY 行：pgid 为远端进程组号（fullmatch [0-9]{1,10} 且大于 1），终止时 kill -KILL -- -<pgid>。"""

    pgid: int


@dataclass(frozen=True)
class FileStart:
    """序号 index 的文件开始输出（FILE 帧）；调用方此时记下结果文件的偏移。"""

    index: int


@dataclass(frozen=True)
class FileData:
    """序号 index 的一段原始 grep 输出，按到达顺序原样写入结果文件（不一定在行边界上切分）。"""

    index: int
    data: bytes


@dataclass(frozen=True)
class FileEnd:
    """序号 index 的文件结束（END 帧）；status.ok 为假时调用方截回到 FileStart 时的偏移。"""

    index: int
    status: FileStatus


@dataclass(frozen=True)
class FrameDone:
    """收到 DONE 行：全部序号都已结束。"""


FrameEvent = FrameReady | FileStart | FileData | FileEnd | FrameDone


class _BadFrame(Exception):
    pass


def parse_file_status(text: bytes, grep_stages: int | None = None) -> FileStatus:
    """解析 END 帧中的结束状态：``N``，或 ``F|Z <gzip 退出码> <grep 退出码>…``（1 或 2 段 grep）。

    grep_stages 给出时，grep 退出码的个数须与之相同。不符时抛出 ValueError。
    """
    if text == b"N":
        if grep_stages is not None and grep_stages not in (1, 2):
            raise ValueError("grep 段数须为 1 或 2")
        return FileStatus("N", None, ())
    m = _STATUS_RAN.fullmatch(text)
    if m is None:
        raise ValueError("结束状态格式不符")
    gzip = int(m.group(2))
    grep = tuple(int(c) for c in m.group(3).split())
    if gzip > 255 or any(c > 255 for c in grep):
        raise ValueError("退出码超出 0 至 255")
    if grep_stages is not None and len(grep) != grep_stages:
        raise ValueError("grep 退出码的个数与段数不符")
    return FileStatus(m.group(1).decode("ascii"), gzip, grep)


class FrameParser:
    """按块解析 ② 的标准输出（纯逻辑，不访问网络与文件系统）。

    ``feed(chunk)`` 可在任意位置切块（包括 B 或帧关键字的中间），返回本块产生的事件列表
    （FrameReady、FileStart、FileData、FileEnd、FrameDone）；``finish()`` 在输出结束（正常结束、
    超时或连接中断）时调用，把行首暂存的字节作为当前文件的数据交出。

    - READY 行之前的内容（登录 Shell 的输出）一律丢弃；``B READY `` 可出现在噪声的任意位置之后
      （噪声可能不以换行结尾），B 是随机串，噪声无法伪造它。
    - 之后只认行首的 "B 加空格加帧关键字" 为帧行，其余行（包括行中出现的 B、以 B 开头但没有关键字
      的行、关键字前没有空格的行）原样归当前文件（8.7）。只在行首暂存至多 len(B)+1+关键字长度的
      字节，长行不整行缓存。
    - 帧序号或顺序不符（FILE 不是下一个序号、END 与当前序号不符、DONE 不在最后一个 END 之后、
      FILE 与 END 之外有数据、DONE 之后还有输出、帧参数无法解析）时抛出 LogViewerError(INTERNAL_ERROR)，
      之后的 feed 继续抛出同样的错误。

    超时或连接中断时由 ``current``（正在输出的序号）、``statuses``（已结束序号的状态）与 ``done``
    得知完成情况。
    """

    def __init__(self, boundary: str, file_count: int, *, grep_stages: int | None = None, host: str = "") -> None:
        if type(boundary) is not str or BOUNDARY_PATTERN.fullmatch(boundary) is None:
            raise ValueError("边界串须为 32 个 [0-9a-f] 字符")
        if type(file_count) is not int or file_count < 0:
            raise ValueError("文件数须为非负整数")
        if grep_stages is not None and grep_stages not in (1, 2):
            raise ValueError("grep 段数须为 1 或 2")
        self._host = host
        self._b = boundary.encode("ascii")
        self._nl_b = b"\n" + self._b
        self._prefixes = tuple((kw, self._b + b" " + kw) for kw in FRAME_KEYWORDS)
        self._max_prefix = max(len(p) for _, p in self._prefixes)
        self._ready_marker = self._b + b" READY"
        self._file_count = file_count
        self._grep_stages = grep_stages
        # 状态
        self._noise_tail = b""       # READY 之前：末尾可能是标记的前半段
        self.noise_bytes = 0         # 丢弃的噪声字节数（只用于运行日志）
        self._started = False        # 已找到 READY 标记
        self._pending = b""          # 行首暂存：可能是帧行前缀的字节
        self._at_line_start = True
        self._line: bytearray | None = None  # 正在读取的帧行参数（关键字之后到 LF 之前）
        self._kw = b""
        self._pgid: int | None = None
        self._current: int | None = None
        self._statuses: list[FileStatus] = []
        self._done = False
        self._failed = False
        self._finished = False

    # ---- 状态 ----

    @property
    def pgid(self) -> int | None:
        """远端进程组号；还没收到 READY 时为 None（此时终止只关闭连接）。"""
        return self._pgid

    @property
    def current(self) -> int | None:
        """正在输出（已收到 FILE、还没收到 END）的序号，否则为 None。"""
        return self._current

    @property
    def statuses(self) -> tuple[FileStatus, ...]:
        """已结束序号的状态，按序号排列（第 i 项即序号 i）。"""
        return tuple(self._statuses)

    @property
    def done(self) -> bool:
        """是否已收到 DONE。"""
        return self._done

    # ---- 喂入 ----

    def feed(self, chunk: bytes) -> list[FrameEvent]:
        if self._failed:
            raise LogViewerError(ErrorCode.INTERNAL_ERROR)
        if self._finished:
            raise ValueError("finish() 之后不能再喂入")
        events: list[FrameEvent] = []
        if not chunk:
            return events
        try:
            if not self._started:
                chunk = self._skip_noise(bytes(chunk))
                if chunk is None:
                    return events
            elif self._pending:
                chunk = self._pending + chunk
                self._pending = b""
            self._feed_body(bytes(chunk), events)
        except _BadFrame as e:
            self._failed = True
            logger.warning("游戏服 %s 的查询输出分帧不符：%s", self._host, e)
            raise LogViewerError(ErrorCode.INTERNAL_ERROR) from None
        return events

    def finish(self) -> list[FrameEvent]:
        """输出结束时调用（可在超时或中断后调用）：行首暂存的字节归当前文件；未完成的帧行丢弃。"""
        if self._failed:
            raise LogViewerError(ErrorCode.INTERNAL_ERROR)
        events: list[FrameEvent] = []
        if self._finished:
            return events
        self._finished = True
        if self._started and self._line is None and self._pending and self._current is not None:
            events.append(FileData(self._current, self._pending))
        self._pending = b""
        self._line = None
        return events

    def _skip_noise(self, chunk: bytes) -> bytes | None:
        """丢弃 READY 之前的内容；找到 ``B READY`` 时返回其后的字节（此后读取 READY 的参数）。"""
        data = self._noise_tail + chunk
        i = data.find(self._ready_marker)
        if i < 0:
            keep = len(self._ready_marker) - 1
            tail = data[-keep:] if len(data) > keep else data
            self.noise_bytes += len(data) - len(tail)
            self._noise_tail = tail
            return None
        self.noise_bytes += i
        self._noise_tail = b""
        self._started = True
        self._line = bytearray()
        self._kw = b"READY"
        return data[i + len(self._ready_marker):]

    def _classify(self, buf: bytes, pos: int) -> bytes | bool | None:
        """行首的分类：帧关键字（帧行）、False（数据行）或 None（字节不够，暂存）。"""
        for kw, prefix in self._prefixes:
            if buf.startswith(prefix, pos):
                return kw
        head = buf[pos:]
        if len(head) < self._max_prefix and any(p.startswith(head) for _, p in self._prefixes):
            return None
        return False

    def _feed_body(self, buf: bytes, events: list[FrameEvent]) -> None:
        n = len(buf)
        pos = 0
        while pos < n:
            if self._done:
                raise _BadFrame("DONE 之后还有输出")
            if self._line is not None:
                j = buf.find(b"\n", pos)
                end = n if j < 0 else j
                self._line += buf[pos:end]
                if len(self._line) > MAX_FRAME_ARGS:
                    raise _BadFrame("帧行过长")
                if j < 0:
                    return
                pos = j + 1
                args = bytes(self._line)
                self._line = None
                self._at_line_start = True
                self._frame(self._kw, args, events)
                continue
            if self._at_line_start:
                kind = self._classify(buf, pos)
                if kind is None:
                    self._pending = buf[pos:]
                    return
                if kind is not False:
                    assert isinstance(kind, bytes)
                    self._line = bytearray()
                    self._kw = kind
                    pos += len(self._b) + 1 + len(kind)
                    continue
                self._at_line_start = False
            # 行中：数据一直延续到下一个 "LF 加 B"（之后的行首再分类）
            k = buf.find(self._nl_b, pos)
            if k >= 0:
                self._data(buf[pos:k + 1], events)
                pos = k + 1
                self._at_line_start = True
                continue
            # 本块中再没有完整的 "LF 加 B"：只有最后一个行首可能是 B 的前半段，需要暂存
            t = buf.rfind(b"\n", pos)
            if t >= 0 and t + 1 < n and self._b.startswith(buf[t + 1:]):
                self._data(buf[pos:t + 1], events)
                self._pending = buf[t + 1:]
                self._at_line_start = True
                return
            self._data(buf[pos:], events)
            self._at_line_start = buf.endswith(b"\n")
            return

    def _data(self, data: bytes, events: list[FrameEvent]) -> None:
        if not data:
            return
        if self._current is None:
            raise _BadFrame("FILE 与 END 之外有数据")
        events.append(FileData(self._current, data))

    def _frame(self, kw: bytes, args: bytes, events: list[FrameEvent]) -> None:
        if kw == b"READY":
            m = _READY_ARGS.fullmatch(args)
            if self._pgid is not None or m is None:
                raise _BadFrame("READY 行重复或格式不符")
            pgid = int(m.group(1))
            if pgid <= 1:
                raise _BadFrame("进程组号须大于 1")
            self._pgid = pgid
            events.append(FrameReady(pgid))
        elif kw == b"FILE":
            m = _FILE_ARGS.fullmatch(args)
            if m is None:
                raise _BadFrame("FILE 行格式不符")
            index = int(m.group(1))
            if self._current is not None or index != len(self._statuses) or index >= self._file_count:
                raise _BadFrame("FILE 序号或顺序不符")
            self._current = index
            events.append(FileStart(index))
        elif kw == b"END":
            m = _END_ARGS.fullmatch(args)
            if m is None:
                raise _BadFrame("END 行格式不符")
            index = int(m.group(1))
            if self._current is None or index != self._current:
                raise _BadFrame("END 序号或顺序不符")
            try:
                status = parse_file_status(m.group(2), self._grep_stages)
            except ValueError as e:
                raise _BadFrame(f"END 行的结束状态不符：{e}") from None
            self._statuses.append(status)
            self._current = None
            events.append(FileEnd(index, status))
        else:  # DONE
            if args or self._current is not None or len(self._statuses) != self._file_count:
                raise _BadFrame("DONE 行格式或位置不符")
            self._done = True
            events.append(FrameDone())


# ---------------------------------------------------------------------------
# ② 查询执行与终止（design.md「终止、分帧解析与 SSH」）
# ---------------------------------------------------------------------------

# 终止时 kill 会话的时限（秒）；超过即放弃并直接关闭连接（远端由 timeout -s KILL 兜底，9.4）。
KILL_TIMEOUT_S = 5

# 进程组号须 fullmatch [0-9]{1,10} 且大于 1（以 root 执行 kill -- -1 会杀掉全部进程）。
_PGID_PATTERN = re.compile(r"[0-9]{1,10}")
MAX_PGID = 9_999_999_999

# 被再次取消时仍在后台运行的终止任务（保持引用，防止被回收）。
_CLEANUP_TASKS: set[asyncio.Task[bool]] = set()


def build_kill_command(pgid: int) -> str:
    """终止远端进程组的固定命令 ``kill -KILL -- -<pgid>``。

    pgid 取自 READY 行，须为 int、十进制表示 fullmatch [0-9]{1,10} 且大于 1，否则抛出 ValueError。
    """
    if type(pgid) is not int or not 1 < pgid <= MAX_PGID or _PGID_PATTERN.fullmatch(str(pgid)) is None:
        raise ValueError("进程组号须为 2 至 %d 的整数" % MAX_PGID)
    command = "kill -KILL -- -%d" % pgid
    if not is_fixed_command(command):  # 防御
        raise ValueError("命令行含固定命令行之外的字符")
    return command


class ResultSink(Protocol):
    """run_query 的写入接口：接收各序号的 grep 输出与结束状态（文件实现见 results.py，任务 7.1）。

    方法都是同步的，按事件顺序调用，同一时刻至多一个序号处于"已开始、未结束"：

    - ``begin_file(index, file)``：序号 index 开始输出；实现在此记下结果文件的当前偏移。
    - ``write(index, data)``：追加一段原始 grep 输出（不一定在行边界上切分）。
    - ``end_file(index, status)``：序号 index 结束；``status.ok`` 为假时实现截回到 begin_file 时的偏移
      （该文件失败，记入 files_skipped，2.7）。
    - ``interrupt_file(index)``：超时时序号 index 还没结束；实现截到最后一个完整行（保留已收到的完整行，9.3）。

    方法抛出的异常（例如 LogViewerError(RESULT_STORE_FAILED)）会终止远端进程后原样传给调用方。
    run_query 抛出异常（取消、SSH_FAILED、INTERNAL_ERROR 等）时不再调用 sink，写入的内容由调用方丢弃。
    """

    def begin_file(self, index: int, file: QueryFile) -> None: ...

    def write(self, index: int, data: bytes) -> None: ...

    def end_file(self, index: int, status: FileStatus) -> None: ...

    def interrupt_file(self, index: int) -> None: ...


class FileState(StrEnum):
    """一个序号的结果。"""

    READ = "read"                # 读完且成功（END 的 status.ok 为真）
    FAILED = "failed"            # 读取或解压失败，或两个文件都不存在（status.ok 为假，已截回）
    TRUNCATED = "truncated"      # 超时时正在读取，只保留了完整行
    NOT_REACHED = "not_reached"  # 超时前还没开始读取


@dataclass(frozen=True)
class FileOutcome:
    """序号 file.index 的结果；status 为 END 帧的结束状态（没有收到 END 时为 None）。"""

    file: QueryFile
    state: FileState
    status: FileStatus | None = None


@dataclass(frozen=True)
class QueryOutcome:
    """run_query 的结果（正常完成，或超时时已开始读取）。

    - files：按序号排列的各文件结果。
    - done：是否收到 DONE（全部序号都已结束）。
    - timed_out：是否因截止时刻停止读取（摘要的 truncated 为 timeout）；收到 DONE 后才到截止时刻不算超时。
    - pgid：READY 行给出的远端进程组号（没有收到 READY 时为 None）。
    - exit：② 会话的结束情况；超时终止时为 None。
    - killed：是否成功执行了 ``kill -KILL -- -<pgid>``。
    """

    files: tuple[FileOutcome, ...]
    done: bool
    timed_out: bool
    pgid: int | None
    exit: RemoteExit | None
    killed: bool = False

    @property
    def files_read(self) -> tuple[str, ...]:
        """摘要的 files_read：读完的文件与超时时读到一半的文件（按序号）。"""
        return tuple(f.file.name for f in self.files if f.state in (FileState.READ, FileState.TRUNCATED))

    @property
    def files_skipped(self) -> tuple[str, ...]:
        """摘要的 files_skipped（本次执行部分）：读取或解压失败的文件，以及因超时未读的文件。"""
        return tuple(f.file.name for f in self.files if f.state in (FileState.FAILED, FileState.NOT_REACHED))


async def _send_kill(conn: RemoteConnection, pgid: int) -> bool:
    """在 conn 上新开会话执行 kill -KILL -- -<pgid>，限时 KILL_TIMEOUT_S；返回是否成功。"""
    command = build_kill_command(pgid)
    proc: RemoteProcess | None = None
    try:
        async with asyncio.timeout(KILL_TIMEOUT_S):
            proc = await conn.start(command)
            while await proc.read():
                pass
            ex = await proc.wait()
    except TimeoutError:
        logger.warning("终止游戏服 %s 上的进程组 %d 超时，直接关闭连接", conn.host, pgid)
        if proc is not None:
            proc.close()
        return False
    except LogViewerError:
        logger.warning("无法在游戏服 %s 上执行终止命令（进程组 %d），直接关闭连接", conn.host, pgid)
        if proc is not None:
            proc.close()
        return False
    if ex.exit_status != 0:
        logger.warning("游戏服 %s 上的终止命令（进程组 %d）退出码 %s，信号 %s",
                       conn.host, pgid, ex.exit_status, ex.exit_signal)
        return False
    return True


async def _kill_and_close(conn: RemoteConnection, process: RemoteProcess | None, pgid: int | None) -> bool:
    killed = False
    try:
        if pgid is not None and not conn.closed:
            killed = await _send_kill(conn, pgid)
    finally:
        if process is not None:
            process.close()
        await conn.close()
    return killed


async def terminate_query(conn: RemoteConnection, process: RemoteProcess | None, pgid: int | None) -> bool:
    """终止 ② 并关闭连接：pgid 已知时先在同一连接上新开会话执行 kill -KILL -- -<pgid>（限时），
    然后关闭 ② 的会话与连接；pgid 为 None（还没收到 READY）时只关闭。返回是否成功执行了 kill。

    终止在独立任务中执行并以 shield 等待：调用方在终止期间再次被取消时，CancelledError 照常抛出，
    终止仍在后台完成（9.3、10.5、10.6）。
    """
    task = asyncio.ensure_future(_kill_and_close(conn, process, pgid))
    _CLEANUP_TASKS.add(task)
    task.add_done_callback(_CLEANUP_TASKS.discard)
    return await asyncio.shield(task)


async def run_query(
    conn: RemoteConnection,
    query: QueryScript,
    sink: ResultSink,
    *,
    deadline: float | None = None,
) -> QueryOutcome:
    """在 conn 上执行 ② 脚本 query（build_query_script 的结果），把各序号的输出与结束状态交给 sink。

    deadline 为截止时刻（事件循环时间 ``loop.time()``，即 8.2 的开始时刻 + Query_Timeout），None 表示不限。

    - 正常完成（收到 DONE 且会话结束）：返回 QueryOutcome（done 为真），连接留给调用方关闭。
    - 到达截止时刻：终止远端进程组并关闭连接（9.3）。已开始读取（收到过 FILE）时，正在读取的序号交给
      ``sink.interrupt_file`` 截到完整行，返回 timed_out 为真的 QueryOutcome；还没开始读取时抛出
      LogViewerError(TIMEOUT)。
    - 被取消（调用方断开或停机，10.5、10.6）：终止远端进程组并关闭连接，然后抛出 CancelledError。
    - 分帧不符：终止后抛出 LogViewerError(INTERNAL_ERROR)。读取时连接中断：关闭连接后抛出
      LogViewerError(SSH_FAILED)（整台服失败，已写入的内容由调用方丢弃）。sink 抛出的异常：终止后原样抛出。
    - 输出在 DONE 之前结束：没有收到 READY 且退出码为 127（timeout、nice、ionice 或 bash 缺失）时抛出
      LogViewerError(REMOTE_TOOL_UNAVAILABLE)，否则抛出 LogViewerError(SSH_FAILED)；会话已结束，不执行 kill。
    """
    files = query.files
    parser = FrameParser(query.boundary, len(files), grep_stages=query.grep_stages, host=conn.host)
    states = [FileState.NOT_REACHED] * len(files)
    statuses: list[FileStatus | None] = [None] * len(files)
    started = False
    process: RemoteProcess | None = None
    exit: RemoteExit | None = None
    timed_out = False

    def dispatch(events: list[FrameEvent]) -> None:
        nonlocal started
        for ev in events:
            if isinstance(ev, FileData):
                sink.write(ev.index, ev.data)
            elif isinstance(ev, FileStart):
                started = True
                sink.begin_file(ev.index, files[ev.index])
            elif isinstance(ev, FileEnd):
                states[ev.index] = FileState.READ if ev.status.ok else FileState.FAILED
                statuses[ev.index] = ev.status
                sink.end_file(ev.index, ev.status)
            # FrameReady 与 FrameDone 记录在 parser.pgid 与 parser.done 中

    try:
        cm = asyncio.timeout_at(deadline)
        try:
            async with cm:
                process = await conn.start(query.command, query.script)
                while chunk := await process.read():
                    dispatch(parser.feed(chunk))
                dispatch(parser.finish())
                exit = await process.wait()
        except TimeoutError:
            if not cm.expired():
                raise
            timed_out = True
    except BaseException:
        # 取消、分帧不符、连接中断、sink 出错：终止远端进程组（已收到 READY 时）并关闭连接
        await terminate_query(conn, process, parser.pgid)
        raise

    def outcome(killed: bool) -> QueryOutcome:
        return QueryOutcome(
            files=tuple(FileOutcome(f, states[f.index], statuses[f.index]) for f in files),
            done=parser.done, timed_out=timed_out, pgid=parser.pgid, exit=exit, killed=killed,
        )

    if timed_out:
        if parser.done:
            # 输出已完整，只是等待会话结束时到了截止时刻：不算超时；远端随即退出，不再 kill
            timed_out = False
            await terminate_query(conn, process, None)
            return outcome(False)
        killed = await terminate_query(conn, process, parser.pgid)
        logger.warning("游戏服 %s 的查询到达截止时刻，已终止（进程组 %s，kill %s）",
                       conn.host, parser.pgid, "成功" if killed else "未执行或失败")
        if not started:
            raise LogViewerError(ErrorCode.TIMEOUT)
        current = parser.current
        if current is not None:
            states[current] = FileState.TRUNCATED
            sink.interrupt_file(current)
        return outcome(killed)

    assert exit is not None
    if not parser.done:
        if parser.pgid is None and exit.exit_status == 127:
            logger.warning("游戏服 %s 上固定命令行中的程序缺失（退出码 127，没有脚本输出）", conn.host)
            raise LogViewerError(ErrorCode.REMOTE_TOOL_UNAVAILABLE)
        logger.warning("游戏服 %s 的查询输出在 DONE 之前结束（READY %s，退出码 %s，信号 %s）",
                       conn.host, "已收到" if parser.pgid is not None else "未收到", exit.exit_status, exit.exit_signal)
        raise LogViewerError(ErrorCode.SSH_FAILED)
    if exit.exit_status != 0:
        logger.warning("游戏服 %s 的查询已完成但退出码为 %s（信号 %s）", conn.host, exit.exit_status, exit.exit_signal)
    return outcome(False)
