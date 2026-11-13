# log-viewer-mcp

让 AI 助手（Kiro、Claude Code、Codex）直接查询游戏服的 SFS2X 日志。一位研发可以同时使用多个项目的多套环境（例如 A 项目线上、A 项目内网、B 项目内网），在对话里说要查哪套即可。

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

每套环境（一个项目的线上或内网）各部署一套跳板机服务，各有自己的地址、管理库和 token。研发电脑上只有一个本地 MCP：它从 `~/.log-viewer-mcp/envs.toml` 读取本机登记的全部环境，再按项目根目录的 `.log-viewer.toml` 只把本项目的环境提供给助手。助手每次调用工具时用 `env` 参数指定环境。

一次查询的过程：

1. 助手调用工具并指定 `env`，本地 MCP 带着研发在该环境的 token 把请求发给该环境的跳板机服务。
2. 跳板机服务校验 token，在管理库 `tbl_webserver` 中按 svr_id 查到游戏服的内网 IP。
3. 跳板机服务 SSH 登录游戏服，按时间选出日志文件，执行 grep，把结果存成文件，并写一条审计记录。
4. 本地 MCP 把结果文件下载到项目的 `query_log/<env>/` 目录，跳板机上的副本随即删除。

本项目由三个程序组成，装在一个 Python 包里：

| 程序 | 装在哪里 | 作用 | 文档中的别名 |
|---|---|---|---|
| `log-viewer-mcp-local` | 每位研发的电脑 | 由 AI 客户端自动启动，把查询转给跳板机并下载结果 | Local_MCP |
| `log-viewer-mcp-server` | 跳板机（Docker） | 按 svr_id 在管理库 `tbl_webserver` 中找到游戏服内网 IP，SSH 登录后 grep 日志 | Log_MCP_Server |
| `log-viewer-mcp-token` | 跳板机（Docker 容器内） | 给研发发放和吊销访问 token | Token_CLI |

助手能用的工具只有两个：`list_servers`（列出游戏服）和 `search_logs`（查日志）。参数与用法写在 [skills/log-viewer/SKILL.md](skills/log-viewer/SKILL.md) 中，安装 skill 后助手会自己读。

## 研发：安装与使用

开始前，向每套要用的环境的运维要两样东西：**跳板机地址**和**你在这套环境的 token**。每套环境的 token 单独发放，不能混用。

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

### 第 3 步：登记环境

一套跳板机服务就是一个**环境**，例如 A 项目线上、A 项目内网、B 项目内网各是一个环境。把你要用的环境都登记在用户目录下的 `envs.toml` 中，所有项目、所有 AI 客户端共用这一份。**这个文件里有 token，不要提交到任何 git 仓库。**

| 系统 | 文件位置 |
|---|---|
| macOS / Linux | `~/.log-viewer-mcp/envs.toml` |
| Windows | `%USERPROFILE%\.log-viewer-mcp\envs.toml` |

先把本仓库 clone 到任意目录（第 6 步安装 skill 也要用），再从模板 [examples/envs.toml](examples/envs.toml) 复制一份。

macOS / Linux：

```bash
git clone --depth 1 https://github.com/sunbeamomni/Log-Viewer-MCP.git log-viewer-mcp
mkdir -p ~/.log-viewer-mcp
cp log-viewer-mcp/examples/envs.toml ~/.log-viewer-mcp/envs.toml
chmod 600 ~/.log-viewer-mcp/envs.toml
```

Windows（PowerShell）：

```powershell
git clone --depth 1 https://github.com/sunbeamomni/Log-Viewer-MCP.git log-viewer-mcp
New-Item -ItemType Directory -Force ~\.log-viewer-mcp | Out-Null
Copy-Item log-viewer-mcp\examples\envs.toml ~\.log-viewer-mcp\envs.toml
```

然后每套环境写一节，用不到的删掉：

```toml
[envs.a-prod]
description = "A 项目线上"
service_url = "http://<A 线上跳板机地址>:8999"
token = "<A 线上的 token>"

[envs.a-dev]
description = "A 项目内网"
service_url = "http://<A 内网跳板机地址>:8999"
token = "<A 内网的 token>"
```

- 环境名（`[envs.` 后面的部分）只能用小写字母、数字和连字符，以字母或数字开头，最长 20 个字符。助手用它选择环境，结果也放在 `query_log/<环境名>/` 下。项目文件（第 5 步）会写环境名，所以同一个项目的同事要用相同的名字，最好由项目统一约定。
- `description` 写清是哪个项目、线上还是内网。助手根据它判断用户说的"线上""内网"对应哪套环境。
- 写错时本地 MCP 启动失败，客户端的 MCP 日志中会列出是哪一项有问题（不会打印 token）。

以后新增环境、换 token，只改这个文件，然后重启 AI 客户端。

### 第 4 步：配置 AI 客户端

客户端配置里只写启动命令，不写地址和 token。任选你使用的客户端。

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
      "command": "log-viewer-mcp-local"
    }
  }
}
```

文件里已经有 `mcpServers` 时，只把 `"log-viewer": {...}` 这一项加进去。

#### Claude Code

对所有项目生效（推荐）：

```bash
claude mcp add --scope user log-viewer -- log-viewer-mcp-local
```

或者只对某个项目生效：把 [examples/claude-code/.mcp.json](examples/claude-code/.mcp.json) 复制到项目根目录。这个文件里没有地址和 token，可以提交到仓库。

#### Codex

把 [examples/codex/config.toml](examples/codex/config.toml) 的内容追加到 Codex 的配置文件中：

| 系统 | 文件位置 |
|---|---|
| macOS / Linux | `~/.codex/config.toml` |
| Windows | `%USERPROFILE%\.codex\config.toml` |

示例中的 `tool_timeout_sec = 60` 不要调小，否则较慢的查询会被 Codex 提前中断。

### 第 5 步：给项目指定环境

在每个项目的根目录放一个 `.log-viewer.toml`（模板见 [examples/.log-viewer.toml](examples/.log-viewer.toml)），列出这个项目用到的环境。它只有环境名，没有地址和 token，可以提交到仓库，同事共用。

A 项目的仓库：

```toml
envs = ["a-prod", "a-dev"]
```

B 项目的仓库：

```toml
envs = ["b-dev"]
```

效果：

- 在 A 项目里，助手只看到 `a-prod` 和 `a-dev`，要查哪个由你在对话里说明，例如"查 A 线上 1001 服……"或"查内网……"。没说清时助手会先问你。
- 在 B 项目里只有 `b-dev` 一套环境，助手不用问，直接查。
- 项目里没有 `.log-viewer.toml` 时，助手能看到 `envs.toml` 中的全部环境。
- `.log-viewer.toml` 里写了 `envs.toml` 中没有的环境名时，本地 MCP 启动失败并提示缺哪个环境：向运维要这套环境的地址和 token，补进 `envs.toml`。

切换项目时不用改任何配置：在哪个项目里打开 AI 客户端，就用哪个项目的环境。同一项目里切换线上和内网，在对话里说明即可。

### 第 6 步：安装 skill

skill 告诉助手怎样调用工具、怎样选择环境，以及怎样换算服务器时间。把仓库里的 `skills/log-viewer` 整个目录复制到客户端的 skills 目录，目录名保持 `log-viewer`：

| 客户端 | 只对某个项目生效 | 对所有项目生效 |
|---|---|---|
| Kiro | `.kiro/skills/log-viewer/SKILL.md` | `~/.kiro/skills/log-viewer/SKILL.md` |
| Claude Code | `.claude/skills/log-viewer/SKILL.md` | `~/.claude/skills/log-viewer/SKILL.md` |
| Codex | `.agents/skills/log-viewer/SKILL.md` | `~/.agents/skills/log-viewer/SKILL.md` |

以装到 Kiro 的"对所有项目生效"目录为例（其他客户端只需替换目标目录），在第 3 步 clone 仓库的目录中执行。

macOS / Linux：

```bash
mkdir -p ~/.kiro/skills
cp -R log-viewer-mcp/skills/log-viewer ~/.kiro/skills/
```

Windows（PowerShell）：

```powershell
New-Item -ItemType Directory -Force ~\.kiro\skills | Out-Null
Copy-Item -Recurse log-viewer-mcp\skills\log-viewer ~\.kiro\skills\
```

### 第 7 步：忽略结果目录

结果文件含线上数据。在使用本工具的每个项目的 `.gitignore` 中加一行：

```text
query_log/
```

### 第 8 步：试一下

重启 AI 客户端，然后对助手说：

> 列出 A 线上可以查日志的游戏服

助手调用 `list_servers` 并列出服务器，就说明安装成功。项目有多套环境时，每套都试一下，确认各自的 token 可用。

之后直接用自然语言描述要查的内容即可，例如"查 A 线上 1001 服服务器时间 2026-10-06 21:13 前后 5 分钟含 `uid=123` 的日志"。结果在项目的 `query_log/<环境名>/` 下，默认 24 小时后自动清理。

### 升级

```bash
uv tool upgrade log-viewer-mcp
```

然后重启 AI 客户端。skill 也有更新时，先在 clone 下来的 `log-viewer-mcp` 目录中执行 `git pull`，再重新执行第 6 步的复制命令。

## 运维：部署跳板机服务

跳板机服务用 Docker 部署，部署步骤、token 管理、游戏服上的日志要求和故障排查见 [deploy/README.md](deploy/README.md)。

## 常见问题

| 现象或错误码 | 原因与处理 |
|---|---|
| 客户端里看不到 log-viewer 工具 | 本地 MCP 没装好，或客户端找不到命令。按研发第 2 步检查；仍不行就把配置中的 `"command"` 改成 `which` / `Get-Command` 输出的完整路径，然后重启客户端 |
| 本地 MCP 启动失败 | `envs.toml` 或项目的 `.log-viewer.toml` 写错了。客户端的 MCP 日志里会列出每个问题（不含 token），按提示修改后重启客户端 |
| 助手看到了别的项目的环境 | 项目根目录没有 `.log-viewer.toml`，或客户端启动本地 MCP 时的工作目录不是项目根目录 |
| `INVALID_ARGUMENT` 且说明中提到 env | 项目有多套环境，助手没指定或指定了不存在的环境。告诉助手要查哪套 |
| `AUTH_FAILED` | 这套环境的 token 填错或已被吊销。注意每套环境的 token 不同，找这套环境的运维重新发放，改 `envs.toml` |
| `SERVICE_UNAVAILABLE` | 连不上这套环境的跳板机，或等待结果超时。检查 `envs.toml` 中的 `service_url`、端口和网络（内网环境要连上内网）；超时的话缩小时间范围或加关键字后重查 |
| `SERVER_NOT_FOUND` | 这套环境的管理库中没有这个 svr_id。先确认环境选对了，再让助手调用 `list_servers` 确认 |
| `BUSY` | 同时进行的查询太多，稍后重试 |
| `SSH_FAILED`、`HOST_KEY_MISMATCH`、`LOG_DIR_UNAVAILABLE`、`DB_UNAVAILABLE` 等 | 跳板机或游戏服一侧的问题，把错误码告诉运维，处理方法见[部署文档的故障排查](deploy/README.md#故障排查) |
| 结果里有查询范围之外的行 | 正常现象：服务按文件选取，一个文件覆盖 1 分钟到 1 天。需要精确范围时让助手按行首时间再过滤 |
| 摘要中 `truncated` 为 `timeout` 或 `scan_limit` | 结果不完整：缩小时间范围、加包含关键字，或分段查询 |

## 参考

### 本地 MCP 的配置文件

| 文件 | 位置 | 内容 | 能否提交到仓库 |
|---|---|---|---|
| 环境登记文件 | `~/.log-viewer-mcp/envs.toml`（可用 `LOG_MCP_ENVS_FILE` 改路径） | 每套环境的名字、说明、地址与 token，模板见 [examples/envs.toml](examples/envs.toml) | 不能 |
| 项目文件 | 项目根目录的 `.log-viewer.toml` | 本项目可用的环境名，模板见 [examples/.log-viewer.toml](examples/.log-viewer.toml) | 可以 |

两个文件都在本地 MCP 启动时读取，改动后重启 AI 客户端生效。

### 本地 MCP 的环境变量

都可以不设，需要时写在客户端配置的 `env` 中：

| 变量 | 说明 | 默认值 |
|---|---|---|
| `LOG_MCP_ENVS_FILE` | 环境登记文件的路径；指定后文件必须存在 | `~/.log-viewer-mcp/envs.toml` |
| `LOG_MCP_RESULT_DIR` | 结果下载目录；相对路径按客户端启动本地 MCP 时的工作目录（一般是项目根目录）解析 | `query_log` |
| `LOG_MCP_CALL_TIMEOUT_S` | 每次查询等待结果与下载的时限（秒） | `50` |
| `LOG_MCP_RESULT_TTL_H` | 本地结果文件保留多久（小时） | `24` |

旧版本的配置方式（客户端配置的 `env` 里写 `LOG_MCP_SERVICE_URL` 与 `LOG_MCP_TOKEN`）仍然可用：没有环境登记文件时，这两个变量组成一套名为 `default` 的环境。有了环境登记文件后这两个变量会被忽略，可以从客户端配置中删掉。

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
