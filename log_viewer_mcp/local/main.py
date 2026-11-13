"""Local_MCP：研发机器上的 stdio MCP 服务（design.md「Local_MCP（local/main.py）」）。

Agent 只调用本服务；本服务把调用转发给跳板机上的 Log_MCP_Server（Streamable HTTP，stateless）。

任务 10.1：

- 启动配置（4.9）：从环境变量读取 LOG_MCP_SERVICE_URL、LOG_MCP_TOKEN（必填）、LOG_MCP_RESULT_DIR
  （默认 query_log，相对路径按启动时的工作目录解析）、LOG_MCP_CALL_TIMEOUT_S（50）与 LOG_MCP_RESULT_TTL_H（24）。
  缺少必填项或取值不合法时在标准错误中说明（不回显取值）并以 2 退出。
- 日志只写标准错误（标准输出是 stdio 的 MCP 通道），格式化后的文字中 token 原文一律替换为 ``***``；
  httpx 与 httpcore 的日志只保留 WARNING 以上，不记录请求头。
- ``LocalMCP``：FastMCP 子类，注册与跳板机相同的两个工具（说明与 schema 取自 common/tool_docs.py；
  log_type 只声明为字符串、不写死取值，上限省略，由跳板机校验）。注册只用于 tools/list。
  覆盖 ``call_tool``：低层服务端以 ``validate_input=False`` 注册 call_tool，Agent 发来的原始参数字典原样
  到达这里，不经 FastMCP 的参数模型，再原样转发给跳板机（未定义的参数、类型都由跳板机按 8.1 校验）。
- 每次调用新建一个 httpx 客户端（带 ``Authorization: Bearer <token>``）与 MCP 客户端会话（跳板机是 stateless），
  initialize 后直接发送 tools/call 请求（不经 ClientSession.call_tool，它在成功后会再发 tools/list 校验输出），
  调用结束即关闭连接（4.1）。
- 只用 httpx 访问 Service_URL，不启动任何外部命令，也不连接 Admin_DB 或游戏服（4.10）。

任务 10.2（4.5、4.6、10.5）：

- ``_forward`` 抛出的异常在 ``call_tool`` 中转为整次错误结果（``_error_for_exception``）：跳板机以 HTTP 401
  拒绝 → AUTH_FAILED（SDK 的任务组把 httpx.HTTPStatusError 包在 ExceptionGroup 中，逐层展开查找）；
  连不上、HTTP 503 等其余状态码与超时 → SERVICE_UNAVAILABLE。
- Service_Call_Timeout 从收到调用起算：``call_tool`` 开头按 anyio 时钟算出 deadline，用 ``anyio.fail_at``
  限定等待结果，并把同一个 deadline 交给 ``_handle_result`` 供下载共用；DELETE 另限 ``DELETE_TIMEOUT_S``。
- 被 Agent 取消（低层服务端取消处理任务）时取消原样抛出，各层 async with 关闭会话与 HTTP 连接。

任务 10.3（4.2、4.3、4.4、4.7）：``_handle_result`` 处理 search_logs 的成功结果（list_servers 与错误结果原样返回）。

- 路径限定：Result_Handle 须 fullmatch ``[A-Za-z0-9_-]{22}``、结果文件名须 fullmatch
  ``[0-9]{1,10}_[a-z][a-z0-9_]{0,31}\\.log``（common/formats.py），拼接出的 ``<handle>/<文件名>`` 与
  ``<文件名>.part`` 再用 realpath 确认位于 Local_Result_Dir 之内；不符时不触碰文件系统、不发请求，
  该 svr_id 标 DOWNLOAD_FAILED。handle 本身不合法时所有成功的 svr_id 都标 DOWNLOAD_FAILED，也不发 DELETE。
- 下载：逐个文件 ``GET /results/<handle>/<文件名>``，收到 200 后才创建 ``<Local_Result_Dir>/<handle>/``，流式写入
  ``<文件名>.part``，字节数与摘要的 bytes 一致后 os.replace 为正式文件；状态码不是 200、连接出错、字节数不符
  或写入失败时删除 .part，该 svr_id 改为 failed 与 DOWNLOAD_FAILED（bytes、lines 记 0），其余文件照常处理。
  下载与等待结果共用 deadline，到时尚未完成的文件都标 DOWNLOAD_FAILED。
- 下载全部结束（无论成功与否）后 ``DELETE /results/<handle>``，另限 ``DELETE_TIMEOUT_S`` 秒；删除失败只写
  运行日志，不影响返回给 Agent 的摘要（跳板机按 Result_TTL 兜底清理）。
- 返回给 Agent 的摘要与跳板机的结构相同，只把每个 svr_id 的 file 换成 local_path（本地绝对路径，未下载时为
  null）；不含下载地址，也不含日志内容。
- 被 Agent 取消时删除正在写入的 .part，取消原样抛出（不再发 DELETE，跳板机按 Result_TTL 清理）。

任务 10.5（4.8）：``cleanup_expired_results`` 删除 Local_Result_Dir 中过期的结果目录。

- 启动时在 ``main`` 中（``server.run`` 之前，尚无事件循环，同步执行）、每次 search_logs 开始时在 ``call_tool``
  中（经 anyio.to_thread 放到工作线程，不阻塞事件循环；耗时计入 Service_Call_Timeout）执行。list_servers
  不产生结果目录，不触发清理。
- 只看 Local_Result_Dir 的直接子项：名字须 fullmatch Result_Handle 格式，lstat（不跟随符号链接）确认是真实
  目录（不是符号链接，Windows 上也不是 junction 等重解析点），且修改时间早于 Local_Result_TTL；其余一律不动。
  目录用 shutil.rmtree 删除，其中的符号链接只删除链接本身、不跟随。Local_Result_Dir 不存在时什么也不做（不创建）。
- 出错（列目录、lstat、删除失败）只写运行日志（路径与异常类型，不含 token），跳过该项继续，不影响启动与查询。
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import stat
import sys
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import anyio
import httpx
import mcp.types as types
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.tools import Tool
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from log_viewer_mcp.common.errors import ErrorCode, LogViewerError
from log_viewer_mcp.common.formats import is_valid_result_file_name, is_valid_result_handle
from log_viewer_mcp.common.tool_docs import (
    LIST_SERVERS,
    LIST_SERVERS_DESCRIPTION,
    LIST_SERVERS_TITLE,
    SEARCH_LOGS,
    SEARCH_LOGS_DESCRIPTION,
    SEARCH_LOGS_TITLE,
    list_servers_input_schema,
    search_logs_input_schema,
)

__all__ = [
    "DEFAULT_CALL_TIMEOUT_S",
    "DEFAULT_RESULT_DIR",
    "DEFAULT_RESULT_TTL_H",
    "ENV_CALL_TIMEOUT_S",
    "ENV_RESULT_DIR",
    "ENV_RESULT_TTL_H",
    "ENV_SERVICE_URL",
    "ENV_TOKEN",
    "SERVER_NAME",
    "LocalMCP",
    "LocalSettings",
    "SettingsError",
    "build_tools",
    "cleanup_expired_results",
    "configure_logging",
    "load_settings",
    "main",
]

logger = logging.getLogger(__name__)

SERVER_NAME = "log-viewer-mcp-local"

ENV_SERVICE_URL = "LOG_MCP_SERVICE_URL"
ENV_TOKEN = "LOG_MCP_TOKEN"
ENV_RESULT_DIR = "LOG_MCP_RESULT_DIR"
ENV_CALL_TIMEOUT_S = "LOG_MCP_CALL_TIMEOUT_S"
ENV_RESULT_TTL_H = "LOG_MCP_RESULT_TTL_H"

DEFAULT_RESULT_DIR = "query_log"
DEFAULT_CALL_TIMEOUT_S = 50.0
DEFAULT_RESULT_TTL_H = 24.0

MCP_PATH = "/mcp"
RESULTS_PATH = "/results"

# 等待结果与下载共用 Service_Call_Timeout（从收到调用起算）；之后的 DELETE 另限 5 秒（任务 10.3 使用），
# 保证在客户端约 60 秒的工具超时内答复。
DELETE_TIMEOUT_S = 5.0

HTTP_UNAUTHORIZED = 401
HTTP_OK = 200
HTTP_NO_CONTENT = 204

PART_SUFFIX = ".part"

# .part 只写不读、截断重写；POSIX 上不跟随符号链接（Windows 没有 O_NOFOLLOW，靠 realpath 检查）。
_PART_OPEN_FLAGS = (
    os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
)
_PART_MODE = 0o600

REDACTED = "***"

# token 放进 Authorization 头：可见 ASCII、不含空白（与跳板机认证中间件接受的格式一致）。
_TOKEN_PATTERN = re.compile(r"[\x21-\x7e]+")

_READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=True)


# ---------------------------------------------------------------------------
# 启动配置（4.9）
# ---------------------------------------------------------------------------


class SettingsError(Exception):
    """环境变量缺失或不合法；说明中不含任何环境变量的取值。"""

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__("；".join(self.problems))


@dataclass(frozen=True)
class LocalSettings:
    service_url: str  # 基地址，不以 "/" 结尾
    token: str
    result_dir: Path  # 绝对路径
    call_timeout_s: float
    result_ttl_h: float

    @property
    def mcp_url(self) -> str:
        return self.service_url + MCP_PATH

    @property
    def results_url(self) -> str:
        return self.service_url + RESULTS_PATH

    def __repr__(self) -> str:  # 不让 token 出现在 repr 中
        return (
            f"LocalSettings(service_url={self.service_url!r}, token={REDACTED!r}, result_dir={str(self.result_dir)!r}, "
            f"call_timeout_s={self.call_timeout_s!r}, result_ttl_h={self.result_ttl_h!r})"
        )


def _parse_service_url(value: str) -> str | None:
    """校验 http(s) 基地址并去掉末尾的 "/"；不合法时返回 None。"""
    try:
        parts = urlsplit(value)
        hostname = parts.hostname
        parts.port  # 端口不合法时抛出 ValueError
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not hostname:
        return None
    if parts.username is not None or parts.password is not None or parts.query or parts.fragment:
        return None
    if any(c.isspace() for c in value):
        return None
    return value.rstrip("/")


def _parse_positive(environ: Mapping[str, str], name: str, default: float, problems: list[str]) -> float:
    raw = environ.get(name, "")
    if raw == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value <= 0:
        problems.append(f"环境变量 {name} 须为正数")
        return default
    return value


def load_settings(environ: Mapping[str, str] | None = None, cwd: str | os.PathLike[str] | None = None) -> LocalSettings:
    """从环境变量读取配置；任一项缺失或不合法时抛出 SettingsError（列出全部问题，不含取值）。

    空字符串视为未设置。LOG_MCP_RESULT_DIR 的相对路径按 cwd（默认为当前工作目录）解析为绝对路径。
    """
    env = os.environ if environ is None else environ
    problems: list[str] = []

    raw_url = env.get(ENV_SERVICE_URL, "")
    service_url: str | None = None
    if raw_url == "":
        problems.append(f"缺少环境变量 {ENV_SERVICE_URL}（跳板机服务的基地址，例如 http://<跳板机地址>:8999）")
    else:
        service_url = _parse_service_url(raw_url)
        if service_url is None:
            problems.append(
                f"环境变量 {ENV_SERVICE_URL} 须为 http:// 或 https:// 开头的基地址，不含用户名、密码、查询串与空白"
            )

    token = env.get(ENV_TOKEN, "")
    if token == "":
        problems.append(f"缺少环境变量 {ENV_TOKEN}（由跳板机上的 Token_CLI 生成）")
    elif _TOKEN_PATTERN.fullmatch(token) is None:
        problems.append(f"环境变量 {ENV_TOKEN} 含空白或非 ASCII 字符")

    raw_dir = env.get(ENV_RESULT_DIR, "") or DEFAULT_RESULT_DIR
    base = Path(os.getcwd() if cwd is None else cwd)
    result_dir = Path(os.path.abspath(base / raw_dir))

    call_timeout_s = _parse_positive(env, ENV_CALL_TIMEOUT_S, DEFAULT_CALL_TIMEOUT_S, problems)
    result_ttl_h = _parse_positive(env, ENV_RESULT_TTL_H, DEFAULT_RESULT_TTL_H, problems)

    if problems:
        raise SettingsError(problems)
    assert service_url is not None
    return LocalSettings(
        service_url=service_url,
        token=token,
        result_dir=result_dir,
        call_timeout_s=call_timeout_s,
        result_ttl_h=result_ttl_h,
    )


# ---------------------------------------------------------------------------
# 日志（只写标准错误，不含 token）
# ---------------------------------------------------------------------------


class _RedactingFormatter(logging.Formatter):
    """格式化后（含异常堆栈）把 secrets 中的每个值替换为 ***。"""

    def __init__(self, fmt: str, secrets: tuple[str, ...]) -> None:
        super().__init__(fmt)
        self._secrets = tuple(s for s in secrets if s)

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        return text


def configure_logging(token: str | None = None, *, stream: Any = None) -> None:
    """运行日志写标准错误（标准输出是 MCP 通道）；token 原文替换为 ***；httpx、httpcore 只记 WARNING 以上。

    须在构造 FastMCP 之前调用（FastMCP 的 basicConfig 在根 logger 已有处理器时不生效）。
    """
    handler = logging.StreamHandler(sys.stderr if stream is None else stream)
    handler.setFormatter(
        _RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s", (token,) if token else ())
    )
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# 工具与转发（4.1）
# ---------------------------------------------------------------------------


async def _not_called() -> None:  # pragma: no cover - call_tool 不经 FastMCP 的工具函数
    raise RuntimeError("工具由 LocalMCP.call_tool 转发")


def _tool(name: str, title: str, description: str, parameters: dict[str, Any]) -> Tool:
    tool = Tool.from_function(
        _not_called, name=name, title=title, description=description, annotations=_READ_ONLY, structured_output=False,
    )
    return tool.model_copy(update={"parameters": parameters})


def build_tools() -> list[Tool]:
    """与跳板机相同的两个工具；log_type 只声明为字符串，上限省略（由跳板机校验）。"""
    return [
        _tool(SEARCH_LOGS, SEARCH_LOGS_TITLE, SEARCH_LOGS_DESCRIPTION, search_logs_input_schema()),
        _tool(LIST_SERVERS, LIST_SERVERS_TITLE, LIST_SERVERS_DESCRIPTION, list_servers_input_schema()),
    ]


def _error_result(error: LogViewerError) -> CallToolResult:
    payload = {"error": error.to_dict()}
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
        structuredContent=payload,
        isError=True,
    )


def _leaf_exceptions(exc: BaseException) -> Iterator[BaseException]:
    """展开（可能嵌套的）ExceptionGroup：SDK 的任务组会把 httpx.HTTPStatusError 包在其中。"""
    if isinstance(exc, BaseExceptionGroup):
        for inner in exc.exceptions:
            yield from _leaf_exceptions(inner)
    else:
        yield exc


def _is_auth_rejection(exc: BaseException) -> bool:
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == HTTP_UNAUTHORIZED


def _error_for_exception(exc: BaseException) -> LogViewerError:
    """转发失败的异常对应的整次错误（4.5、4.6）。

    跳板机以 HTTP 401 拒绝 → AUTH_FAILED（token 无效或已吊销）；其余（连不上、HTTP 503 等状态码、
    超过 Service_Call_Timeout、协议错误）→ SERVICE_UNAVAILABLE。
    """
    if any(_is_auth_rejection(e) for e in _leaf_exceptions(exc)):
        return LogViewerError(ErrorCode.AUTH_FAILED)
    return LogViewerError(ErrorCode.SERVICE_UNAVAILABLE)


# ---------------------------------------------------------------------------
# 下载、DELETE 与路径限定（4.2、4.3、4.4、4.7）
# ---------------------------------------------------------------------------


def _copy_result(result: CallToolResult) -> CallToolResult:
    return CallToolResult(content=result.content, structuredContent=result.structuredContent, isError=result.isError)


def _json_result(payload: dict[str, Any]) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
        structuredContent=payload,
        isError=False,
    )


def _summary_of(result: CallToolResult) -> dict[str, Any] | None:
    """跳板机的摘要：优先取 structuredContent，否则解析第一段文本内容；不是 JSON 对象时返回 None。"""
    if isinstance(result.structuredContent, dict):
        return result.structuredContent
    for content in result.content:
        if isinstance(content, TextContent):
            try:
                data = json.loads(content.text)
            except ValueError:
                return None
            return data if isinstance(data, dict) else None
    return None


def _is_within(base: str, path: str) -> bool:
    """path 是否位于 base 之内（不等于 base）；两者都应是 realpath。不同盘符等无法比较时视为不在其内。"""
    base_n, path_n = os.path.normcase(base), os.path.normcase(path)
    try:
        return path_n != base_n and os.path.commonpath([base_n, path_n]) == base_n
    except ValueError:
        return False


@dataclass(frozen=True)
class _Download:
    index: int  # 在 servers 列表中的位置
    svr_id: Any
    file_name: str
    expected_bytes: int
    target: str  # <Local_Result_Dir>/<handle>/<文件名>，绝对路径


def _local_entry(entry: dict[str, Any], local_path: str | None) -> dict[str, Any]:
    """把一个 svr_id 的摘要中的 file 换成 local_path（保持字段顺序）；没有 file 字段时补在末尾。"""
    out: dict[str, Any] = {}
    for key, value in entry.items():
        if key == "file":
            out["local_path"] = local_path
        else:
            out[key] = value
    out.setdefault("local_path", local_path)
    return out


def _download_failed_entry(entry: dict[str, Any]) -> dict[str, Any]:
    out = _local_entry(entry, None)
    out["status"] = "failed"
    out["error"] = LogViewerError(ErrorCode.DOWNLOAD_FAILED).to_dict()
    out["bytes"] = 0
    out["lines"] = 0
    return out


def _unlink_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("删除未完成的下载文件失败（%s）：%s", type(exc).__name__, path)


# ---------------------------------------------------------------------------
# 本地结果清理（4.8）
# ---------------------------------------------------------------------------

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_SECONDS_PER_HOUR = 3600.0


def _is_reparse_point(st: os.stat_result) -> bool:
    """Windows 上的符号链接或 junction（lstat 可能把 junction 报告为目录）；POSIX 上恒为假。"""
    return bool(getattr(st, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


def _ignore_missing(func: Any, path: str, exc: Any) -> None:
    # shutil.rmtree 的错误回调：并发删除造成的"已不存在"忽略，其余错误照常抛出。
    e = exc if isinstance(exc, BaseException) else exc[1]
    if isinstance(e, FileNotFoundError):
        return
    raise e


def _rmtree(path: str) -> None:
    """删除目录树；其中的符号链接与 junction 只删除链接本身，不跟随（shutil.rmtree 的行为）。"""
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_ignore_missing)
    else:  # pragma: no cover - 3.11
        shutil.rmtree(path, onerror=_ignore_missing)


def cleanup_expired_results(
    result_dir: str | os.PathLike[str], ttl_h: float, *, now: float | None = None
) -> list[str]:
    """删除 result_dir 中修改时间早于 ttl_h 小时的结果目录，返回已删除的目录名（4.8）。

    只处理名字 fullmatch Result_Handle 格式、lstat 为真实目录（不是符号链接或重解析点）的直接子项。
    result_dir 不存在时什么也不做。出错只写运行日志并跳过该项，不抛出 OSError。now 为 Unix 时间，默认取当前时间。
    """
    base = os.fspath(result_dir)
    cutoff = (time.time() if now is None else now) - ttl_h * _SECONDS_PER_HOUR
    try:
        names = sorted(os.listdir(base))
    except FileNotFoundError:
        return []
    except OSError as exc:
        logger.warning("无法列出结果目录（%s）：%s", type(exc).__name__, base)
        return []
    removed: list[str] = []
    for name in names:
        if not is_valid_result_handle(name):
            continue
        path = os.path.join(base, name)
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.warning("无法检查过期结果目录（%s）：%s", type(exc).__name__, path)
            continue
        if not stat.S_ISDIR(st.st_mode) or _is_reparse_point(st) or st.st_mtime >= cutoff:
            continue
        try:
            _rmtree(path)
        except OSError as exc:
            logger.warning("删除过期结果目录失败（%s）：%s", type(exc).__name__, path)
            continue
        removed.append(name)
    if removed:
        logger.info("已删除 %d 个过期结果目录（超过 %s 小时）", len(removed), ttl_h)
    return removed


class LocalMCP(FastMCP):
    """stdio 上的 Local_MCP：call_tool 把原始参数字典原样转发给跳板机。

    transport 只供测试注入（例如 httpx.ASGITransport、httpx.MockTransport）；默认用 httpx 的网络传输。
    """

    def __init__(self, settings: LocalSettings, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__(name=SERVER_NAME, tools=build_tools())
        self.local_settings = settings
        self._transport = transport

    def http_client(self) -> httpx.AsyncClient:
        """新建一个访问跳板机的 httpx 客户端：带 Authorization 头，不跟随重定向（头不会被带到别处）。"""
        return httpx.AsyncClient(
            headers={"Authorization": f"Bearer {self.local_settings.token}"},
            timeout=httpx.Timeout(self.local_settings.call_timeout_s),
            follow_redirects=False,
            transport=self._transport,
        )

    def cleanup_results(self) -> list[str]:
        """删除 Local_Result_Dir 中过期的结果目录（4.8）；任何异常都只写运行日志，不影响启动与查询。"""
        try:
            return cleanup_expired_results(self.local_settings.result_dir, self.local_settings.result_ttl_h)
        except Exception:
            logger.warning("清理过期结果目录失败", exc_info=True)
            return []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> CallToolResult:  # type: ignore[override]
        """转发一次工具调用；arguments 是 Agent 发来的原始参数字典。

        Service_Call_Timeout 从收到调用起算：deadline 在这里算出（anyio 时钟），等待跳板机的结果与
        之后的下载（任务 10.3，经 ``_handle_result``）共用同一个 deadline。search_logs 开始时先在工作线程中
        清理过期结果目录（4.8）。

        转发失败时返回整次错误结果（isError 为真，内容为 {"error": {"code": …, "message": …}}），
        原始异常只写运行日志（token 由日志格式化器替换）。被 Agent 取消时取消原样抛出，不转为错误结果；
        各层 async with 随之关闭会话与 HTTP 连接，断开传到跳板机（10.5）。
        """
        deadline = anyio.current_time() + self.local_settings.call_timeout_s
        if name == SEARCH_LOGS:
            await anyio.to_thread.run_sync(self.cleanup_results)
        try:
            with anyio.fail_at(deadline):
                result = await self._forward(name, arguments)
        except TimeoutError:
            logger.warning("转发 %s 超时：%s 秒内没有收到跳板机的结果", name, self.local_settings.call_timeout_s)
            return _error_result(LogViewerError(ErrorCode.SERVICE_UNAVAILABLE))
        except Exception as exc:
            error = _error_for_exception(exc)
            logger.warning("转发 %s 失败（%s）", name, error.code.value, exc_info=True)
            return _error_result(error)
        return await self._handle_result(name, result, deadline)

    async def _forward(self, name: str, arguments: dict[str, Any]) -> CallToolResult:
        """新建客户端会话，initialize 后发送 tools/call，返回跳板机的结果；会话随即关闭。

        时限由调用方的取消域控制（``call_tool`` 中的 deadline）。
        """
        request = types.ClientRequest(
            types.CallToolRequest(params=types.CallToolRequestParams(name=name, arguments=arguments))
        )
        async with self.http_client() as http:
            async with streamable_http_client(self.local_settings.mcp_url, http_client=http) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await session.send_request(request, CallToolResult)

    async def _handle_result(self, name: str, result: CallToolResult, deadline: float) -> CallToolResult:
        """处理跳板机的结果：search_logs 成功时下载结果文件、删除跳板机上的结果并改写摘要（4.2、4.3、4.4）。

        list_servers 与整次错误的结果原样返回。下载在截止时刻为 deadline 的取消域内进行（与等待结果共用
        Service_Call_Timeout），之后的 DELETE 另限 ``DELETE_TIMEOUT_S`` 秒。
        """
        if name != SEARCH_LOGS or result.isError:
            return _copy_result(result)
        summary = _summary_of(result)
        servers = summary.get("servers") if summary is not None else None
        if summary is None or not isinstance(servers, list):
            logger.warning("跳板机返回的 search_logs 摘要格式不符，无法下载结果")
            return _error_result(LogViewerError(ErrorCode.SERVICE_UNAVAILABLE))

        handle = summary.get("result_handle")
        handle_ok = is_valid_result_handle(handle)
        if not handle_ok:
            logger.warning("跳板机返回的 Result_Handle 格式不符，不下载结果")

        downloads: list[_Download] = []
        for index, entry in enumerate(servers):
            if isinstance(entry, dict) and entry.get("status") == "ok" and handle_ok:
                download = self._plan_download(index, handle, entry)
                if download is not None:
                    downloads.append(download)

        done: set[int] = set()
        if handle_ok:
            async with self.http_client() as http:
                with anyio.CancelScope(deadline=deadline) as scope:
                    for download in downloads:
                        if await self._download(http, handle, download):
                            done.add(download.index)
                if scope.cancelled_caught:
                    logger.warning("下载结果超时：%s 秒内未完成，未完成的文件标为 DOWNLOAD_FAILED",
                                   self.local_settings.call_timeout_s)
                await self._delete_results(http, handle)

        targets = {d.index: d.target for d in downloads}
        local_servers: list[Any] = []
        for index, entry in enumerate(servers):
            if not isinstance(entry, dict):
                local_servers.append(entry)
            elif entry.get("status") != "ok":
                local_servers.append(_local_entry(entry, None))
            elif index in done:
                local_servers.append(_local_entry(entry, targets[index]))
            else:
                local_servers.append(_download_failed_entry(entry))
        return _json_result({**summary, "servers": local_servers})

    def _plan_download(self, index: int, handle: str, entry: dict[str, Any]) -> _Download | None:
        """按 4.7 校验文件名与字节数，并确认目标与 .part 的 realpath 位于 Local_Result_Dir 之内；不符时返回 None。

        只读取路径信息（realpath），不创建、不写入任何文件。handle 已由调用方校验。
        """
        svr_id = entry.get("svr_id")
        file_name = entry.get("file")
        expected = entry.get("bytes")
        if not is_valid_result_file_name(file_name):
            logger.warning("svr_id %s 的结果文件名格式不符，不下载", svr_id)
            return None
        if type(expected) is not int or expected < 0:
            logger.warning("svr_id %s 的结果字节数不合法，不下载", svr_id)
            return None
        base = os.path.realpath(self.local_settings.result_dir)
        target = os.path.join(self.local_settings.result_dir, handle, file_name)
        for path in (target, target + PART_SUFFIX):
            if not _is_within(base, os.path.realpath(path)):
                logger.warning("svr_id %s 的结果路径不在结果目录之内，不下载", svr_id)
                return None
        return _Download(index, svr_id, file_name, expected, target)

    async def _download(self, http: httpx.AsyncClient, handle: str, download: _Download) -> bool:
        """下载一个结果文件：经 .part 写入，字节数一致后改名为正式文件。失败时删除 .part 并返回 False。

        取消（超时或 Agent 取消）时同样删除 .part，取消继续抛出。
        """
        url = f"{self.local_settings.results_url}/{handle}/{download.file_name}"
        part = download.target + PART_SUFFIX
        created = False
        finished = False
        try:
            async with http.stream("GET", url) as response:
                if response.status_code != HTTP_OK:
                    logger.warning("下载 svr_id %s 的结果失败：HTTP %d", download.svr_id, response.status_code)
                    return False
                os.makedirs(os.path.dirname(download.target), exist_ok=True)
                received = 0
                fd = os.open(part, _PART_OPEN_FLAGS, _PART_MODE)
                created = True
                with open(fd, "wb") as f:
                    async for chunk in response.aiter_bytes():
                        received += len(chunk)
                        if received > download.expected_bytes:
                            break
                        f.write(chunk)
            if received != download.expected_bytes:
                logger.warning("svr_id %s 的结果字节数与摘要不符（摘要 %d，收到%s %d）", download.svr_id,
                               download.expected_bytes, "超过" if received > download.expected_bytes else "",
                               received)
                return False
            os.replace(part, download.target)
            finished = True
            return True
        except (httpx.HTTPError, OSError) as exc:
            logger.warning("下载 svr_id %s 的结果失败（%s）", download.svr_id, type(exc).__name__, exc_info=True)
            return False
        finally:
            if created and not finished:
                _unlink_quietly(part)

    async def _delete_results(self, http: httpx.AsyncClient, handle: str) -> None:
        """DELETE /results/<handle>（4.3），限时 DELETE_TIMEOUT_S 秒；失败只写运行日志。"""
        url = f"{self.local_settings.results_url}/{handle}"
        with anyio.move_on_after(DELETE_TIMEOUT_S) as scope:
            try:
                response = await http.delete(url)
            except httpx.HTTPError as exc:
                logger.warning("删除跳板机上的结果失败（%s）", type(exc).__name__, exc_info=True)
                return
            if response.status_code != HTTP_NO_CONTENT:
                logger.warning("删除跳板机上的结果失败：HTTP %d", response.status_code)
        if scope.cancelled_caught:
            logger.warning("删除跳板机上的结果超时（%s 秒）", DELETE_TIMEOUT_S)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def _startup_failed(message: object) -> int:
    print(f"log-viewer-mcp（Local_MCP）启动失败：{message}", file=sys.stderr)
    return 2


def main(environ: Mapping[str, str] | None = None) -> int:
    """`log-viewer-mcp-local`（或 `python -m log_viewer_mcp.local`）的入口：读取环境变量，以 stdio 运行 Local_MCP。"""
    try:
        settings = load_settings(environ)
    except SettingsError as e:
        return _startup_failed(e)
    configure_logging(settings.token)
    server = LocalMCP(settings)
    logger.info("Local_MCP 启动：跳板机 %s，结果目录 %s", settings.service_url, settings.result_dir)
    server.cleanup_results()
    server.run(transport="stdio")
    return 0
