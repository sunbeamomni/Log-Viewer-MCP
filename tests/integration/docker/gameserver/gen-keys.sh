#!/bin/bash
# keys 服务：在 keys 卷中生成测试专用的 SSH 密钥（只用于集成测试，不是任何真实环境的密钥）。已生成过则直接退出。
#
# 生成内容（/keys 下）：
#   client/id_ed25519(.pub)   测试运行容器用来以 root 登录游戏服的私钥，属主 TEST_UID，权限 0600
#   host/<名称>/...           各游戏服容器的主机密钥，目录 0700 root，测试运行容器读不到私钥
#   known_hosts               "<IP> ssh-ed25519 <公钥>"，每台游戏服一行
#   known_hosts_wrong         同样的 IP 配另一把公钥，用于 HOST_KEY_MISMATCH 测试
#   ready                     生成完成的标记
#
# 环境变量：GS_HOSTS="名称=IP 名称=IP ..."，TEST_UID（默认 10001）
set -euo pipefail

out=/keys
uid=${TEST_UID:-10001}
hosts=${GS_HOSTS:?GS_HOSTS 未设置}

if [ -f "$out/ready" ]; then
  echo "gen-keys: 测试密钥已存在，跳过" >&2
  exit 0
fi

umask 077
rm -rf "$out/client" "$out/host" "$out/known_hosts" "$out/known_hosts_wrong" "$out/.tmp"
mkdir -p "$out/.tmp"

ssh-keygen -q -t ed25519 -N '' -C 'log-viewer-mcp integration TEST ONLY client key' -f "$out/.tmp/id_ed25519"
mkdir -p "$out/client"
mv "$out/.tmp/id_ed25519" "$out/.tmp/id_ed25519.pub" "$out/client/"
chown "$uid:$uid" "$out/client/id_ed25519" "$out/client/id_ed25519.pub"
chmod 0600 "$out/client/id_ed25519"
chmod 0644 "$out/client/id_ed25519.pub"

ssh-keygen -q -t ed25519 -N '' -C 'log-viewer-mcp integration TEST ONLY wrong host key' -f "$out/.tmp/wrong_host_key"
wrong=$(cut -d' ' -f1,2 "$out/.tmp/wrong_host_key.pub")

mkdir -p "$out/host"
: >"$out/.tmp/known_hosts"
: >"$out/.tmp/known_hosts_wrong"
for entry in $hosts; do
  name=${entry%%=*}
  ip=${entry#*=}
  mkdir -m 0700 "$out/host/$name"
  ssh-keygen -q -t ed25519 -N '' -C "log-viewer-mcp integration TEST ONLY host key ($name)" \
    -f "$out/host/$name/ssh_host_ed25519_key"
  printf '%s %s\n' "$ip" "$(cut -d' ' -f1,2 "$out/host/$name/ssh_host_ed25519_key.pub")" >>"$out/.tmp/known_hosts"
  printf '%s %s\n' "$ip" "$wrong" >>"$out/.tmp/known_hosts_wrong"
done
mv "$out/.tmp/known_hosts" "$out/.tmp/known_hosts_wrong" "$out/"
chmod 0644 "$out/known_hosts" "$out/known_hosts_wrong"
rm -rf "$out/.tmp"

chmod 0755 "$out" "$out/client" "$out/host"
touch "$out/ready"
chmod 0644 "$out/ready"
echo "gen-keys: 已生成测试密钥" >&2
