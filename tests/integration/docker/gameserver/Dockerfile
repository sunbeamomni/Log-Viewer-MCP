# 测试专用的游戏服镜像：Debian 12 系统自带工具（bash、GNU grep、gzip、findutils、coreutils、util-linux）加 openssh-server。
# 三个构建目标：
#   base      与线上一致的工具集，root 用测试密钥登录，~/.bashrc 会打印内容
#   noionice  去掉 ionice（验证 ① 的探测与 ② 不加 ionice）
#   grepspy   grep 换成"记录 argv 后 exec 真 grep"的替身（属性 2）；记录写在 /var/log/grep-argv，位于快照范围之外
# 镜像中不含任何密钥：主机密钥与客户端公钥都在运行时由 keys 服务生成（见 gen-keys.sh）。

# 样例日志（任务 5.2）：用 tests/integration/sample_logs.py（只用标准库）确定性地生成 samples.tar 与 manifest.json。
# 生成器来自 compose 中的命名构建上下文 itsrc（即 tests/integration 目录）；游戏服镜像本身不装 Python，
# 工具集与线上一致。
FROM python:3.11-slim-bookworm AS samples
COPY --from=itsrc sample_logs.py /gen/sample_logs.py
RUN python /gen/sample_logs.py --out /out


FROM debian:12 AS base

# - 安装时自动生成的主机密钥不进镜像，运行时换成 keys 服务生成的测试密钥
# - 容器内 pam_loginuid 可能失败，改为 optional，避免登录被拒
# - [log_types] 的四个非压缩目录与 ziplogs，与线上一样权限 777；样例文件由 /opt/testenv/init.d 中的脚本写入（任务 5.2）
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends openssh-server \
 && rm -rf /var/lib/apt/lists/* \
 && rm -f /etc/ssh/ssh_host_* \
 && sed -i 's/^session\s\+required\s\+pam_loginuid.so/session optional pam_loginuid.so/' /etc/pam.d/sshd \
 && mkdir -p /run/sshd /opt/testenv/init.d \
 && mkdir -p /usr/local/ds/SFS2X/logs /usr/local/ds/SFS2X/logpushinit /usr/local/ds/SFS2X/loganalys \
             /usr/local/ds/SFS2X/polsarlog /usr/local/ds/SFS2X/ziplogs \
 && chmod 0777 /usr/local/ds/SFS2X/logs /usr/local/ds/SFS2X/logpushinit /usr/local/ds/SFS2X/loganalys \
               /usr/local/ds/SFS2X/polsarlog /usr/local/ds/SFS2X/ziplogs

COPY sshd_test.conf /etc/ssh/sshd_config.d/00-test.conf
COPY root.bashrc /root/.bashrc
COPY bash_env.sh /etc/testenv/bash_env.sh
COPY entrypoint.sh gen-keys.sh /opt/testenv/
COPY init.d/ /opt/testenv/init.d/
COPY --from=samples /out/ /opt/testenv/samples/
# Windows 检出可能带 CRLF，统一去掉
RUN sed -i 's/\r$//' /etc/ssh/sshd_config.d/00-test.conf /root/.bashrc /etc/testenv/bash_env.sh \
        /opt/testenv/entrypoint.sh /opt/testenv/gen-keys.sh /opt/testenv/init.d/* \
 && chmod 0755 /opt/testenv/entrypoint.sh /opt/testenv/gen-keys.sh /opt/testenv/init.d/* \
 && chmod 0644 /root/.bashrc /etc/testenv/bash_env.sh \
 && chmod 0755 /opt/testenv/samples && chmod 0644 /opt/testenv/samples/*

EXPOSE 22
ENTRYPOINT ["/opt/testenv/entrypoint.sh"]


FROM base AS noionice
# 按 PATH 找到真正的 ionice 并删除（兼容 merged-usr 与否）
RUN real=$(readlink -f "$(command -v ionice)") \
 && rm -f "$real" /usr/bin/ionice /bin/ionice \
 && ! command -v ionice


FROM base AS grepspy
COPY grep-spy.sh /tmp/grep-spy.sh
# 真 grep 移到 PATH 之外，原位置换成替身；记录目录 0777，测试运行容器（非 root）可以读取和清理
RUN sed -i 's/\r$//' /tmp/grep-spy.sh \
 && real=$(readlink -f "$(command -v grep)") \
 && mkdir -p /usr/lib/testenv \
 && mv "$real" /usr/lib/testenv/grep-real \
 && install -m 0755 /tmp/grep-spy.sh "$real" \
 && rm /tmp/grep-spy.sh \
 && mkdir -p /var/log/grep-argv && chmod 0777 /var/log/grep-argv \
 && [ "$(readlink -f "$(command -v grep)")" = "$real" ] \
 && /usr/lib/testenv/grep-real --version >/dev/null
