# 许可证与二次开发政策

适用范围:本仓库所有代码、容器镜像、前端依赖、字体、AI 模型、数据。
生效日期:2026-10-22。修改本政策需法务确认。

## 1. 许可证分级

| 级别 | 许可证 | 规则 |
|---|---|---|
| **A 可直接用** | MIT、BSD-2-Clause、BSD-3-Clause、Apache-2.0、ISC、PSF、PostgreSQL License、Unlicense、CC0-1.0、Zlib | 保留版权与许可声明;Apache-2.0 另需保留 NOTICE |
| **B 有条件** | MPL-2.0、LGPL-2.1/3.0、EPL-2.0、CC-BY-4.0(数据/文档)、OFL-1.1(字体) | 见下方条件,须在 THIRD_PARTY.md 登记并写明用法 |
| **C 默认禁止** | GPL、AGPL、SSPL、BSL、Elastic License、RSALv2、Commons Clause、CC-BY-NC、CC-BY-SA(代码)、"仅限研究/非商用"、带使用限制的模型许可(如 Llama 社区许可) | 例外须法务书面批准并记录在 THIRD_PARTY.md |

**B 级条件**
- **MPL-2.0**:不修改上游文件即可;修改了的文件必须按 MPL 公开。
- **LGPL**:只能动态链接(Python/JS 以正常 import 使用),**不得修改源码**,不得静态打包进专有二进制;须保留许可声明并允许用户替换该库。要修改须法务批准。
- **OFL-1.1(字体)**:可嵌入 PDF、可随软件分发,不得单独售卖字体;子集化或转换格式视为修改,要核对"保留字体名(RFN)"条款,并附上版权与 OFL 全文。
- **CC-BY**:保留署名。

## 2. 已识别的禁用/慎用项

| 场景 | 禁用 | 替代 |
|---|---|---|
| PDF 解析 | PyMuPDF/MuPDF(AGPL)、iText(AGPL) | pypdfium2、pdfminer.six、pypdf |
| OCR 流水线 | OCRmyPDF(依赖 Ghostscript,AGPL) | Tesseract(Apache-2.0)加自建流水线 |
| 文档解析模型 | Marker、Surya(GPL 加模型限制) | Docling(MIT;模型许可逐个核对) |
| 缓存/队列 | Redis 7.4+(RSALv2/SSPL;8.x 增加 AGPL 选项) | PostgreSQL 队列(Procrastinate);确需 Redis 用 Valkey |
| 搜索 | Elasticsearch/Kibana(SSPL/AGPL/ELv2) | PostgreSQL 全文加 pgvector;需要时 OpenSearch(Apache-2.0) |
| 图数据库 | Neo4j Community(GPL)、Memgraph(BSL)、ArangoDB(BSL) | PostgreSQL 关系加递归查询;或 Apache AGE(Apache-2.0) |
| 对象存储 | MinIO、Garage(AGPL) | 本地文件系统加 S3 兼容接口 |
| 监控 | Grafana、Loki(AGPL) | OpenTelemetry 加 Prometheus |
| 数据库 | MySQL(GPL)、MySQL Connector/J | PostgreSQL |
| 前端富文本/表格/图表 | CKEditor 5、TinyMCE 7(GPL)、Handsontable、Highcharts(非商用) | Tiptap/ProseMirror 核心(MIT)、ECharts(Apache-2.0) |
| 大模型权重 | Llama、Gemma 等带使用限制的许可 | 优先 MIT/Apache-2.0 权重,**逐个核对许可文本** |
| 图标 | 阿里 iconfont 上传图标(版权不明)、Font Awesome Pro | Lucide(ISC)、Phosphor(MIT) |
| 开发环境 | Docker Desktop(较大企业需付费订阅) | 服务器用 Docker Engine,开发机用 Podman 或购买订阅 |

## 3. 引入新依赖的流程

1. 在 PR 描述里写明:名称、版本、许可证(查包的 LICENSE 文件,不要只信 PyPI/npm 的元数据字段)、用途、是否传递依赖了 C 级许可证。
2. 在 `THIRD_PARTY.md` 登记。
3. CI 许可证检查通过。B 级条目需有评审人签字,C 级需法务批准。
4. 依赖版本锁定(`uv.lock`、`pnpm-lock.yaml`),升级走 PR 并重跑检查,防止上游中途更换许可证。

## 4. 二次开发规范

1. **默认当库使用,不 fork。** 通过正常依赖引入,不拷贝源码进仓库。
2. **确需修改上游时**:放入 `third_party/<项目名>/`,保留原 `LICENSE`、`NOTICE`、版权头;修改以补丁形式放 `third_party/<项目名>/patches/`;在 `README` 记录上游地址、基线版本/提交、修改原因;尽量向上游回馈。
3. **复制代码**:无许可证的仓库默认保留全部权利,**不得复制**;StackOverflow 代码为 CC-BY-SA,不得直接粘贴进产品;博客/教程代码先确认许可。
4. **AI 生成代码**:入库前走正常评审,警惕与开源代码高度相似的片段。
5. **一期平台代码**:新平台**不拷贝一期源码**,只迁移经核对的数据与经验。

## 5. 交付物

- 每次发布生成 CycloneDX SBOM(Python 与 JS 各一份),随交付件提供。
- 产品内「开源许可声明」页(用户端与管理端),内容由 `scripts/gen_notices.py` 之类的脚本从锁定依赖生成。
- 容器镜像保留基础镜像的许可证文件。

## 6. 数据与模型

- 法规原文的版权与使用条款登记在 `SOURCE_TERMS.md`,**对外付费展示全文前须法务确认**。
- 发送给外部大模型 API 的内容默认脱敏,不含个人信息与企业敏感信息;须保留切换本地模型的能力。
- 模型权重与 API 的服务条款在 THIRD_PARTY.md 登记。
