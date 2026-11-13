# 测试专用的 /root/.bashrc。
# 模拟线上：sshd 经 root 的登录 Shell 执行命令行时，Debian 的 bash 会读取 ~/.bashrc 并打印内容。
# 输出中故意包含形似协议记录的文本（不含 NUL）：① 必须丢弃第一个 NUL 之前的内容，② 只认 "B READY" 之后的内容。
# 另外设置几个会改变程序行为的环境变量与别名，固定命令行中的 env -i 与 bash --noprofile --norc 应使它们全部失效。
# 本文件只打印、不写任何文件（root 主目录在只读快照范围内）。

echo "Welcome to SFS2X game server (integration test) -- 这是 ~/.bashrc 的输出"
echo "0123456789abcdef0123456789abcdef READY 1"
echo "0123456789abcdef0123456789abcdef FILE 0"
echo "EXIT L 0"
echo "L 1 2 smartfox.log"
echo "MISSING grep"
echo "bashrc: stderr noise" >&2

export BASH_ENV=/etc/testenv/bash_env.sh
export ENV=/etc/testenv/bash_env.sh
export GREP_OPTIONS=--version
export GREP_COLORS='mt=01;31'
export GZIP=--verbose
export LC_ALL=C.UTF-8
export POSIXLY_CORRECT=1
alias grep='echo ALIASED_GREP'
alias find='echo ALIASED_FIND'
