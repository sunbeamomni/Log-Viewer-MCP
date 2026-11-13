# 跳板机部署文档

本文面向运维，说明怎样在跳板机上部署和维护 log-viewer-mcp 的跳板机服务。研发电脑上的安装见仓库根目录的 [README.md](../README.md)。

目录：

- [部署前准备](#部署前准备)
- [部署步骤](#部署步骤)
- [日常运维](#日常运维)
- [游戏服上的日志要求](#游戏服上的日志要求)
- [配置参考](#配置参考)
- [故障排查](#故障排查)

## 部署前准备

跳板机服务做三件事：校验研发的 token；在管理库 `tbl_webserver` 中按 svr_id 查到游戏服的内网 IP；SSH 登录游戏服，只读地 grep 日志并把结果交给研发电脑下载。每次调用都写审计日志。

跳板机的要求：

- Linux（x86_64 或 arm64），装有 git 和 Docker（含 compose 插件）；
- 最好能访问 ghcr.io 拉取预构建镜像，不能访问时用源码构建，需要能访问 Docker Hub 和 PyPI；
- 能连上管理库 MySQL（默认 3306 端口），账号只需要 `tbl_webserver` 的 SELECT 权限；
- 能用 SSH 密钥登录各游戏服（默认 22 端口，root 用户），私钥不能设口令；
- 研发电脑能访问跳板机的服务端口（默认 8999）。

游戏服的 `/usr/bin` 或 `/bin` 中需要有 `env`、`timeout`、`nice`、`bash`、`find`、`grep` 和 `gzip`，常见 Linux 发行版都自带。有 `ionice` 时查询会以最低的 I/O 优先级运行，减少对游戏服的影响。

服务以容器方式运行，使用宿主机网络。下表是容器用到的几个目录：

| 跳板机上的目录 | 容器内路径 | 内容 |
|---|---|---|
| `deploy/config/` | `/etc/log-viewer-mcp` | config.toml、ssh_key、tokens.json，可选的 known_hosts、db_password |
| `deploy/audit/` | `/var/log/log-viewer-mcp/audit` | 审计日志，每天一个 `audit-YYYY-MM-DD.jsonl`，默认保留 90 天 |
| （容器内） | `/tmp/query_log` | 等待研发下载的结果文件，下载后删除，未取走的 60 分钟后清理 |

`deploy/config/` 和 `deploy/audit/` 已加入 `.gitignore`，不会被提交。

## 部署步骤

### 第 1 步：取代码，准备配置目录

```bash
git clone https://github.com/sunbeamomni/Log-Viewer-MCP.git log-viewer-mcp && cd log-viewer-mcp
mkdir deploy/config
cp deploy/config.example.toml deploy/config/config.toml
```

### 第 2 步：编辑配置

打开 `deploy/config/config.toml`。必须检查的项如下，其余保持默认即可（说明都写在文件注释里，完整列表见[配置参考](#配置参考)）：

| 配置项 | 填什么 |
|---|---|
| `[admin_db]` 的 `host`、`user`、`password`、`database` | 管理库的连接信息 |
| `listen` | 服务监听地址与端口，默认 `0.0.0.0:8999`。只想在内网网卡上监听时，把地址改成跳板机的内网 IP |
| `server_timezone` | 游戏服的时区，默认 `-02:00`，格式为 `±HH:MM` |
| `zip_dir` | 游戏服上压缩日志的目录；没有压缩日志就删掉这一行 |
| `[log_types]` | 每种日志类型对应游戏服上的哪个目录，见[游戏服上的日志要求](#游戏服上的日志要求) |

不想把数据库密码直接写在配置里时，删掉 `password`，改用 `password_file` 指向一个只含密码的文件，例如 `/etc/log-viewer-mcp/db_password`（对应跳板机上的 `deploy/config/db_password`）。

`server_timezone` 不要写 `Etc/GMT-2` 这样的 IANA 时区名，它实际表示 UTC+2，正负号与直觉相反。

### 第 3 步：放入 SSH 私钥

```bash
cp <登录游戏服的私钥> deploy/config/ssh_key
chmod 600 deploy/config/ssh_key
```

默认不校验游戏服的主机公钥。需要校验时：

1. 把 known_hosts 文件放到 `deploy/config/known_hosts`；
2. 在 config.toml 中取消 `[ssh]` 和其下 `known_hosts` 两行的注释。

配置后，known_hosts 中没有登记的游戏服会被拒绝登录（`HOST_KEY_MISMATCH`）。

### 第 4 步：启动

```bash
cd deploy
docker compose up -d
docker compose logs -f log-viewer-mcp
```

`docker compose up` 会先拉取 GitHub Actions 构建好的镜像 `ghcr.io/sunbeamomni/log-viewer-mcp:latest`（amd64 与 arm64 都有，Docker 自动选择跳板机的架构）。跳板机访问不了 ghcr.io 时，它会自动改用本地源码构建，无需额外操作。

日志中出现 `log-viewer-mcp 启动：监听 …` 即启动成功，按 Ctrl+C 退出日志查看，服务继续运行。

镜像的几种用法：

| 场景 | 做法 |
|---|---|
| 固定某个版本，不随 latest 变化 | `export LOG_VIEWER_MCP_IMAGE=ghcr.io/sunbeamomni/log-viewer-mcp:0.1.3` 后再执行 `docker compose up -d`；可用的版本见仓库的 Releases 页面 |
| 跳板机能上网但拉取报 `denied` 或 `unauthorized` | 镜像是私有的：让仓库管理员把 GitHub 上的 package 设为 Public，或在跳板机上执行 `docker login ghcr.io`（用户名为 GitHub 账号，密码为带 `read:packages` 权限的 PAT） |
| 不用预构建镜像，强制用本地源码构建 | `docker compose up -d --build` |

启动时服务会检查配置文件、私钥、tokens.json，并试连管理库。任一项不通过，服务会在日志中写明原因后退出（compose 会不断重启它）。改好配置后执行 `docker compose up -d` 即可。

### 第 5 步：给研发发 token

在 `deploy/` 目录中执行。发放和吊销都立即生效，不用重启服务：

```bash
# 创建：token 明文只显示这一次，复制后交给使用者
docker compose exec log-viewer-mcp log-viewer-mcp-token create <使用者姓名>
# 查看所有 token（只显示 token_id、使用者、创建时间和是否已吊销，不含明文）
docker compose exec log-viewer-mcp log-viewer-mcp-token list
# 吊销
docker compose exec log-viewer-mcp log-viewer-mcp-token revoke <token_id>
```

把跳板机地址（例如 `http://10.0.0.5:8999`）和 token 一起交给研发。token 丢失无法找回，吊销后重新创建即可。

### 第 6 步：上线前检查

1. 在一台游戏服上执行 `date +%z`。输出应与 `server_timezone` 一致，例如 `-02:00` 对应 `-0200`。
2. 用一台配好的研发电脑让助手调用 `list_servers`，能列出服务器。
3. 在游戏服最近的日志里挑一个关键字，让助手用 `search_logs` 查它。摘要状态为 ok，研发电脑 `query_log/` 中的结果与游戏服上 `grep -F '<关键字>' <原文件>` 的输出一致。
4. 在跳板机上执行 `docker compose exec log-viewer-mcp ls /tmp/query_log`，其中没有刚才的结果目录（下载后会删除）；`deploy/audit/audit-<日期>.jsonl` 中有这两次调用的记录。
5. 吊销测试用的 token 后再查一次，应返回 `AUTH_FAILED`。

## 日常运维

以下命令都在 `deploy/` 目录中执行：

| 操作 | 命令 |
|---|---|
| 升级到最新版 | `git pull && docker compose pull && docker compose up -d` |
| 升级（用本地源码构建） | `git pull && docker compose up -d --build` |
| 查看当前运行的版本 | `docker compose images log-viewer-mcp` |
| 修改配置后生效 | `docker compose restart log-viewer-mcp` |
| 查看运行日志 | `docker compose logs --tail 200 log-viewer-mcp` |
| 查看今天的审计日志 | `tail -n 20 audit/audit-$(date +%F).jsonl` |
| 停止 | `docker compose down` |

修改 config.toml 后要重启服务才生效；tokens.json 由 token 命令维护，改动会自动生效，不要手动编辑。

审计日志每行是一条 JSON 记录，包括调用者、时间、参数、访问的游戏服和在游戏服上执行的脚本。审计日志写入失败时，该次查询的结果会被丢弃，研发收到 `AUDIT_FAILED`。

## 游戏服上的日志要求

服务不解析日志行的内容，只按**文件名**判断每个文件覆盖的时间段，再对选中的文件执行 grep。因此游戏服上的日志须满足下面的约定。SFS2X 默认的 logback 配置（`%d | %-5level | %thread | %logger{40} | %msg%n`，参见 [example/logback.xml](../example/logback.xml)）已经满足。

### 非压缩目录

每种日志类型一个目录，配置在 `[log_types]` 的 `dir`。目录中：

| 文件 | 说明 |
|---|---|
| `smartfox.log` | 正在写入的当前文件 |
| `smartfox.log.<时间戳>` | 滚动出来的历史文件 |

`<时间戳>` 是游戏服本地时间。以下三种格式都支持，按文件名自动识别，无需配置：

| logback 的 fileNamePattern | 文件名示例 | 每个文件覆盖 |
|---|---|---|
| `smartfox.log.%d{yyyy-MM-dd}` | `smartfox.log.2026-10-06` | 1 天 |
| `smartfox.log.%d{yyyy-MM-dd-HH}` | `smartfox.log.2026-10-06-21` | 1 小时 |
| `smartfox.log.%d{yyyy-MM-dd-HH-mm}` | `smartfox.log.2026-10-06-21-13` | 1 分钟 |

其他名称的文件、子目录和符号链接都会被忽略。

### 压缩目录（可选）

压缩目录配置在 `zip_dir`，存放运维脚本压缩的历史日志，文件名为 `smartfox.log.<时间戳><类型后缀>.tar.gz`，例如 `smartfox.log.2026-10-06-21-13.loganalys.tar.gz`。

- 多种日志类型共用一个压缩目录时，靠类型后缀（`[log_types]` 的 `suffix`）区分，共用同一目录的类型之间后缀不能相同。
- 同一时间段的非压缩文件还在时，优先读非压缩文件。
- 某个类型的压缩包放在别处时，在该类型中单独写 `zip_dir`，它会覆盖顶层的 `zip_dir`。

没有压缩日志的环境（例如内网）有两种情况，都能正常查询，只是查不到已被压缩的部分：

- 配置中不写 `zip_dir`；
- 写了 `zip_dir`，但游戏服上没有这个目录。

压缩目录存在但没有读权限时仍会报 `LOG_DIR_UNAVAILABLE`，以便发现权限问题。

### `[log_types]` 示例

键名就是助手查询时用的 `log_type`，只能用小写字母、数字和下划线：

```toml
zip_dir = "/usr/local/ds/SFS2X/ziplogs"              # 所有类型默认共用的压缩目录，可省略

[log_types]
normal = { dir = "/usr/local/ds/SFS2X/logs", suffix = "" }
login = { dir = "/usr/local/ds/SFS2X/logpushinit", suffix = ".logpushinit" }
analysis = { dir = "/usr/local/ds/SFS2X/loganalys", suffix = ".loganalys" }
# 某个类型的压缩包放在别处时，单独写 zip_dir
pulsar = { dir = "/usr/local/ds/SFS2X/polsarlog", suffix = ".polsarlog", zip_dir = "/data/pulsar_zip" }
```

`dir` 与 `zip_dir` 必须是绝对路径，只能含字母、数字和 `/._-`。logback 配置里写的是相对 SFS2X 工作目录的路径（如 `logs/smartfox.log`），SFS2X 不是装在 `/usr/local/ds/SFS2X` 时要相应修改。

## 配置参考

全部配置项与默认值见 [config.example.toml](config.example.toml) 中的注释，注释掉的项写的就是默认值。常用的几组：

| 配置组 | 用途 |
|---|---|
| 顶层 | `listen`、`zip_dir`、`server_timezone`、`tokens_file` |
| `[admin_db]` | 管理库连接 |
| `[ssh]` | 登录游戏服的用户、端口、私钥、known_hosts、连接超时 |
| `[limits]` | 单次最多查几台服（10）、最长时间跨度（24 小时）、单次调用时限（40 秒）、单台服读取上限（8 GiB）、并发上限等 |
| `[results]` | 跳板机上结果文件的目录与保留时间 |
| `[audit]` | 审计日志目录与保留天数 |
| `[log_types]` | 日志类型与目录 |

配置中出现未知的项或格式不对时，服务拒绝启动并在日志中指出是哪一项。

容器的启动方式见 [compose.yaml](compose.yaml)，镜像构建见 [Dockerfile](Dockerfile)。

## 故障排查

先看运行日志：`docker compose logs --tail 200 log-viewer-mcp`。研发收到的错误只有错误码和简短说明，具体原因只记在运行日志中。

| 错误码或现象 | 原因与处理 |
|---|---|
| 容器反复重启 | 启动检查没通过，运行日志第一行写明了原因：配置项不对、私钥文件缺失或为空、管理库连不上等 |
| `AUTH_FAILED` | token 错误或已吊销，用 `log-viewer-mcp-token list` 核对，必要时重新发放 |
| `DB_UNAVAILABLE` | 管理库连不上，检查 `[admin_db]` 和跳板机到数据库的网络 |
| `SERVER_NOT_FOUND` | `tbl_webserver` 中没有该 svr_id |
| `INVALID_SERVER_ADDRESS` | `tbl_webserver` 中该服的 `ip_inner` 不是合法的 IPv4 地址 |
| `SSH_FAILED` | 登录游戏服失败或连接中断：检查私钥是否已加到游戏服的 authorized_keys、`[ssh]` 的用户和端口、网络 |
| `HOST_KEY_MISMATCH` | 配置了 known_hosts，但其中没有该游戏服或公钥不符，更新 known_hosts 后重启服务 |
| `LOG_DIR_UNAVAILABLE` | 游戏服上该类型的非压缩目录不存在或不可读，或压缩目录存在但不可读，检查 `[log_types]` 的 `dir` 与 `zip_dir` |
| `REMOTE_TOOL_UNAVAILABLE` | 游戏服缺少 `find`、`grep` 或 `gzip` 等命令 |
| `TIMEOUT` | 在开始读取日志前就用完了单次调用时限（`limits.query_timeout_s`），通常是 SSH 连接慢 |
| `BUSY` | 并发查询已满（`limits.max_concurrent`、`limits.max_concurrent_per_server`），稍后重试或调大上限 |
| `AUDIT_FAILED` | 审计日志写不进去，检查 `deploy/audit/` 的磁盘空间和权限 |
| 查询结果的时间对不上 | `server_timezone` 与游戏服实际时区不一致，用 `date +%z` 核对 |
