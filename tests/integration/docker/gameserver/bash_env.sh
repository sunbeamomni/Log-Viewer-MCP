# 测试专用：~/.bashrc 把 BASH_ENV 与 ENV 指向本文件。
# 固定命令行用 env -i 清空环境，远端 bash 不应读取本文件；若被读取，会在标准输出与标准错误中留下标记，测试据此发现问题。
# 本文件只打印、不写任何文件。
echo "BASH_ENV_SOURCED_MARKER"
echo "BASH_ENV_SOURCED_MARKER" >&2
