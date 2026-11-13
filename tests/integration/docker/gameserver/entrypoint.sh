#!/bin/bash
# 游戏服容器入口：从 keys 卷安装测试主机密钥与 root 的 authorized_keys，运行 /opt/testenv/init.d 中的初始化脚本
# （任务 5.2 用它生成样例日志），然后前台运行 sshd。初始化完成后创建 /run/testenv-ready，供健康检查判断。
set -euo pipefail

name=${GS_NAME:?GS_NAME 未设置}
keys=/keys

if [ ! -f "$keys/ready" ]; then
  echo "entrypoint: $keys 中没有测试密钥（keys 服务未运行？）" >&2
  exit 1
fi

install -m 0600 -o root -g root "$keys/host/$name/ssh_host_ed25519_key" /etc/ssh/ssh_host_ed25519_key
install -m 0644 -o root -g root "$keys/host/$name/ssh_host_ed25519_key.pub" /etc/ssh/ssh_host_ed25519_key.pub
install -d -m 0700 -o root -g root /root/.ssh
install -m 0600 -o root -g root "$keys/client/id_ed25519.pub" /root/.ssh/authorized_keys

# grepspy：记录目录是共享卷，确保测试运行容器（非 root）可以读取和清理
if [ -d /var/log/grep-argv ]; then
  chmod 0777 /var/log/grep-argv
fi

for s in /opt/testenv/init.d/*; do
  if [ -f "$s" ] && [ -x "$s" ]; then
    echo "entrypoint: 运行 $s" >&2
    "$s"
  fi
done

mkdir -p /run/sshd
/usr/sbin/sshd -t
touch /run/testenv-ready
exec /usr/sbin/sshd -D -e
