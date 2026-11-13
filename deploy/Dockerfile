# 跳板机服务镜像。构建上下文为仓库根目录，在 deploy/ 中执行 docker compose up -d --build 即可。
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /src
COPY pyproject.toml ./
COPY log_viewer_mcp ./log_viewer_mcp
# 安装 log-viewer-mcp-server 与 log-viewer-mcp-token 两个命令
RUN pip install --no-cache-dir ".[server]"

# exec 形式：docker stop 的 SIGTERM 直接到达服务进程
ENTRYPOINT ["log-viewer-mcp-server"]
CMD ["-c", "/etc/log-viewer-mcp/config.toml"]
