"""search_logs 与 list_servers 的工具说明与参数 schema（需求 1.1、1.8；design.md「工具接口」）。

跳板机服务（server/app.py）、Local_MCP（任务 10.1）与 SKILL.md（任务 12.3）用同一份文字。
schema 只用于 tools/list 展示给 Agent；实际校验由跳板机的 validate.py 按原始参数字典进行，
不依赖 schema（FastMCP 的参数模型会做类型转换，与 8.1 冲突）。

- 跳板机：``search_logs_input_schema(log_types=配置中的键, max_servers=…, …)``，log_type 带 enum，
  个数、长度与上下文行数带配置中的上限。
- Local_MCP：``search_logs_input_schema()``，log_type 声明为字符串、不写死取值，上限省略，由跳板机校验。

本模块只用标准库，Local_MCP 可以直接导入。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

__all__ = [
    "LIST_SERVERS",
    "LIST_SERVERS_DESCRIPTION",
    "LIST_SERVERS_TITLE",
    "SEARCH_LOGS",
    "SEARCH_LOGS_DESCRIPTION",
    "SEARCH_LOGS_TITLE",
    "list_servers_input_schema",
    "search_logs_input_schema",
]

SEARCH_LOGS = "search_logs"
LIST_SERVERS = "list_servers"
SEARCH_LOGS_TITLE = "查询游戏服日志"
LIST_SERVERS_TITLE = "列出游戏服"

# 秒级 Unix 时间戳的取值范围（1.3），与 validate.py 一致。
_TIMESTAMP_MIN = 0
_TIMESTAMP_MAX = 4102444799
_SVR_ID_MIN = 1
_SVR_ID_MAX = 4294967295

SEARCH_LOGS_DESCRIPTION = """\
按 svr_id、日志类型与时间查询游戏服的 SFS2X 日志。结果写成文件（每个 svr_id 一个 .log 文件）并返回本地路径，\
响应中只有摘要，没有日志内容；拿到路径后在本地结果文件中 grep 或读取。svr_id、log_type 或服务器时区不清楚时\
先调用 list_servers。

时间参数：
- at_time、start_time、end_time 是秒级 Unix 时间戳（整数），与时区无关；字符串、小数与毫秒级的值会被拒绝。
- 只给 at_time：查询其前后各 5 分钟；只给 start_time：查到当前时刻；同时给 start_time 与 end_time：查 \
[start_time, end_time)；都不给：查最近 1 小时。at_time 不能与另两个同时给，不能只给 end_time，单次跨度不超过 24 小时\
（以上为默认配置）。

服务器时间：日志行首与文件名里的时间是游戏服的服务器时间，时区以跳板机配置为准，见 list_servers 结果与摘要中的 \
server_timezone（默认 GMT-2，即 UTC−2），与研发本地时间不同。例如服务器时间 GMT-2（UTC−2）下，日志里的 \
2026-10-06 21:13:05 是 2026-10-06T23:13:05Z，即时间戳 1791328385。换算请用命令，不要心算，例如 \
python -c "from datetime import datetime,timezone,timedelta as d; \
print(int(datetime(2026,10,6,21,13,5,tzinfo=timezone(d(hours=-2))).timestamp()))"（hours 按 server_timezone 填写）；\
先弄清用户给的是服务器时间还是本地时间，向用户报告时间时注明时区。摘要中的时间同时给出 Unix 时间戳与服务器时间。

log_type：取值以跳板机配置为准，list_servers 结果中的 log_types 列出可用取值与目录。默认配置为：
- normal：logs 目录，未单独分流的日志，如 CMD、PUSH、EVENT 与异常；
- login：logpushinit 目录，PUSHINIT logger，登录时的 init 推送；
- analysis：loganalys 目录，ANALYSISLOG logger，分析事件；
- pulsar：polsarlog 目录，Pulsar 日志发送服务（LogSendService）。
分流出去的日志不会出现在 normal 里。传入未配置的值会返回 INVALID_ARGUMENT，错误说明中列出可用取值。

日志行格式：yyyy-MM-dd HH:mm:ss,SSS | 级别 | 线程 | logger | 消息，例如 \
2026-10-06 21:13:05,123 | INFO  | <线程> | CMD | <SERVER_ID> | CMD | <请求名> | <uid> | …；\
消息部分由业务代码决定，要 grep 的关键字先看代码确定。

过滤（grep 语义）：先去掉匹配任一 exclude_patterns 的行，再只保留至少匹配一个 include_patterns 的行，两者都不给时保留全部行；\
regex 为 true 时按扩展正则（grep -E，不支持反向引用 \\1–\\9）匹配，否则按字面匹配；ignore_case 忽略大小写；\
context_before、context_after 对应 grep -B、-A，设置后结果中保留 "--" 分隔行。模式只作为 grep 的匹配内容，\
不会被当作命令、选项或路径执行；模式不能含换行、制表符等控制字符。

结果精度：按文件名中的时间选文件（文件按分钟或小时切割，终点多取 1 分钟），以文件为时间精度，结果可能含 Time_Range 之外的行，\
需要时在本地按行首的服务器时间再过滤。异常堆栈是紧随其后、不带时间的多行，需要时用 context_after 取出\
（上限 Max_Context_Lines，默认 100）。

摘要中每个 svr_id 给出状态（ok 或 failed 与错误码）、结果文件、字节数与行数、已读取与已跳过的文件、truncated\
（none、timeout 或 scan_limit：因超时或扫描上限截断）；Time_Range 内没有该类型日志文件时给出 available_range\
（现有日志的最早与最晚时间）。"""

LIST_SERVERS_DESCRIPTION = """\
列出可查询的游戏服：tbl_webserver 中全部服务器的 svr_id、svr_name、zone、ip_inner 与 is_test（含测试服），\
按 svr_id 排序；同时给出 server_timezone（服务器时区，例如 GMT-2）与 log_types（可用的 log_type 及其日志目录）。\
不带参数。用于确定 search_logs 的 svr_ids、log_type 与时间换算。"""


def _maybe(schema: dict[str, Any], key: str, value: int | None) -> dict[str, Any]:
    if value is not None:
        schema[key] = value
    return schema


def _patterns_schema(description: str, max_patterns: int | None, max_pattern_length: int | None) -> dict[str, Any]:
    item = _maybe({"type": "string", "minLength": 1}, "maxLength", max_pattern_length)
    return _maybe(
        {"type": "array", "items": item, "minItems": 1, "description": description},
        "maxItems",
        max_patterns,
    )


def _timestamp_schema(description: str) -> dict[str, Any]:
    return {"type": "integer", "minimum": _TIMESTAMP_MIN, "maximum": _TIMESTAMP_MAX, "description": description}


def _context_schema(description: str, max_context_lines: int | None) -> dict[str, Any]:
    return _maybe(
        {"type": "integer", "minimum": 0, "default": 0, "description": description},
        "maximum",
        max_context_lines,
    )


def search_logs_input_schema(
    *,
    log_types: Sequence[str] | None = None,
    max_servers: int | None = None,
    max_patterns: int | None = None,
    max_pattern_length: int | None = None,
    max_context_lines: int | None = None,
) -> dict[str, Any]:
    """search_logs 的参数 JSON Schema（1.1）。

    log_types 给出时 log_type 带 enum（跳板机按配置），否则只声明为字符串（Local_MCP）。
    各上限给出时写入 maxItems、maxLength、maximum，否则省略。可选参数在 schema 中不声明 null；
    跳板机的校验仍把 null 视为未提供。
    """
    log_type: dict[str, Any] = {"type": "string", "description": "日志类型：normal、login、analysis、pulsar 等，以跳板机配置为准"}
    if log_types is not None:
        log_type["enum"] = list(log_types)
        log_type["description"] = "日志类型，可用取值：" + "、".join(log_types)
    svr_ids = _maybe(
        {
            "type": "array",
            "items": {"type": "integer", "minimum": _SVR_ID_MIN, "maximum": _SVR_ID_MAX},
            "minItems": 1,
            "description": "要查询的游戏服 svr_id 列表（整数，重复值只处理一次）",
        },
        "maxItems",
        max_servers,
    )
    return {
        "type": "object",
        "properties": {
            "svr_ids": svr_ids,
            "log_type": log_type,
            "at_time": _timestamp_schema("时间点（秒级 Unix 时间戳），查询其前后各 Point_Window；不能与 start_time、end_time 同时给"),
            "start_time": _timestamp_schema("起点（秒级 Unix 时间戳，含）"),
            "end_time": _timestamp_schema("终点（秒级 Unix 时间戳，不含）；须与 start_time 一起给"),
            "include_patterns": _patterns_schema(
                "包含模式：只保留至少匹配其中一个的行（grep -e p1 -e p2）", max_patterns, max_pattern_length
            ),
            "exclude_patterns": _patterns_schema(
                "排除模式：先去掉匹配其中任一个的行（grep -v）", max_patterns, max_pattern_length
            ),
            "regex": {"type": "boolean", "default": False, "description": "true 时模式按扩展正则（grep -E）匹配，否则按字面匹配"},
            "ignore_case": {"type": "boolean", "default": False, "description": "true 时忽略大小写（grep -i）"},
            "context_before": _context_schema("每个匹配行之前多取的行数（grep -B）", max_context_lines),
            "context_after": _context_schema("每个匹配行之后多取的行数（grep -A），用于取出异常堆栈", max_context_lines),
        },
        "required": ["svr_ids", "log_type"],
        "additionalProperties": False,
    }


def list_servers_input_schema() -> dict[str, Any]:
    """list_servers 不带参数。"""
    return {"type": "object", "properties": {}, "additionalProperties": False}
