# 车研数合 — 汽车数据跨境合规平台

SaaS 多企业使用的汽车数据跨境合规平台。三个业务模块:

1. **法规知识库**:定期抓取各国官方法规 → 解析/OCR → 机器翻译(受术语库约束)→ 律所复核 → 六要素/十二类关系知识图谱。
2. **安全报告**:问答引导采集信息 → 多 Agent 协作 → 八维度报告。
3. **申请表**:按出发国/目的国/场景判定所需申请表 → 问答收集字段 → 填充导出。

分**用户端**(车企用户)与**管理端**(平台管理员、复核员)。暂无门户。

## 目录

```
backend/       FastAPI 后端(Python 3.12)
apps/          前端(pnpm workspace):web-user 用户端、web-admin 管理端
deploy/        docker compose 与部署配置
docs/          架构、合规(许可证/字体/数据来源条款)、产品文档
scripts/       工程脚本(许可证检查等)
data/seed/     从一期迁移的种子数据(带校验清单)
```

## 本地开发

```powershell
# 1. 后端(注意:虚拟环境必须放在纯 ASCII 路径,嵌入式 PostgreSQL 在含中文的路径下无法初始化)
cd backend
$env:UV_PROJECT_ENVIRONMENT = "C:\Users\<你>\.venvs\cyshuhe"
uv sync --python 3.12
uv run pytest -q

# 2. 配置(密钥只放环境变量或 .env,不要提交)
copy ..\.env.example ..\.env   # 然后填写 CYSH_JWT_SECRET

# 3. 数据库迁移与创建首个平台管理员
uv run alembic upgrade head
uv run python -m app.cli create-platform-admin --email you@example.com

# 4. 启动 API(8000)与后台 worker(负责定时采集)
uv run uvicorn app.main:app --port 8000
uv run python -m app.worker            # 常驻;加 --once 只跑一轮

# 5. 前端
cd .. ; pnpm install ; pnpm dev:user ; pnpm dev:admin
```

## 采集流程(P1)

管理端「采集站点」新增站点(默认停用)→ **确认来源使用条款并写明依据**(才能启用)→ 启用后由 worker 按间隔定时抓取
→ 发现原文 → 遵守 robots.txt 礼貌下载(限速、体积上限、防内网地址)→ 原文按内容哈希归档 → 解析 → 切分条款。
内容变化时生成新版本,旧版本与条款保留。没有文字层的 PDF 标记为"需要 OCR",不会冒充已解析。
开发机若开着代理软件的 fake-IP 模式,域名会被解析到 198.18.0.0/15 而被拦截,可在 `.env` 里设置
`CYSH_CRAWL_ALLOW_RESOLVED_CIDRS=["198.18.0.0/15"]`(生产保持为空)。

## 商用合规

本项目面向商用,**引入任何开源依赖、字体、数据前必须先看 `docs/compliance/`**:

- [LICENSE_POLICY.md](docs/compliance/LICENSE_POLICY.md) — 许可证分级、禁用清单、二次开发规范
- [THIRD_PARTY.md](docs/compliance/THIRD_PARTY.md) — 直接依赖登记
- [FONTS.md](docs/compliance/FONTS.md) — 字体登记与使用规则
- [SOURCE_TERMS.md](docs/compliance/SOURCE_TERMS.md) — 各国法规来源的使用条款登记

CI 会对依赖许可证做门禁检查,命中禁止级许可证则构建失败。

## 版权

本项目为专有软件,版权与授权方式待与甲方确认后补充 `LICENSE`。
