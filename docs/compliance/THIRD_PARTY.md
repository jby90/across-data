# 第三方依赖登记

> 直接依赖登记。传递依赖由 CI 的许可证扫描(`scripts/check_licenses.py`)和 SBOM 覆盖。
> **许可证一栏为初判,以包内 LICENSE 文件为准;CI 扫描结果与此表不一致时,以扫描结果为准并更新本表。**

## 后端(Python)

| 包 | 许可证(初判) | 级别 | 用途 | 使用方式 | 是否修改 | 审批 |
|---|---|---|---|---|---|---|
| fastapi | MIT | A | Web 框架 | 依赖 | 否 | — |
| uvicorn | BSD-3-Clause | A | ASGI 服务器 | 依赖 | 否 | — |
| sqlalchemy | MIT | A | ORM | 依赖 | 否 | — |
| asyncpg | Apache-2.0 | A | PostgreSQL 异步驱动 | 依赖 | 否 | — |
| alembic | MIT | A | 数据库迁移 | 依赖 | 否 | — |
| pydantic / pydantic-settings | MIT | A | 数据校验/配置 | 依赖 | 否 | — |
| email-validator | Unlicense | A | 邮箱校验 | 依赖 | 否 | — |
| pyjwt | MIT | A | JWT | 依赖 | 否 | — |
| argon2-cffi | MIT | A | 密码哈希 | 依赖 | 否 | — |
| httpx | BSD-3-Clause | A | 采集 HTTP 客户端(P1 起为生产依赖) | 依赖 | 否 | — |
| beautifulsoup4、soupsieve | MIT | A | HTML 链接发现与网页文本提取 | 依赖 | 否 | — |
| pypdfium2(含 PDFium) | Apache-2.0 OR BSD-3-Clause(PDFium 为 BSD-3-Clause/Apache-2.0) | A | PDF 文字层提取、页面渲染(OCR 用) | 依赖 | 否 | — |
| pillow | HPND | A | 图像处理(OCR 渲染页面) | 依赖 | 否 | — |
| pytesseract | Apache-2.0 | A | Tesseract 命令行封装 | 依赖 | 否 | — |
| Tesseract OCR 5.5(外部程序) | Apache-2.0 | A | 扫描件 OCR,以独立进程调用,不链接进产品 | 独立进程 | 否 | — |
| Tesseract 语言包 eng/ara/fra(tessdata_fast) | Apache-2.0 | A | OCR 语言模型,来源 github.com/tesseract-ocr/tessdata_fast;SHA-256:eng `7D4322BD…170B2`、ara `E3206D3D…3E729`、fra `CED03756…445CA` | 数据文件 | 否 | — |
| Tesseract 阿语语言包 ara(tessdata_best) | Apache-2.0 | A | 仅作隔离 OCR 候选探测，未替换线上语言包；[官方仓库](https://github.com/tesseract-ocr/tessdata_best)，SHA-256 `ab9d157d8e38ca00e7e39c7d5363a5239e053f5b0dbdb3167dde9d8124335896`；来源与探测记录见 `docs/reviews/s5-sources-20261025-ocr-provenance.json` | 数据文件 | 否 | — |

开发/测试:pytest、pytest-asyncio、httpx、ruff(均为宽松许可证)、pip-licenses(MIT)、pgserver(嵌入式 PostgreSQL,**仅测试**,不进入生产镜像;其内含 PostgreSQL 二进制,PostgreSQL License)。

## 基础设施

| 组件 | 许可证(初判) | 级别 | 说明 |
|---|---|---|---|
| PostgreSQL | PostgreSQL License | A | 主库 |
| pgvector | PostgreSQL License | A | 向量检索(后续) |

## 前端(JavaScript,`apps/web-user`、`apps/web-admin`)

| 包 | 许可证(初判) | 级别 | 用途 | 是否修改 |
|---|---|---|---|---|
| vue | MIT | A | 框架 | 否 |
| vue-router | MIT | A | 路由 | 否 |
| pinia | MIT | A | 状态管理 | 否 |
| element-plus | MIT | A | UI 组件库 | 否 |
| vite、@vitejs/plugin-vue | MIT | A | 构建(开发依赖) | 否 |
| typescript | Apache-2.0 | A | 类型检查(开发依赖,锁定 ~5.9 以兼容 vue-tsc) | 否 |
| vue-tsc | MIT | A | 单文件组件类型检查(开发依赖) | 否 |

> 前端传递依赖由 `scripts/check_licenses.py --js ../apps/web-user ../apps/web-admin` 扫描。
> Element Plus 目前为全量引入(包体约 1 MB),后续改为按需引入。

## 传递依赖中的 B 级条目

| 包 | 许可证 | 条件与处理 |
|---|---|---|
| certifi(Python,httpx 的传递依赖,开发/测试环境) | MPL-2.0 | 不修改即可;需保留许可声明。若进入生产镜像,随 NOTICE 一并列出 |
| lightningcss(JS,构建工具的传递依赖) | MPL-2.0 | 仅作构建工具使用,未修改;若其代码进入产物,需随 NOTICE 列出并确认 |

## AI 模型与 API(待补)

S12 新增：WeasyPrint 70.0（BSD-3-Clause）、pydyf/cssselect2/tinycss2/webencodings（BSD）、tinyhtml5（MIT）、fonttools/Brotli（MIT）、Zopfli（Apache-2.0），均作为未修改库使用。实际版本和许可全文随 `licenses.json`、`THIRD-PARTY-NOTICES.txt` 与 `sbom-lock.json` 生成。

Pyphen 0.18.1 的 Python 代码由上游明确三选一授权，本发行选择 MPL-1.1，保留代码、许可证和来源 https://github.com/Kozea/Pyphen；生产镜像删除全部未使用的 dictionaries，避免分发独立授权的字典，报告禁用自动断字。未修改源码，可从对应版本源码取得；工程登记不等于人工法务审批。

Pango/GLib/FriBidi 等原生动态库使用 Debian 原包（LGPL），HarfBuzz 为 MIT、FreeType 采用 FTL。保留 `/usr/share/doc/*/copyright`，不静态链接、不修改，可以自行替换动态库；对应源码通过 Debian Sources/包管理器按部署记录的版本获得。Windows MSYS2 包仅用于本机 QA，不进入产品镜像。镜像最终 [164 个 Debian 包、源包版本、原始许可标签及版权文件哈希](native-linux-runtime.json) 已实采，164 个版权文件均存在。清单也包含基础系统工具，其未标准化的许可标签不表示应用依赖自动准入或法务批准；279 条应用依赖扫描的范围与操作系统清单不同。

字体使用 Fontsource WOFF2 和固定提交的 Google Fonts TTF，OFL-1.1，来源、SHA-256、RFN 和子集策略见 [FONTS.md](FONTS.md)。nginx 为 BSD-2-Clause，Node/uv 仅构建使用（MIT / MIT OR Apache-2.0）。

| 名称 | 许可/条款 | 用途 | 数据出境说明 | 状态 |
|---|---|---|---|---|
| DeepSeek API | 服务条款待核对 | 翻译/抽取/分析 | 发送内容含境外传输风险,默认脱敏 | 待核对 |
