#!/bin/bash
# 测试专用的 grep 替身（只在 grepspy 镜像中安装到 grep 的原位置）。
# 每次调用把 argv[1..] 逐个以 NUL 结尾写入 /var/log/grep-argv/<微秒时间戳>-<pid>.argv（零个参数时为空文件），
# 然后 exec 真 grep，标准输入、输出与退出码都不受影响。参数本身不含 NUL，所以记录与 grep 收到的参数逐字节相同。
# 记录目录在只读快照范围（日志目录、ziplogs、/tmp、root 主目录）之外。先写 .tmp 再改名，读取方只看 .argv。
d=/var/log/grep-argv
f=$d/${EPOCHREALTIME/./}-$$
if { for a in "$@"; do printf '%s\0' "$a"; done; } >"$f.tmp" 2>/dev/null; then
  mv -f -- "$f.tmp" "$f.argv" 2>/dev/null
fi
exec /usr/lib/testenv/grep-real "$@"
