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

| 名称 | 许可/条款 | 用途 | 数据出境说明 | 状态 |
|---|---|---|---|---|
| DeepSeek API | 服务条款待核对 | 翻译/抽取/分析 | 发送内容含境外传输风险,默认脱敏 | 待核对 |
