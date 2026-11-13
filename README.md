# log-viewer-mcp

让 AI 助手（Kiro、Claude Code、Codex）直接查询线上游戏服的 SFS2X 日志。

研发在对话里说"查一下 1001 服 21:13 前后含 uid=123 的日志"，助手会调用本工具，跳板机登录对应的游戏服执行 grep，把结果文件下载到当前项目的 `query_log/` 目录，助手再读取文件分析。全程只读，不会修改游戏服上的任何文件。

目录：

- [工作原理](#工作原理)
- [研发：安装与使用](#研发安装与使用)（macOS、Linux、Windows）
- [运维：部署跳板机服务](#运维部署跳板机服务)（单独的文档：[deploy/README.md](deploy/README.md)）
- [常见问题](#常见问题)
- [参考](#参考)

## 工作原理

```text
AI 客户端 ──▶ 本地 MCP ──HTTP + token──▶ 跳板机服务 ──SSH──▶ 游戏服
（研发电脑）  （研发电脑）                （跳板机 Docker）     （只读 grep）
```

一次查询的过程：

1. 助手调用工具，本地 MCP 带着研发的 token 把请求发给跳板机服务。
2. 跳板机服务校验 token，在管理库 `tbl_webserver` 中按 svr_id 查到游戏服的内网 IP。
3. 跳板机服务 SSH 登录游戏服，按时间选出日志文件，执行 grep，把结果存成文件，并写一条审计记录。
4. 本地 MCP 把结果文件下载到项目的 `query_log/` 目录，跳板机上的副本随即删除。

本项目由三个程序组成，装在一个 Python 包里：

| 程序 | 装在哪里 | 作用 | 文档中的别名 |
|---|---|---|---|
| `log-viewer-mcp-local` | 每位研发的电脑 | 由 AI 客户端自动启动，把查询转给跳板机并下载结果 | Local_MCP |
| `log-viewer-mcp-server` | 跳板机（Docker） | 按 svr_id 在管理库 `tbl_webserver` 中找到游戏服内网 IP，SSH 登录后 grep 日志 | Log_MCP_Server |
| `log-viewer-mcp-token` | 跳板机（Docker 容器内） | 给研发发放和吊销访问 token | Token_CLI |

助手能用的工具只有两个：`list_servers`（列出游戏服）和 `search_logs`（查日志）。参数与用法写在 [skills/log-viewer/SKILL.md](skills/log-viewer/SKILL.md) 中，安装 skill 后助手会自己读。

## 研发：安装与使用

开始前向运维要两样东西：**跳板机地址**和**你自己的 token**。下文的 `<跳板机地址>`、`<token>` 都要换成实际值。

### 第 1 步：安装 uv

[uv](https://docs.astral.sh/uv/) 是 Python 的包管理工具，用来安装本地 MCP，不需要另外装 Python。

macOS / Linux：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Windows（PowerShell）：

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

装完后**关掉并重新打开终端**，执行 `uv --version` 能看到版本号即可。

### 第 2 步：安装本地 MCP

三个系统的命令相同（需要能用 git 访问本仓库）：

```bash
uv tool install git+https://github.com/sunbeamomni/Log-Viewer-MCP.git
```

然后确认命令能找到：

| 系统 | 检查命令 |
|---|---|
| macOS / Linux | `which log-viewer-mcp-local` |
| Windows | `Get-Command log-viewer-mcp-local` |

找不到时执行 `uv tool update-shell`，再重开终端。记下输出的完整路径，第 4 步可能用到。

### 第 3 步：配置 AI 客户端

任选你使用的客户端。**含 token 的配置文件不要提交到 git 仓库。**

#### Kiro

编辑用户级配置文件（没有就新建）：

| 系统 | 文件位置 |
|---|---|
| macOS / Linux | `~/.kiro/settings/mcp.json` |
| Windows | `%USERPROFILE%\.kiro\settings\mcp.json` |

内容（模板见 [examples/kiro/mcp.json](examples/kiro/mcp.json)）：

```json
{
  "mcpServers": {
    "log-viewer": {
      "command": "log-viewer-mcp-local",
      "env": {
        "LOG_MCP_SERVICE_URL": "http://<跳板机地址>:8999",
        "LOG_MCP_TOKEN": "<token>"
      }
    }
  }
}
```

文件里已经有 `mcpServers` 时，只把 `"log-viewer": {...}` 这一项加进去。

#### Claude Code

1. 把 [examples/claude-code/.mcp.json](examples/claude-code/.mcp.json) 复制到要使用本工具的项目根目录，把其中的 `<跳板机地址>` 换成实际值。这个文件里不写 token，可以提交到仓库。
2. 把 token 设成环境变量 `LOG_MCP_TOKEN`：

   macOS（默认 zsh）：

   ```bash
   echo 'export LOG_MCP_TOKEN="<token>"' >> ~/.zshrc && source ~/.zshrc
   ```

   Linux（bash）：

   ```bash
   echo 'export LOG_MCP_TOKEN="<token>"' >> ~/.bashrc && source ~/.bashrc
   ```

   Windows（PowerShell，写入用户环境变量，重开终端后生效）：

   ```powershell
   [Environment]::SetEnvironmentVariable("LOG_MCP_TOKEN", "<token>", "User")
   ```

3. 在新终端中进入项目目录启动 `claude`。

#### Codex

把 [examples/codex/config.toml](examples/codex/config.toml) 的内容追加到 Codex 的配置文件中，并替换 `<跳板机地址>` 和 `<token>`：

| 系统 | 文件位置 |
|---|---|
| macOS / Linux | `~/.codex/config.toml` |
| Windows | `%USERPROFILE%\.codex\config.toml` |

示例中的 `tool_timeout_sec = 60` 不要调小，否则较慢的查询会被 Codex 提前中断。

### 第 4 步：安装 skill

skill 告诉助手怎样调用工具，以及怎样换算服务器时间（GMT-2）。把仓库里的 `skills/log-viewer` 整个目录复制到客户端的 skills 目录，目录名保持 `log-viewer`：

| 客户端 | 只对某个项目生效 | 对所有项目生效 |
|---|---|---|
| Kiro | `.kiro/skills/log-viewer/SKILL.md` | `~/.kiro/skills/log-viewer/SKILL.md` |
| Claude Code | `.claude/skills/log-viewer/SKILL.md` | `~/.claude/skills/log-viewer/SKILL.md` |
| Codex | `.agents/skills/log-viewer/SKILL.md` | `~/.agents/skills/log-viewer/SKILL.md` |

以装到 Kiro 的"对所有项目生效"目录为例（其他客户端只需替换目标目录）。

macOS / Linux：

```bash
git clone --depth 1 https://github.com/sunbeamomni/Log-Viewer-MCP.git log-viewer-mcp
mkdir -p ~/.kiro/skills
cp -R log-viewer-mcp/skills/log-viewer ~/.kiro/skills/
```

Windows（PowerShell）：

```powershell
git clone --depth 1 https://github.com/sunbeamomni/Log-Viewer-MCP.git log-viewer-mcp
New-Item -ItemType Directory -Force ~\.kiro\skills | Out-Null
Copy-Item -Recurse log-viewer-mcp\skills\log-viewer ~\.kiro\skills\
```

### 第 5 步：忽略结果目录

结果文件含线上数据。在使用本工具的每个项目的 `.gitignore` 中加一行：

```text
query_log/
```

### 第 6 步：试一下

重启 AI 客户端，然后对助手说：

> 列出可以查日志的游戏服

助手调用 `list_servers` 并列出服务器，就说明安装成功。之后直接用自然语言描述要查的内容即可，例如"查 1001 服服务器时间 2026-10-06 21:13 前后 5 分钟含 `uid=123` 的日志"。结果在项目的 `query_log/` 下，默认 24 小时后自动清理。

### 升级

```bash
uv tool upgrade log-viewer-mcp
```

然后重启 AI 客户端。skill 也有更新时，重新执行第 4 步的复制命令。

## 运维：部署跳板机服务

跳板机服务用 Docker 部署，部署步骤、token 管理、游戏服上的日志要求和故障排查见 [deploy/README.md](deploy/README.md)。

## 常见问题

| 现象或错误码 | 原因与处理 |
|---|---|
| 客户端里看不到 log-viewer 工具 | 本地 MCP 没装好，或客户端找不到命令。按研发第 2 步检查；仍不行就把配置中的 `"command"` 改成 `which` / `Get-Command` 输出的完整路径，然后重启客户端 |
| `AUTH_FAILED` | token 填错或已被吊销，找运维重新发放 |
| `SERVICE_UNAVAILABLE` | 连不上跳板机，或等待结果超时。检查跳板机地址、端口和网络；超时的话缩小时间范围或加关键字后重查 |
| `SERVER_NOT_FOUND` | 管理库中没有这个 svr_id，先让助手调用 `list_servers` 确认 |
| `BUSY` | 同时进行的查询太多，稍后重试 |
| `SSH_FAILED`、`HOST_KEY_MISMATCH`、`LOG_DIR_UNAVAILABLE`、`DB_UNAVAILABLE` 等 | 跳板机或游戏服一侧的问题，把错误码告诉运维，处理方法见[部署文档的故障排查](deploy/README.md#故障排查) |
| 结果里有查询范围之外的行 | 正常现象：服务按文件选取，一个文件覆盖 1 分钟到 1 天。需要精确范围时让助手按行首时间再过滤 |
| 摘要中 `truncated` 为 `timeout` 或 `scan_limit` | 结果不完整：缩小时间范围、加包含关键字，或分段查询 |

## 参考

### 本地 MCP 的环境变量

写在客户端配置的 `env` 中：

| 变量 | 说明 | 默认值 |
|---|---|---|
| `LOG_MCP_SERVICE_URL` | 跳板机服务地址，例如 `http://<跳板机地址>:8999` | 必填 |
| `LOG_MCP_TOKEN` | 访问 token | 必填 |
| `LOG_MCP_RESULT_DIR` | 结果下载目录；相对路径按客户端启动本地 MCP 时的工作目录（一般是项目根目录）解析 | `query_log` |
| `LOG_MCP_CALL_TIMEOUT_S` | 每次查询等待结果与下载的时限（秒） | `50` |
| `LOG_MCP_RESULT_TTL_H` | 本地结果文件保留多久（小时） | `24` |

### 开发与测试

单元测试和属性测试可以在任何系统上运行（在仓库根目录执行）。

macOS / Linux：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[server,test]"
python -m pytest tests/unit tests/property
```

Windows（PowerShell）：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[server,test]"
python -m pytest tests/unit tests/property
```

需要 Python 3.11 或更高版本。Windows 上少数依赖 POSIX 权限或 GNU grep 的用例会自动跳过。

集成测试在 Docker 中运行（需要 Docker Desktop 或 Docker Engine），会启动 MySQL 和模拟的游戏服。三个系统命令相同，改动代码后要重新 build：

```bash
docker compose -f tests/integration/compose.yaml build
docker compose -f tests/integration/compose.yaml run --rm runner
docker compose -f tests/integration/compose.yaml down -v
```

测试环境的说明见 [tests/integration/README.md](tests/integration/README.md)。

### 版本发布

每次推送到 `main` 分支，GitHub Actions（[.github/workflows/release.yml](.github/workflows/release.yml)）会自动：

1. 运行单元测试和属性测试，失败则停止；
2. 计算新版本号：最新的 `vX.Y.Z` tag 补丁号加 1，例如 `v0.1.3` 之后是 `v0.1.4`；
3. 构建 linux/amd64 与 linux/arm64 的跳板机镜像，推送到 `ghcr.io/sunbeamomni/log-viewer-mcp`，标签为 `0.1.4`、`0.1`、`latest` 和提交的短哈希；
4. 创建 tag `v0.1.4` 与对应的 GitHub Release，说明中自动列出这次包含的提交。

要升主版本或次版本，把 `pyproject.toml` 中的 `version` 改成新的版本号（如 `0.2.0`）后推送；它比"最新 tag 加 1"大时以它为准。同一个提交重新运行工作流不会重复发版。
