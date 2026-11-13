"""跳板机服务与 Local_MCP 共用的格式规则。

所有字符串规则都用 ``re.fullmatch`` 与显式字符类（``[0-9]``、``[a-z]``）：
Python 的 ``$`` 允许末尾换行，``\\d`` 会匹配非 ASCII 数字，都不能用。
非 str 的值一律视为不符合格式，不做类型转换。
"""

from __future__ import annotations

import re
from typing import Any

# svr_id：tbl_webserver 的 INT UNSIGNED 主键，可查询的取值为 1 至 4294967295。
SVR_ID_MIN = 1
SVR_ID_MAX = 4294967295

# svr_id 的十进制文本形式：无前导零、无符号、无空白。
SVR_ID_PATTERN = re.compile(r"[1-9][0-9]{0,9}")

# Result_Handle：secrets.token_urlsafe(16) 生成，22 个字符、128 位。
RESULT_HANDLE_LENGTH = 22
RESULT_HANDLE_PATTERN = re.compile(r"[A-Za-z0-9_-]{22}")

# Log_Type 的名称（配置 [log_types] 的键）。
LOG_TYPE_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,31}")

# 结果文件名 <svr_id>_<log_type>.log。
RESULT_FILE_PATTERN = re.compile(r"([0-9]{1,10})_([a-z][a-z0-9_]{0,31})\.log")


def _fullmatch(pattern: re.Pattern[str], value: Any) -> re.Match[str] | None:
    if type(value) is not str:
        return None
    return pattern.fullmatch(value)


def is_valid_svr_id(value: Any) -> bool:
    """JSON 原值是否为合法的 svr_id：int（不含 bool）且在 1 至 4294967295 之间。"""
    return type(value) is int and SVR_ID_MIN <= value <= SVR_ID_MAX


def is_valid_svr_id_text(value: Any) -> bool:
    """字符串是否为合法 svr_id 的十进制形式（无前导零，且不超过上限）。"""
    return _fullmatch(SVR_ID_PATTERN, value) is not None and int(value) <= SVR_ID_MAX


def is_valid_result_handle(value: Any) -> bool:
    return _fullmatch(RESULT_HANDLE_PATTERN, value) is not None


def is_valid_log_type_name(value: Any) -> bool:
    return _fullmatch(LOG_TYPE_PATTERN, value) is not None


def is_valid_result_file_name(value: Any) -> bool:
    return _fullmatch(RESULT_FILE_PATTERN, value) is not None


def parse_result_file_name(value: Any) -> tuple[str, str] | None:
    """把结果文件名拆为 (svr_id 文本, log_type)；不符合格式时返回 None。"""
    m = _fullmatch(RESULT_FILE_PATTERN, value)
    if m is None:
        return None
    return m.group(1), m.group(2)


def result_file_name(svr_id: int, log_type: str) -> str:
    """生成结果文件名 <svr_id>_<log_type>.log；参数不合法时抛出 ValueError。"""
    if not is_valid_svr_id(svr_id):
        raise ValueError("svr_id 不合法")
    if not is_valid_log_type_name(log_type):
        raise ValueError("log_type 名称不合法")
    name = "%d_%s.log" % (svr_id, log_type)
    if not is_valid_result_file_name(name):  # 防御：生成的名称必须能通过校验
        raise ValueError("结果文件名不合法")
    return name
