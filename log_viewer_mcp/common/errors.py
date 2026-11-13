"""错误码与对外的中文说明（需求 10.3、10.4）。

对外的错误只包含错误码与中文说明，不包含异常堆栈、数据库或 SSH 的原始错误文本、
内部地址与凭据；详细错误只写入运行日志。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    # Log_MCP_Server
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    SERVER_NOT_FOUND = "SERVER_NOT_FOUND"
    INVALID_SERVER_ADDRESS = "INVALID_SERVER_ADDRESS"
    DB_UNAVAILABLE = "DB_UNAVAILABLE"
    SSH_FAILED = "SSH_FAILED"
    HOST_KEY_MISMATCH = "HOST_KEY_MISMATCH"
    LOG_DIR_UNAVAILABLE = "LOG_DIR_UNAVAILABLE"
    REMOTE_TOOL_UNAVAILABLE = "REMOTE_TOOL_UNAVAILABLE"
    TIMEOUT = "TIMEOUT"
    BUSY = "BUSY"
    RESULT_STORE_FAILED = "RESULT_STORE_FAILED"
    AUDIT_FAILED = "AUDIT_FAILED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    # Local_MCP
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    AUTH_FAILED = "AUTH_FAILED"
    DOWNLOAD_FAILED = "DOWNLOAD_FAILED"


SERVER_ERROR_CODES: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.INVALID_ARGUMENT,
        ErrorCode.SERVER_NOT_FOUND,
        ErrorCode.INVALID_SERVER_ADDRESS,
        ErrorCode.DB_UNAVAILABLE,
        ErrorCode.SSH_FAILED,
        ErrorCode.HOST_KEY_MISMATCH,
        ErrorCode.LOG_DIR_UNAVAILABLE,
        ErrorCode.REMOTE_TOOL_UNAVAILABLE,
        ErrorCode.TIMEOUT,
        ErrorCode.BUSY,
        ErrorCode.RESULT_STORE_FAILED,
        ErrorCode.AUDIT_FAILED,
        ErrorCode.INTERNAL_ERROR,
    }
)

LOCAL_ERROR_CODES: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.SERVICE_UNAVAILABLE,
        ErrorCode.AUTH_FAILED,
        ErrorCode.DOWNLOAD_FAILED,
    }
)

MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.INVALID_ARGUMENT: "参数不合法",
    ErrorCode.SERVER_NOT_FOUND: "管理库中没有该 svr_id",
    ErrorCode.INVALID_SERVER_ADDRESS: "管理库中该服务器的内网 IP 不是合法的 IPv4 地址",
    ErrorCode.DB_UNAVAILABLE: "管理库暂时不可用",
    ErrorCode.SSH_FAILED: "无法登录游戏服或连接中断",
    ErrorCode.HOST_KEY_MISMATCH: "游戏服主机公钥未登记或与登记的不符，已拒绝登录",
    ErrorCode.LOG_DIR_UNAVAILABLE: "游戏服上的日志目录不可读",
    ErrorCode.REMOTE_TOOL_UNAVAILABLE: "游戏服缺少查询所需的命令",
    ErrorCode.TIMEOUT: "查询超时，未能开始读取日志",
    ErrorCode.BUSY: "查询并发已满，请稍后重试",
    ErrorCode.RESULT_STORE_FAILED: "写入结果文件失败",
    ErrorCode.AUDIT_FAILED: "写入审计记录失败，本次结果已删除",
    ErrorCode.INTERNAL_ERROR: "服务内部错误",
    ErrorCode.SERVICE_UNAVAILABLE: "无法连接日志查询服务，或等待结果超时",
    ErrorCode.AUTH_FAILED: "token 无效或已吊销",
    ErrorCode.DOWNLOAD_FAILED: "下载结果文件失败",
}


def message_for(code: ErrorCode) -> str:
    return MESSAGES[code]


def error_dict(code: ErrorCode, message: str | None = None) -> dict[str, str]:
    """对外的错误对象 {"code": …, "message": …}；message 省略时用标准中文说明。"""
    code = ErrorCode(code)
    return {"code": code.value, "message": message if message is not None else MESSAGES[code]}


class LogViewerError(Exception):
    """带错误码的异常。

    message 只能是程序写定的中文说明（例如 INVALID_ARGUMENT 时列出可用的 log_type），
    不得放入原始异常文本、内部地址或凭据；这些只写运行日志。
    """

    def __init__(self, code: ErrorCode, message: str | None = None) -> None:
        self.code = ErrorCode(code)
        self.message = message if message is not None else MESSAGES[self.code]
        super().__init__(f"{self.code.value}: {self.message}")

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code.value, "message": self.message}
