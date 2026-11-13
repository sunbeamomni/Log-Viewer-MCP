# 集成测试环境

研发机器是 Windows，集成测试与依赖 POSIX 权限或 GNU grep 的测试都在 compose 中的测试运行容器里执行。环境中的密码与 SSH 密钥只用于测试：密钥在首次运行时由 keys 服务生成到 keys 卷，不进镜像，也不进仓库。

## 运行

```powershell
docker compose -f tests/integration/compose.yaml build
docker compose -f tests/integration/compose.yaml run --rm runner                      # 全部测试
docker compose -f tests/integration/compose.yaml run --rm runner python -m pytest tests/integration -x
docker compose -f tests/integration/compose.yaml down -v                              # 清理（-v 同时删除密钥、grep 记录与生产镜像的审计卷）
```

测试运行镜像与生产镜像（server-image）在构建时复制源码，改动代码后需要重新 build。server-image 在多次 `run` 之间持续运行，审计会累积，`down -v` 时清除。

## 服务

| 服务 | 地址 | 说明 |
|---|---|---|
| keys | 无网络 | 一次性任务：生成测试密钥与 known_hosts，已存在则跳过 |
| mysql | 172.30.77.10:3306 | MySQL 8.4，库 admin，表 tbl_webserver（见 docker/mysql/init） |
| gs | 172.30.77.11 | Debian 12 游戏服，系统自带工具加 openssh-server |
| gs-noionice | 172.30.77.12 | 同 gs，删除了 ionice |
| gs-grepspy | 172.30.77.13 | 同 gs，grep 换成记录 argv 后 exec 真 grep 的替身 |
| server-image | 172.30.77.20:8080 | 由 deploy/Dockerfile 构建的生产镜像，见"生产镜像" |
| runner | 172.30.77.100 | Python 3.11，以 uid 10001（tester，非 root）运行 pytest；等其余服务健康后才启动 |

游戏服共同点：

- root 用测试密钥登录（只允许密钥）；主机密钥每台不同，由 keys 服务生成。
- /root/.bashrc 会打印内容，其中有形似协议记录的文本（不含 NUL），并设置 BASH_ENV、GREP_OPTIONS、GZIP 等环境变量与别名；固定命令行应使它们全部失效。若远端 bash 读取了 BASH_ENV，输出中会出现 `BASH_ENV_SOURCED_MARKER`。
- /usr/local/ds/SFS2X 下有 logs、logpushinit、loganalys、polsarlog、ziplogs 五个目录，权限 777；镜像中为空，样例在容器启动时写入（见"样例日志"）。
- 入口脚本在启动 sshd 之前依次运行 /opt/testenv/init.d 中的可执行文件（样例日志由此写入，任务 5.2），完成后健康检查才通过。
- 使用 `init: true`，被终止的远端进程会被回收，不留僵尸进程。

## 生产镜像

server-image 由 deploy/Dockerfile 构建（构建上下文为仓库根目录，忽略规则 deploy/Dockerfile.dockerignore），以镜像自己的 ENTRYPOINT 与 CMD 启动，容器内以 root 运行，`init: true`。与生产的 deploy/compose.yaml 不同，它接入测试网络并使用固定地址，不用 `network_mode: host`：宿主机网络访问不到测试网络中的游戏服与 MySQL。生产 compose 的宿主机网络由 tests/unit/test_deploy.py 检查。

- Config_Dir：docker/server-image/config 只读挂载为 /etc/log-viewer-mcp。config.toml 监听 0.0.0.0:8080，密码直接写在 admin_db.password 中，`ssh.key_file` 指向 keys 卷中的 /keys/client/id_ed25519，不配置 known_hosts；tokens.json 用默认路径，登记一个测试专用 token（使用者 it-image，明文为 runner 的 LVM_IT_IMAGE_TOKEN）。结果目录与审计目录用默认值。
- 审计目录 /var/log/log-viewer-mcp/audit 是 image-audit 卷，只读挂载到 runner 的 /image-audit。审计文件由 root 以 0600 写入，tester 读不到，所以 runner 镜像装了 sudo，只允许 tester 不带参数运行 `sudo -n /usr/local/bin/it-image-audit`（按日期顺序输出全部 audit-*.jsonl），没有其他 sudo 权限。
- 健康检查用镜像中的 python 连接 127.0.0.1:8080；服务启动时要连上 MySQL，所以它等 keys 完成、mysql 健康后才启动。

test_image_smoke.py：不带 token 访问 /mcp 返回 401；经 stdio 启动 log-viewer-mcp-local 指向该服务，调用 list_servers 与 search_logs（参数同 test_e2e_smoke.py，时间窗口与真实时钟无关），确认本地结果文件与参照过滤逐字节相同、审计中新增了这两次调用的记录（只比较调用前后新增的行）。

## 样例日志

由 `sample_logs.py`（只用标准库）确定性地生成：构建游戏服镜像时在 samples 阶段运行，产出 `/opt/testenv/samples/samples.tar` 与 `manifest.json`（compose 以命名构建上下文 itsrc 把本目录交给 Dockerfile，游戏服镜像本身不装 Python）。`/opt/testenv/init.d/10-sample-logs` 在启动 sshd 之前清空五个日志目录并解包，属主 root、权限 0777、mtime 固定。测试中用 `sample_logs.build()` 得到同一份内容的内存模型（每个文件的内容、Covered_Period、标记行），游戏服上的 manifest.json 可经 `GameServers.manifest()` 读取。

时间都是服务器时间 GMT-2（样例的"当前时刻"为 2026-09-17 16:12:30）。四类目录的时段相同：

| 时间戳 | 粒度 | 位置 |
|---|---|---|
| 2026-09-14 | 天 | 只在 ziplogs |
| 2026-09-15 | 天 | 只在非压缩目录 |
| 2026-09-16-10 | 小时 | 两处都有（内容相同） |
| 2026-09-16-11 | 小时 | 只在 ziplogs |
| 2026-09-16-12、-14 | 小时 | 只在非压缩目录（-13 没有文件） |
| 2026-09-17-16-00、-01 | 分钟 | 只在 ziplogs |
| 2026-09-17-16-02、-03 | 分钟 | 两处都有 |
| 2026-09-17-16-04、-06 至 -09 | 分钟 | 只在非压缩目录（-05 没有文件） |
| smartfox.log | Current | 16:10 起 |

normal 另有只在 ziplogs、已截断的 2026-09-16-09 压缩包（gzip -dcf 以非 0 退出）。压缩文件按清理脚本的方式生成（gzip -c，头中带原文件名），名为 `smartfox.log.<时间戳><后缀>.tar.gz`。

- 每个日志文件恰有一行 `SAMPLE_MARK file=<log_type>/<时间戳或 current>`，可据此判断读了哪些文件。
- 日志行格式按 example/logback.xml；含异常堆栈、30–60 KB 的长行、中文、异步写入的上一周期末尾行，normal 16-07 与 login 16-02 含伪造帧行、命令片段与面向 AI 的指令。
- 应被忽略的条目（gc.log、boot/ 等子目录、名称合法的子目录、符号链接与悬空链接、怪文件名、ziplogs 中未配置或多余的后缀）内容都含 `SHOULD_NOT_BE_READ`、`CMD` 与 `uid=123`；它们多用没有日志的 16-05、-13 等时段的名称。完整清单见 manifest.json 的 ignored。

## 夹具（conftest.py、itenv.py）

| 夹具 | 说明 |
|---|---|
| it_env | LVM_IT_* 环境变量 |
| samples | `sample_logs.build()` |
| game_servers | 以 root 登录游戏服执行测试脚本（干净环境的 bash，丢弃 ~/.bashrc 的输出）；`snapshot`、`assert_unchanged`（日志目录、ziplogs、/tmp、/root 的文件集合、内容、大小、权限、属主、mtime，不比较 atime）、`manifest`、`reset_samples` |
| make_service_files、service_files | 在临时目录生成 config.toml、凭据副本（普通方式写入，权限取决于 umask）与 tokens.json（一个有效 token 与一个已吊销的 token）；可改 limits、known_hosts="wrong" 等 |
| restore_samples | `restore_samples(ip)` 登记后，测试结束时恢复该游戏服的样例 |

game_servers 的方法是协程；同步测试（例如 Hypothesis）用 `itenv.run_sync` 调用。未设置 LVM_IT=1 时本目录的测试全部跳过。

## tbl_webserver 测试数据

| svr_id | ip_inner | 预期 |
|---|---|---|
| 1001、2001（is_test=1）、4294967295 | 172.30.77.11 | gs |
| 1002 | 172.30.77.12 | gs-noionice |
| 1003 | 172.30.77.13 | gs-grepspy |
| 1004 | 172.30.77.99（无主机） | SSH_FAILED |
| 1005–1013 | 越界、前导零、空串、注入串、末尾换行、主机名、IPv6、NULL、非 ASCII 数字 | INVALID_SERVER_ADDRESS |

不存在的 svr_id（例如 9999）预期为 SERVER_NOT_FOUND。账号 logview 对 admin 库有全部权限（与线上沿用现有账号一致），只读由程序保证。

## 测试运行容器的环境变量

| 变量 | 值 |
|---|---|
| LVM_IT | 1（集成测试据此判断是否在容器中，未设置时应跳过） |
| LVM_IT_DB_HOST、LVM_IT_DB_PORT、LVM_IT_DB_NAME、LVM_IT_DB_USER、LVM_IT_DB_PASSWORD | 管理库连接参数 |
| LVM_IT_SSH_USER、LVM_IT_SSH_PORT | root、22 |
| LVM_IT_SSH_KEY | /keys/client/id_ed25519（属主 10001，权限 0600） |
| LVM_IT_KNOWN_HOSTS | /keys/known_hosts，每台游戏服一行 `<IP> ssh-ed25519 <公钥>` |
| LVM_IT_KNOWN_HOSTS_WRONG | /keys/known_hosts_wrong，同样的 IP 配另一把公钥（HOST_KEY_MISMATCH） |
| LVM_IT_GS_IP、LVM_IT_GS_NOIONICE_IP、LVM_IT_GS_GREPSPY_IP、LVM_IT_GS_UNREACHABLE_IP | 各游戏服地址 |
| LVM_IT_GREP_ARGV_DIR | /grep-argv，gs-grepspy 的 grep 调用记录 |
| LVM_IT_IMAGE_URL | http://172.30.77.20:8080，server-image 的地址 |
| LVM_IT_IMAGE_TOKEN | server-image 的 tokens.json 中登记的测试专用 token |

keys 卷以只读方式挂载。夹具（make_service_files）把需要的凭据复制到临时目录后再使用；服务不检查这些文件的属主与权限位。

## grep 调用记录

gs-grepspy 上每次调用 grep 生成一个 `<微秒时间戳>-<pid>.argv` 文件：argv[1] 起每个参数以 NUL 结尾依次写出（零个参数时为空文件），然后 exec 真 grep（/usr/lib/testenv/grep-real），输入、输出与退出码不变。记录目录在游戏服上是 /var/log/grep-argv，位于只读快照范围（日志目录、ziplogs、/tmp、root 主目录）之外；目录权限 0777，测试可以读取并删除记录。只读 `.argv` 文件，`.tmp` 是写入中的记录。
