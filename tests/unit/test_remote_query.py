"""server/remote.py 的 ② 查询脚本生成（任务 6.3）。"""

from __future__ import annotations

import re
import shlex

import pytest

from log_viewer_mcp.server.remote import (
    BOUNDARY_PATTERN,
    QueryScript,
    build_command,
    build_query_script,
    is_fixed_command,
)
from log_viewer_mcp.server.select import SelectedFile

LOGS = "/usr/local/ds/SFS2X/logs"
ZIP = "/usr/local/ds/SFS2X/ziplogs"
B = "5b0d9c2e7f41a3c86e19d0b4f2a7c3e1"

ARCHIVE_ONLY = SelectedFile(plain=None, archive="smartfox.log.2026-09-17-15.tar.gz", inode=None,
                            ts="2026-09-17-15", start=0, end=3600, size=10)
PAIRED = SelectedFile(plain="smartfox.log.2026-09-17-16", archive="smartfox.log.2026-09-17-16.tar.gz", inode=None,
                      ts="2026-09-17-16", start=3600, end=7200, size=20)
CURRENT = SelectedFile(plain="smartfox.log", archive=None, inode=1234567, ts=None, start=7200, end=9000, size=30)
DESIGN_FILES = (ARCHIVE_ONLY, PAIRED, CURRENT)

# design.md ② 示例（一个排除模式、两个包含模式，-F，-A 2），路径为完整路径
DESIGN_SCRIPT = f"""\
{{ B={B}
  X=(-e 'heartbeat')
  I=(-e 'CMD' -e 'uid=123')
  F=('' '{LOGS}/smartfox.log.2026-09-17-16' '{LOGS}/smartfox.log')
  Z=('{ZIP}/smartfox.log.2026-09-17-15.tar.gz' '{ZIP}/smartfox.log.2026-09-17-16.tar.gz' '')
  D='{LOGS}' CK=2 CI=1234567
  printf '%s READY %s\\n' "$B" "$PPID"
  for i in "${{!F[@]}}"; do
    f=${{F[i]}} z=${{Z[i]}}
    if [ "$i" = "$CK" ]; then n=$(find -H "$D" -mindepth 1 -maxdepth 1 -type f -inum "$CI" -printf '%f' -quit)
      case $n in smartfox.log|smartfox.log.[0-9]*) f=$D/$n ;; *) f= ;; esac
    fi
    printf '%s FILE %d\\n' "$B" "$i"
    if [ -f "$f" ] && [ ! -L "$f" ]; then p=$f k=F
    elif [ -f "$z" ] && [ ! -L "$z" ]; then p=$z k=Z
    else p= k=N; fi
    if [ -n "$p" ]; then
      gzip -dcf -- "$p" | grep -a -F -v "${{X[@]}}" -- | grep -a -F -A 2 "${{I[@]}}" --; s="$k ${{PIPESTATUS[*]}}"
    else s=N; fi
    printf '%s END %d %s\\n' "$B" "$i" "$s"
  done
  printf '%s DONE\\n' "$B"
}} </dev/null
"""


def build(files=DESIGN_FILES, **kwargs) -> QueryScript:
    args = dict(plain_dir=LOGS, zip_dir=ZIP, seconds=45, ionice=True, boundary=B)
    args.update(kwargs)
    return build_query_script(files, **args)


def line_of(script: str, prefix: str) -> str | None:
    for line in script.splitlines():
        if line.startswith(prefix):
            return line
    return None


def pipeline(script: str) -> str:
    line = line_of(script, "      gzip ")
    assert line is not None
    return line.strip()


def array_words(script: str, name: str) -> list[str]:
    """把 ``  NAME=(…)`` 一行按 POSIX Shell 规则拆成词（模式不含控制字符，每个数组恰为一行）。"""
    line = line_of(script, f"  {name}=(")
    assert line is not None and line.endswith(")")
    return shlex.split(line[len(f"  {name}=("):-1])


# ---------------------------------------------------------------------------
# 模板与结构
# ---------------------------------------------------------------------------


def test_design_example_exact_text() -> None:
    q = build(include_patterns=["CMD", "uid=123"], exclude_patterns=["heartbeat"], context_after=2)
    assert q.script == DESIGN_SCRIPT
    assert q.boundary == B
    assert q.command == build_command(45, ionice=True)
    assert [f.index for f in q.files] == [0, 1, 2]
    assert [f.name for f in q.files] == [
        "smartfox.log.2026-09-17-15.tar.gz", "smartfox.log.2026-09-17-16", "smartfox.log"]
    assert [f.plain_path for f in q.files] == [None, f"{LOGS}/smartfox.log.2026-09-17-16", f"{LOGS}/smartfox.log"]
    assert [f.archive_path for f in q.files] == [
        f"{ZIP}/smartfox.log.2026-09-17-15.tar.gz", f"{ZIP}/smartfox.log.2026-09-17-16.tar.gz", None]
    assert [f.is_current for f in q.files] == [False, False, True]
    assert q.files[2].inode == 1234567 and q.files[1].inode is None
    assert [f.source for f in q.files] == list(DESIGN_FILES)


def test_ionice_follows_probe() -> None:
    assert "ionice -c 3" in build(ionice=True).command
    assert "ionice" not in build(ionice=False).command
    assert is_fixed_command(build(ionice=False).command)


def test_boundary_random_each_call() -> None:
    a = build_query_script(DESIGN_FILES, plain_dir=LOGS, zip_dir=ZIP, seconds=45, ionice=False)
    b = build_query_script(DESIGN_FILES, plain_dir=LOGS, zip_dir=ZIP, seconds=45, ionice=False)
    assert BOUNDARY_PATTERN.fullmatch(a.boundary) and BOUNDARY_PATTERN.fullmatch(b.boundary)
    assert a.boundary != b.boundary
    assert a.script.startswith("{ B=" + a.boundary + "\n")
    # B 只出现在脚本中，不在命令行中
    assert a.boundary not in a.command


@pytest.mark.parametrize("bad", ["", "5B0D9C2E7F41A3C86E19D0B4F2A7C3E1", "x" * 32, B + "0", B[:-1] + "\n", 1])
def test_bad_boundary_rejected(bad) -> None:
    with pytest.raises(ValueError):
        build(boundary=bad)


def test_no_include_matches_all_and_no_exclude_stage() -> None:
    q = build()
    assert "  I=(-e '')\n" in q.script
    assert line_of(q.script, "  X=") is None
    assert pipeline(q.script) == 'gzip -dcf -- "$p" | grep -a -F "${I[@]}" --; s="$k ${PIPESTATUS[*]}"'


def test_ignore_case_and_context_placement() -> None:
    q = build(include_patterns=["a"], exclude_patterns=["b"], regex=True, ignore_case=True,
              context_before=3, context_after=1)
    assert pipeline(q.script) == (
        'gzip -dcf -- "$p" | grep -a -E -i -v "${X[@]}" -- | grep -a -E -i -B 3 -A 1 "${I[@]}" --; '
        's="$k ${PIPESTATUS[*]}"')
    q = build(include_patterns=["a"], context_before=2)
    assert pipeline(q.script) == 'gzip -dcf -- "$p" | grep -a -F -B 2 "${I[@]}" --; s="$k ${PIPESTATUS[*]}"'


def test_no_current_and_no_files() -> None:
    q = build(files=(PAIRED,))
    assert "  D='%s' CK=-1 CI=0\n" % LOGS in q.script
    assert array_words(q.script, "F") == [f"{LOGS}/smartfox.log.2026-09-17-16"]
    q = build(files=())
    assert array_words(q.script, "F") == [] and array_words(q.script, "Z") == []
    assert q.files == ()


# ---------------------------------------------------------------------------
# 模式的转义
# ---------------------------------------------------------------------------

NASTY = [
    "'; rm -rf / #", "$(id)", "`reboot`", "${X[@]}", '"quoted"', "a'b''c", "\\", "*", "?[a-z]",
    "-e", "--help", "-", "--", "-v -f /etc/passwd", "&& touch /tmp/x", "> /tmp/out", "< /etc/shadow",
    "uid=123 | 中文昵称=小明", "!$", "~root", "a;b|c&d", " leading space", "trailing space ",
    "x" * 256,
]


def test_patterns_are_quoted_as_single_words() -> None:
    q = build(include_patterns=NASTY, exclude_patterns=list(reversed(NASTY)))
    inc = array_words(q.script, "I")
    exc = array_words(q.script, "X")
    assert inc == [w for p in NASTY for w in ("-e", p)]
    assert exc == [w for p in reversed(NASTY) for w in ("-e", p)]
    # 模式只出现在 X 与 I 两行，其余各行与无模式时的脚本完全相同
    plain = build(include_patterns=["P"], exclude_patterns=["Q"]).script.splitlines()
    nasty = q.script.splitlines()
    assert len(plain) == len(nasty)
    for a, b in zip(plain, nasty):
        if not (a.startswith("  X=(") or a.startswith("  I=(")):
            assert a == b


@pytest.mark.parametrize("bad", ["", "a\nb", "a\rb", "a\tb", "a\0b", "a\x7fb", "a\x85b", "\ud800", b"bytes", 1, None])
@pytest.mark.parametrize("which", ["include_patterns", "exclude_patterns"])
def test_bad_patterns_rejected(which, bad) -> None:
    with pytest.raises(ValueError):
        build(**{which: ["ok", bad]})


def test_patterns_must_be_sequence_of_str() -> None:
    with pytest.raises(ValueError):
        build(include_patterns="CMD")


@pytest.mark.parametrize("kwargs", [
    {"regex": 1}, {"ignore_case": "yes"}, {"context_before": -1}, {"context_after": True},
    {"context_after": 1.0}, {"context_before": 10**7}, {"seconds": 0}, {"ionice": 1},
])
def test_bad_scalar_args_rejected(kwargs) -> None:
    with pytest.raises(ValueError):
        build(**kwargs)


# ---------------------------------------------------------------------------
# 目录与文件名的再次校验
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "relative/logs", "/logs dir", "/logs;rm", "/logs'", "/日志", "/logs\n", "/$HOME"])
@pytest.mark.parametrize("which", ["plain_dir", "zip_dir"])
def test_bad_dirs_rejected(which, bad) -> None:
    with pytest.raises(ValueError):
        build(**{which: bad})


def sel(plain=None, archive=None, inode=None) -> SelectedFile:
    return SelectedFile(plain=plain, archive=archive, inode=inode, ts=None, start=None, end=0, size=0)


@pytest.mark.parametrize("bad", [
    sel(),  # 两者都没有
    sel(plain="gc.log"),
    sel(plain="-rf"),
    sel(plain="../etc/passwd"),
    sel(plain="smartfox.log.2026-09-17-16 x"),
    sel(plain="smartfox.log.2026-09-17-16\n"),
    sel(plain="smartfox.log.２０２６-09-17"),
    sel(archive="smartfox.log.2026-09-17-16.tar.gz.bak"),
    sel(archive="/abs/smartfox.log.2026-09-17-16.tar.gz"),
    sel(archive="smartfox.log.2026-09-17-16"),
    sel(plain="smartfox.log.2026-09-17-16", archive="smartfox.log.2026-09-17-15.tar.gz"),  # 时间戳不同
    sel(plain="smartfox.log.2026-09-17-16", inode=5),  # 只有 Current 带 inode
    sel(plain="smartfox.log"),  # Current 缺 inode
    sel(plain="smartfox.log", inode=-1),
    sel(plain="smartfox.log", inode=10**20),
    sel(plain="smartfox.log", inode=True),
    sel(plain="smartfox.log", archive="smartfox.log.2026-09-17-16.tar.gz", inode=1),
])
def test_bad_file_names_rejected(bad) -> None:
    with pytest.raises(ValueError):
        build(files=(PAIRED, bad))


def test_current_only_once() -> None:
    with pytest.raises(ValueError):
        build(files=(CURRENT, CURRENT))


def test_without_zip_dir_reads_plain_files_only() -> None:
    rotated = sel(plain="smartfox.log.2026-09-17-16")
    q = build(files=(rotated, CURRENT), zip_dir=None)
    assert array_words(q.script, "F") == [f"{LOGS}/smartfox.log.2026-09-17-16", f"{LOGS}/smartfox.log"]
    assert array_words(q.script, "Z") == ["", ""]


@pytest.mark.parametrize("files", [(ARCHIVE_ONLY,), (PAIRED, CURRENT)])
def test_without_zip_dir_rejects_archives(files) -> None:
    with pytest.raises(ValueError):
        build(files=files, zip_dir=None)


def test_accepts_all_name_formats() -> None:
    files = (
        sel(archive="smartfox.log.2026-09-14.tar.gz"),
        sel(plain="smartfox.log.2026-09-15"),
        sel(plain="smartfox.log.2026-09-17-16-02", archive="smartfox.log.2026-09-17-16-02.logpushinit.tar.gz"),
        CURRENT,
    )
    q = build(files=files, plain_dir="/usr/local/ds/SFS2X/logpushinit/")
    assert array_words(q.script, "F") == [
        "", "/usr/local/ds/SFS2X/logpushinit/smartfox.log.2026-09-15",
        "/usr/local/ds/SFS2X/logpushinit/smartfox.log.2026-09-17-16-02", "/usr/local/ds/SFS2X/logpushinit/smartfox.log"]
    assert "CK=3 CI=1234567" in q.script


# ---------------------------------------------------------------------------
# 只读：只用允许的程序与重定向（8.4）
# ---------------------------------------------------------------------------

ALLOWED_COMMANDS = {"printf", "find", "gzip", "grep", "["}
KEYWORDS = {"{", "}", "for", "in", "do", "done", "if", "then", "elif", "else", "fi", "!", "case", "esac"}
_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


ARRAY_PREFIXES = ("  X=(", "  I=(", "  F=(", "  Z=(")


def sq(value: str) -> str:
    """单引号转义（与 shlex.quote 对需要转义的值的做法相同）。"""
    return "'" + value.replace("'", "'\"'\"'") + "'"


def check_array_lines(script: str) -> None:
    """X、I、F、Z 各一行，只由 -e 与单引号词组成（因此数组内不可能有命令、重定向或展开）。"""
    for line in script.splitlines():
        if line.startswith(ARRAY_PREFIXES):
            name = line[2]
            words = array_words(script, name)
            if name in "XI":
                assert words[0::2] == ["-e"] * (len(words) // 2) and len(words) % 2 == 0
                rebuilt = " ".join(w if i % 2 == 0 else sq(w) for i, w in enumerate(words))
            else:
                rebuilt = " ".join(sq(w) for w in words)
            assert line == f"  {name}=({rebuilt})"


def command_words(script: str) -> list[str]:
    """处于命令位置的词（只针对本模板的启发式）：去掉数组行、引号内容与 case 的模式后按分隔符切分。"""
    text = "\n".join(line for line in script.splitlines() if not line.startswith(ARRAY_PREFIXES))
    text = re.sub(r"'[^']*'", "Q", text)
    text = re.sub(r'"[^"]*"', "Q", text)
    text = re.sub(r"case \S+ in [^)]*\)", "case;", text)
    text = text.replace("*)", ";")
    result = []
    for segment in re.split(r"\n|;|&&|\|\||\||\$\(|\)", text):
        tokens = segment.split()
        while tokens and (tokens[0] in KEYWORDS or _ASSIGN.match(tokens[0])):
            if tokens[0] == "for":  # for i in …
                tokens = []
                break
            tokens = tokens[1:]
        if tokens and not tokens[0].startswith("<"):
            result.append(tokens[0])
    return result


@pytest.mark.parametrize("kwargs", [
    {},
    {"include_patterns": ["CMD", "uid=123"], "exclude_patterns": ["heartbeat"], "context_after": 2},
    {"include_patterns": NASTY, "exclude_patterns": NASTY, "regex": True, "ignore_case": True,
     "context_before": 5, "context_after": 5},
])
def test_only_allowed_programs_and_redirects(kwargs) -> None:
    q = build(**kwargs)
    check_array_lines(q.script)
    words = command_words(q.script)
    assert set(words) <= ALLOWED_COMMANDS
    assert {"printf", "find", "gzip", "grep", "["} <= set(words)
    fixed = "\n".join(line for line in q.script.splitlines() if not line.startswith(ARRAY_PREFIXES)) + "\n"
    unquoted = re.sub(r"'[^']*'", "Q", fixed)
    assert "eval" not in unquoted and "<<" not in unquoted and ">" not in unquoted
    assert unquoted.count("<") == 1 and unquoted.endswith("} </dev/null\n")
    assert "&" not in unquoted.replace("&&", "")  # 没有后台运行
    # 选项只有固定的读取类选项
    for opt in ("-delete", "-exec", "-fprint", "-ok", "-o ", "--output", "-w "):
        assert opt not in unquoted
    assert "gzip -dcf -- " in unquoted


def test_command_words_heuristic_catches_extra_program() -> None:
    q = build()
    tampered = q.script.replace("  printf '%s DONE", "  rm -f x; printf '%s DONE")
    assert "rm" in command_words(tampered)
