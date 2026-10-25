# 车研数合 IP 原型部署与运维

记录日期：2026-10-25。上线主机由用户确认为 **47.116.46.81**，使用 IP，不迁移旧域名。

## 当前交付

| 项目 | 实际状态 |
|---|---|
| 企业入口 | <https://47.116.46.81/> |
| 管理入口 | <https://47.116.46.81/admin/> |
| 登录保护 | 数据 API 未登录 401；两端沿用恢复数据中的原账号 |
| HTTP | 80 跳转 443；可信 IP 证书，未绕过浏览器安全提示 |
| 主目录 | `/opt/cyshuhe/current` → `/opt/cyshuhe/releases/s12-stage` |
| 数据库 | PostgreSQL 16 / pgvector；迁移至 `0022` |
| 运行角色 | `cyshuhe_runtime`，非所有者、非超级用户、无 BYPASSRLS；11 张 FORCE RLS 业务表 |
| 原文 | `cyshuhe_archive` Docker 卷；API 与 worker 共用；80 个内容去重文件对应 81 个版本 |
| 数据库卷 | `cyshuhe_pgdata`；不公开 5432 |
| 进程 | db、api、worker、web-user、web-admin、nginx；migrate 成功后退出 |
| 密钥 | 内存经 SSH stdin 注入容器环境；没有保存到 `.env`、代码、文档、日志或构建参数 |
| 引用策略 | 新任务默认 `allow_machine_with_notice`，逐项标明待核对；历史状态保持 |
| 原项目 | 三个旧容器停止，镜像、卷和源目录保留；未执行 `down -v` |

最终镜像标签、ID 与提交见部署 `deploy/release.json` 和 [最终上线清单](../reviews/s12-release-public-final.json)。应用基础提交 `9097be3`，最后聊天等待态与Word分页修正为 `f8d510e`，线上镜像已切换至该标签；文档/验收记录可有更晚提交。`s12-stage` 是保留的发布目录名，不表示当前仍只有预发布可访问。

## 机器资源与适用范围

当前主机 2 vCPU、实际约 1.6 GiB RAM、2 GiB swap、40 GiB 磁盘。Compose 给数据库 320 MiB、API 420 MiB、worker 320 MiB、三个 Nginx 各 48 MiB 上限；另需为系统、页缓存与 Docker 留余量。日志 `json-file` 每容器 10 MiB × 3 轮转。

这是低配演示原型。尚未完成 100 人在线、20 个 AI 报告同时执行或高可用验收；请勿把单次报告、32 题串行评测当作容量测试。外部模型费用按服务商账单，系统 tokens 是可观测用量，可能包含缓存复用，不等于账单金额。正式扩容按此前容量方案重新压测和配置并发预算。

旧、新项目并存和评测期间曾出现高 IO 等待、SSH/接口变慢；没有证据证明一定是 OOM。切换时停止旧栈后恢复。新栈设置内存上限、日志轮转，保留失败证据，不冒称已定位所有宿主机问题。

## 构建与首次部署

仓库目录结构：`backend/` 为 FastAPI/Pydantic/Alembic，`apps/web-user` 与 `apps/web-admin` 为 Vue，`deploy/` 为 Compose/网关。

### 标准联网环境

1. 安装可信来源的 Docker/Compose，准备受保护的独立发布目录；先备份现有系统。
2. 在运维会话环境提供 `POSTGRES_PASSWORD`、`CYSH_RUNTIME_DB_PASSWORD`、`CYSH_JWT_SECRET`、`CYSH_LLM_API_KEY`；不要写入仓库或命令历史。数据库口令采用 URL 安全字符，或正确 URL 编码。
3. 按实际入口设置 `CYSH_CORS_ORIGINS` JSON、模型地址/名称和 `CYSH_RELEASE`。默认代码模型为 `deepseek-flash`，兼容 API 可另配；没有密钥时不能声称真实模型分析完成。
4. 新空库可执行 `docker compose -f deploy/docker-compose.yml up -d --build`。先等待数据库健康，再由所有者迁移/授权，API 和 worker 才使用普通运行角色启动。默认仅绑定 `127.0.0.1:18080` 做预检。
5. 若恢复现有资料，先仅启动 db，在**新空库**恢复 dump 与原文卷，校验哈希，再执行迁移和启动。不得在现有业务库上使用 `--clean`，不得删除旧库。原文卷应由容器 UID 10001 可读写；仅允许已核验原文进入解析流程。
6. 两端标准 Dockerfile 使用 Node 构建 → Nginx。管理端构建基路径为 `/admin/`，用户端 `/`；同源 `/api` 转发，避免把开发端口写进生产包。
7. 验证鉴权、五国目录、原文/译文/图谱、版本激活、一次真实任务、整份/单章导出后再开放入口。

不要直接复制含环境的 `docker inspect` 输出到聊天或 Git。普通用户能访问 Docker socket 即可读取容器密钥，Docker 权限须按管理员权限管理。

### 本次受限网络与低内存路径

服务器直连 Docker Hub 不稳定，本次从官方 registry 下载并校验平台 digest/归档哈希后 `docker load`；Python 生产 wheels 对照 `uv.lock` 哈希离线安装。基础为 Python 3.12-slim-bookworm、Nginx 1.28-alpine、pgvector:pg16；原始清单见私有构建归档，公开提交不含镜像二进制。

构建参数使用固定 uv 0.13.5 的官方 PyPI Linux wheel生成本地工具镜像，`UV_IMAGE=cyshuhe-build-uv:0.13.5`、`UV_OFFLINE=1`。离线 `uv pip install --require-hashes --no-index --find-links` 安装从锁文件导出的 Linux requirements；不是使用未锁版本替代。APT 使用 HTTPS 镜像且保留 Debian 签名验证。

前端本机执行锁定依赖安装、typecheck 和生产构建，再用 `deploy/Dockerfile.prebuilt` 制作静态镜像。准备包含该 Dockerfile、`static.conf` 和本次 `dist/` 的独立构建上下文，执行 `docker build -f Dockerfile.prebuilt -t <标签> .`，避免在 1.6 GiB 主机运行 Node 构建。标准多阶段文件已提供，但不声称本次在本机未启动的 Docker Desktop 上做过 Compose 全栈验收。

PDF 运行库为动态链接 Pango/HarfBuzz/GLib，镜像保留原生许可；Word 仅声明字体名，PDF 注册随包 SHA-256 验证字体。未使用的 Pyphen 字典不进入最终镜像；空资源目录保留以满足初始化。见 [字体策略](../compliance/FONTS.md)、[Linux Python 环境](../compliance/python-linux-runtime.json) 和 `NOTICE`。

## IP HTTPS 与自动续期

本次使用 Certbot 5.4.0 及 Let's Encrypt 的短期 IP 证书，证书路径 `/etc/letsencrypt/live/cyshuhe-ip/`。IP 证书有效期短，必须持续续期；机制依据 [Let's Encrypt 官方说明](https://letsencrypt.org/2026/03/11/shorter-certs-certbot)。

- Certbot 隔离环境：`/opt/cyshuhe/certbot`。
- ACME webroot：`/var/lib/cyshuhe/acme`；公网 80 的 `/.well-known/acme-challenge/` 保持可达。
- auth/cleanup hook：`/opt/cyshuhe/acme-hooks/auth.py`、`cleanup.py`，只接受经过正则验证的 token 文件名。
- reload hook：`/opt/cyshuhe/acme-hooks/reload.sh`，成功续期后 `docker exec cyshuhe-nginx-1 nginx -s reload`。
- systemd：`cyshuhe-cert-renew.service` 与 `cyshuhe-cert-renew.timer`，每日 00:15/12:15 加随机延迟，Persistent=true。
- 本次执行 `certbot renew --cert-name cyshuhe-ip --dry-run --run-deploy-hooks` 成功，真实验证 ACME 挑战与重载链路；staging 证书也保留，但 timer 只操作正式 `cyshuhe-ip`。

`deploy/docker-compose.tls.yml` 将公网 80/443、证书目录和挑战目录只读挂载到网关。不要拷贝私钥到 Git，不要以忽略证书验证代替正确部署。

排查命令（只在主机管理员会话执行）：

```bash
sudo systemctl status cyshuhe-cert-renew.timer --no-pager
sudo journalctl -u cyshuhe-cert-renew.service --since '7 days ago' --no-pager
sudo /opt/cyshuhe/certbot/bin/certbot certificates
sudo docker exec cyshuhe-nginx-1 nginx -t
```

## 日常启动与更新

现有部署使用 `deploy/compose_runtime.py` 复用受保护的容器环境，不落 `.env`。它读取 `deploy/release.json` 的镜像标签及 `tls_enabled`，因此普通升级不会意外移除 HTTPS。仅允许 up/stop/start/ps/restart/logs 与 `config --quiet`；禁止打印含密钥的完整配置。

```bash
cd /opt/cyshuhe/current
sudo python3 deploy/compose_runtime.py config --quiet
sudo python3 deploy/compose_runtime.py ps
# 发布前先备份、确认没有正在执行的任务，再按已校验标签升级：
sudo python3 deploy/compose_runtime.py up -d --no-build
```

Nginx 使用 Docker DNS `127.0.0.11`、共享 upstream zone 与 `resolve`，10 秒刷新，避免容器替换后指向旧 IP。行为依据 [Nginx 官方 upstream 文档](https://nginx.org/en/docs/http/ngx_http_upstream_module.html)。不要在有执行中任务时强制重建 API；当前报告流水线在 API 进程执行，进程重启可能中断任务，轨迹会保留，需按失败原因恢复。

## 数据保护、备份与回滚

### 本轮保全证明

- 本机真实源库 `remediation_s5_sources_20261025` 仍保留原 `0016`，未就地升级；完整五国库与只有两个测试文件的 S6 隔离库严格区分。
- 恢复到服务器新库再增量迁移至 `0022`。20 张表、31,007 行的原有字段摘要一致，见 [恢复证明](../reviews/s12-server-restore-preservation.json) 与 [迁移证明](../reviews/s12-server-migration-preservation.json)。这不表示后续新任务不会追加记录。
- 原始 dump 18,663,878 bytes，SHA-256 `3dae5dccbbeeac2a98275c93288274fde2280e5771ab5333c7d0b74f4c9c96a2`。
- 原文预检 81 个版本全部匹配，去重 80 个文件；旧数据、审批、失败历史没有为了改善统计而删除。

### 旧项目备份

`/opt/cyshuhe-backups/20261025T102726Z-pre-cutover`：旧项目源码、MySQL 全库、运行文件、镜像归档与私有容器元数据。目录 0700，含密钥的元数据 0600，不对外提供。

冻结旧前后端后另做最终备份 `/opt/cyshuhe-backups/20261025T140855Z-final-cutover`：

| 文件 | bytes | SHA-256 |
|---|---:|---|
| mysql-all.sql.gz | 996,809 | `95ecfe42b18dbe2f5fe5320de139e544e4f261b3db00bcffc320d8d0698fa58d` |
| backend-runtime.tar.gz | 2,835,070 | `4a06b8feaa07368bd4ca692f401a855be95f775b1373675b9f0d8a7619501d74` |

已校验 gzip CRC、归档可读性和哈希；没有做旧 MySQL 的隔离恢复演练，不把这些检查称为恢复成功。

### 后续备份要求

对新库使用 PostgreSQL `pg_dump -Fc`，同时备份 `cyshuhe_archive`、发布清单与受保护的运行配置。备份目录必须 0700，不能在 webroot；备份再计算 SHA-256，并定期在独立库做恢复验证。法律数据与审批审计不得通过复制新 dump 覆盖掉上线后的记录。定期备份与异地留存策略需要运维方安排；证书续期 timer 不是数据备份任务。

最后界面修正发布前，另备份了新平台（包含本次新任务与评测）：`/opt/cyshuhe-backups/20261025T150629Z-new-platform`。dump 19,195,723 bytes，原文归档35,429,912 bytes，哈希见 [清单](../reviews/s12-new-platform-backup.json)；未把容器环境或API Key写入该备份。归档CRC已核验，本步骤未做该新dump的隔离恢复，不混称恢复演练完成。

### 回滚顺序

1. 暂停新业务写入、记录正在运行/等待追问的任务，对新库和原文卷做当前备份。
2. 同一 PostgreSQL 数据模型的代码回退：选择已测试兼容当前迁移的镜像标签，保留卷，更新 release 清单后启动；不能盲目执行 Alembic downgrade。
3. 回到原旧项目：先停止新公网网关以释放 80/443，再启动旧数据库、旧后端、旧前端（`multiagent-online-database-1`、`multiagent-online-backend-1`、`multiagent-online-frontend-1`）。旧数据是切换时刻的旧系统状态，新系统期间追加的任务需另行迁移，不能承诺自动双向同步。
4. 验证旧服务后再开放业务。任何恢复到已有库前都必须先保存现状；禁止 `docker compose down -v`、删除旧镜像/卷、无备份覆盖数据。

## 验证与尚未完成的验收

公开入口通过可信 HTTPS、HTTP 跳转、健康检查与未登录 401；已有账号登录成功。五国目录、真实译文、图谱、工作流激活、报告预览及整份/单章/压缩包下载已在实际浏览器操作。PDF 全份 19 页、D3 三页、Word 渲染 14 页逐页检查；后续分页优化记录在单独复核文件。

最终全量后端 554 passed（519.93秒）；12项反例12 passed（14.25秒）；两端typecheck/生产构建与279条应用依赖许可证扫描通过。实际Debian原生包清单另见 [native-linux-runtime.json](../compliance/native-linux-runtime.json)，自动采集的许可标签不是法务批准。32题评测、技术失败原记录与重试结果另见 [总结果](remediation_result.md)。独立 `verify_ui_clickthrough.py` 会写隔离测试数据，新增 `CYSH_UI_ISOLATED_TESTS=1` 防误用，未在真实五国线上库运行它；本轮实际浏览器使用等价手动路径，并如实记录未覆盖的自动脚本项。

法律人工复核、正式来源条款/金标确认、埃及等原文/OCR 缺口、备案适用性及容量验收仍待负责人员处理。公网原型可演示不等于全部正式验收通过。
