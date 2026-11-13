"""common/formats.py 的单元测试（任务 1.4；需求 4.7）。"""

import secrets

import pytest

from log_viewer_mcp.common import formats


# ---------------------------------------------------------------------------
# Result_Handle
# ---------------------------------------------------------------------------


def test_generated_handles_are_valid():
    for _ in range(200):
        handle = secrets.token_urlsafe(16)
        assert len(handle) == formats.RESULT_HANDLE_LENGTH
        assert formats.is_valid_result_handle(handle)


@pytest.mark.parametrize(
    "handle",
    [
        "q3Zr0V2m1yX8kD5sWb7aLg",
        "______________________",
        "----------------------",
        "0123456789abcdefABCDEF",
    ],
)
def test_valid_handles(handle):
    assert formats.is_valid_result_handle(handle)


@pytest.mark.parametrize(
    "handle",
    [
        "",
        "q3Zr0V2m1yX8kD5sWb7aL",  # 21 个字符
        "q3Zr0V2m1yX8kD5sWb7aLgx",  # 23 个字符
        "q3Zr0V2m1yX8kD5sWb7aLg\n",  # 末尾换行（Python 的 $ 会放过）
        "..",
        "../../../../../etc/pwd",  # 22 个字符但含 "/" 与 "."
        "q3Zr0V2m1yX8kD5sWb7a/g",
        "q3Zr0V2m1yX8kD5sWb7a\\g",
        "q3Zr0V2m1yX8kD5sWb7a.g",
        "q3Zr0V2m1yX8kD5sWb7a+g",  # 标准 base64 字符
        "q3Zr0V2m1yX8kD5sWb7aL=",
        "C:Zr0V2m1yX8kD5sWb7aLg",  # 盘符
        "q3Zr0V2m1yX8kD5sWb7a g",
        "q3Zr0V2m1yX8kD5sWb7a\x00g",
        "q3Zr0V2m1yX8kD5sWb7aL\u00e9",  # 非 ASCII 字母
        "q3Zr0V2m1yX8kD5sWb7aL\u0663",  # 非 ASCII 数字
    ],
)
def test_invalid_handles(handle):
    assert not formats.is_valid_result_handle(handle)


@pytest.mark.parametrize("value", [None, 123, b"q3Zr0V2m1yX8kD5sWb7aLg", ["q3Zr0V2m1yX8kD5sWb7aLg"]])
def test_non_str_handles_are_invalid(value):
    assert not formats.is_valid_result_handle(value)


# ---------------------------------------------------------------------------
# 结果文件名
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, parts",
    [
        ("1001_normal.log", ("1001", "normal")),
        ("4294967295_login.log", ("4294967295", "login")),
        ("1_a.log", ("1", "a")),
        ("1001_log_type_2.log", ("1001", "log_type_2")),
    ],
)
def test_valid_result_file_names(name, parts):
    assert formats.is_valid_result_file_name(name)
    assert formats.parse_result_file_name(name) == parts


@pytest.mark.parametrize(
    "name",
    [
        "",
        "1001_normal",
        "1001_normal.txt",
        "1001_normal.log\n",
        "1001_normal.log.part",
        "1001-normal.log",
        "1001_Normal.log",
        "1001_9normal.log",
        "1001_.log",
        "_normal.log",
        "12345678901_normal.log",  # 11 位数字
        "\u0661\u0660\u0660\u0661_normal.log",  # 非 ASCII 数字
        "1001_normal\u00e9.log",
        "1001_" + "a" * 33 + ".log",  # log_type 超过 32 个字符
        "../1001_normal.log",
        "..\\1001_normal.log",
        "sub/1001_normal.log",
        "/1001_normal.log",
        "C:1001_normal.log",
        "C:\\1001_normal.log",
        "1001_normal.log/..",
        "..",
        ".",
    ],
)
def test_invalid_result_file_names(name):
    assert not formats.is_valid_result_file_name(name)
    assert formats.parse_result_file_name(name) is None


@pytest.mark.parametrize("value", [None, 1001, b"1001_normal.log"])
def test_non_str_result_file_names_are_invalid(value):
    assert not formats.is_valid_result_file_name(value)
    assert formats.parse_result_file_name(value) is None


def test_result_file_name_generation():
    assert formats.result_file_name(1001, "normal") == "1001_normal.log"
    assert formats.result_file_name(4294967295, "login") == "4294967295_login.log"


@pytest.mark.parametrize(
    "svr_id, log_type",
    [
        (0, "normal"),
        (4294967296, "normal"),
        (True, "normal"),
        (1001.0, "normal"),
        ("1001", "normal"),
        (1001, "Normal"),
        (1001, "../x"),
        (1001, ""),
    ],
)
def test_result_file_name_rejects_invalid_parts(svr_id, log_type):
    with pytest.raises(ValueError):
        formats.result_file_name(svr_id, log_type)


# ---------------------------------------------------------------------------
# svr_id 与 log_type 名称
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [1, 1001, 4294967295])
def test_valid_svr_ids(value):
    assert formats.is_valid_svr_id(value)


@pytest.mark.parametrize("value", [0, -1, 4294967296, True, False, 1.0, "1001", None])
def test_invalid_svr_ids(value):
    assert not formats.is_valid_svr_id(value)


@pytest.mark.parametrize("value", ["1", "1001", "4294967295"])
def test_valid_svr_id_texts(value):
    assert formats.is_valid_svr_id_text(value)


@pytest.mark.parametrize(
    "value", ["", "0", "01001", "+1", "-1", " 1", "1\n", "4294967296", "1; rm -rf /", "1 OR 1=1", "\u0661", 1001]
)
def test_invalid_svr_id_texts(value):
    assert not formats.is_valid_svr_id_text(value)


@pytest.mark.parametrize("value", ["normal", "login", "a", "a" * 32, "log_2"])
def test_valid_log_type_names(value):
    assert formats.is_valid_log_type_name(value)


@pytest.mark.parametrize("value", ["", "Normal", "2log", "_log", "a" * 33, "nor-mal", "normal\n", None])
def test_invalid_log_type_names(value):
    assert not formats.is_valid_log_type_name(value)
