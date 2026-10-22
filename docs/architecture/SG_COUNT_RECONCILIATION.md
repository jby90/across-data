# 新加坡法规数量对账与四国官方源梳理报告 (S3)

> 生成日期：2026-10-22  
> 关联步骤：`S3 四国法规采集补齐（先于翻译）`

---

## 一、新加坡（SG）法规数量三方对账

### 1. 口径差异核心结论

| 统计口径 / 维度 | 一期口头纪要 (2026-10-19) | 一期工程交接库 (`AutoDataCross/docs/phase2`) | 新平台基线 (`CrossBorderPlatform` S0) | 新平台 S3 补齐后 |
| :--- | :--- | :--- | :--- | :--- |
| **综合台账条目数** | “约 80 余条” | **82 条**（31 源记录 + 4 候选 + 30 义务 + 17 实体） | 不混用台账口径（严格按文档/版本/条款三级统计） | 同左 |
| **官方源文件数（Document）** | 未区分文件与台账条目 | **31 条源记录**（去重后 **29 份**唯一 SHA-256 官方文件，共 809 页） | **68 份文档**（首屏 `limit=50` 仅展示 43 份） | **68 份文档**（100% 含有效版本） |
| **已归档版本数（DocumentVersion）** | — | **29 个版本**（26 份立法文件 + 3 份监管指引） | **66 个版本**（2 份文档因网络/403 缺版本） | **68 个版本**（补齐 2 份缺失版本） |
| **解析条款数（Clause）** | — | **1,200 条主记录** | **1,194 条** | **1,271 条**（全库五国合计 1,895 条） |

### 2. 为什么此前排查时误以为新平台只有“43 份新加坡文档”？

1. **接口默认分页截断**：`GET /api/admin/documents` 默认参数为 `limit=50, offset=0`，并按 `discovered_at DESC` 排序。
2. **首屏被四国文档占用 7 个名额**：未传分页参数拉取前 50 条文档时，排在最前面的 7 份文档属于四国（`EG` 1 份、`LB` 1 份、`MA` 3 份、`TN` 2 份），因此首屏 50 条中恰好剩余 `50 - 7 = 43` 份新加坡文档。
3. **全量翻页核实**：通过 `scripts/audit_platform.py` 按 `limit=200` 循环翻页拉取全表，新平台实际存有 **68 份**新加坡法规文档，不但完整包含一期全部 **29 份**唯一 SHA-256 官方文件（对应 `sources.json` 的 31 条 URL），还通过新加坡电子宪报（`egazette.gov.sg`）公开检索接口增量发现并归档了 **37 份**宪报法规/修正案文件。

### 3. 为什么一期口头纪要称“约 80 余条”？

核查 `AutoDataCross/docs/phase2/四段进度核对_20261019.txt`（第 9 行）及 `AutoDataCross/docs/phase2/data/current/` 目录下的 JSON 快照：
- `sources.json`：**31 条**官方来源记录（其中 29 份具备独立 SHA-256 归档文件：26 份法案/附属立法/修正案 + 3 份 PDPC/LTA 监管指引）；
- `source_discovery_candidates.json`：**4 条**目录发现候选记录；
- `obligations.json`：**30 条**提炼出的法定合规义务条目；
- `entities.json`：**17 条**法定监管机构与义务主体条目。

一期汇报时将上述汇总台账（`31 + 4 + 30 + 17 = 82` 条）统称为“约 80 余条”；而一期工程检查点文件 `stage2/checkpoint.json` 与 `四段进度核对_20261019.txt` 明确记录的实际官方原文为 **29 份官方材料、809 页、1,200 条条款主记录**。

### 4. 新平台 2 份缺版本（`latest_version = null`）SG 文档的补抓与修复

在 S0 基线审计中，68 份 SG 文档里有 66 份具备完整版本，仅以下 2 份 `latest_version` 为 `null`：
1. **`https://assets.egazette.gov.sg/2021/Legislative%20Supplements/Subsidiary%20Legislation%20Supplement/735.pdf`**
   - **标题**：*Personal Data Protection Act 2012 - Personal Data Protection (Notification of Data Breaches) (Amendment) Regulations 2021*
   - **原失败原因**：首次电子宪报批量抓取时源站连接偶发断开（`Server disconnected without sending a response`）。
   - **S3 补抓结果**：经 `PoliteFetcher` 重新抓取，源站正常返回 `HTTP 200`（`application/pdf`，37,435 字节，SHA-256 `b53f28e88005471bc8680a61951822aa417615239bd59be705a21d4cf80a7ee3`），已完成归档与条款解析。
2. **`https://sso.agc.gov.sg/Act/PDPA2012/`**
   - **标题**：*Personal Data Protection Act 2012 (Singapore Statutes Online)*
   - **原失败原因**：Singapore Statutes Online 网页端开启了 Cloudflare 挑战，直接请求 HTML 入口返回 `HTTP 403`。而同一站点下的一期官方 PDF 原文（`https://sso.agc.gov.sg/Act/PDPA2012?ViewType=Pdf&_=20250213000000`，SHA-256 `da6e324753dd171a50827692fcb733ea02dc22e2a3953034544c1aca4f6f5e52`，199 页，224 条款）已完整入库。
   - **S3 处置结果**：通过管理端合规兜底接口绑定官方已核验 PDF 原文归档，消除孤立无版本记录，确保 `GET /api/admin/documents` 中每份 SG 文档 `latest_version` 均非空且含 `content_hash`。

---

## 二、四国（EG / TN / MA / LB）官方入口、文档类型、语言与文件清单梳理

结合 `data/seed/source_sites.json`、`AutoDataCross/docs/phase2/stage4/official-channel-research.json`、`existing-pilot-attachments.json` 及本机实测抓取情况，四国官方源清单梳理如下：

### 1. 埃及（EG，目标 ≥ 3 份）

> **一期背景核实**：一期 `stage4/运行与核验说明.txt` 记录埃及 NTRA 物联网监管框架（31 页阿语原生文字 PDF）已从官方入口抓取入库；MCIT 与 PDPC 官网因源站 TLS 握手超时或 `robots.txt` 返回 HTTP 500 停止自动抓取。同时，`AutoDataCross/docs/policy_library/制度原文/埃及/` 下存有 3 份立项前期收集的埃及法规 PDF（《个人数据保护法》2020 年第 151 号法官方公报扫描件 31 页、《反网络和信息技术犯罪法》2018 年第 175 号法 23 页、《消费者保护法》2018 年第 181 号法 32 页）。
> **S3 处置方案**：保留 MCIT、PDPC、NTRA 官方站点配置并触发抓取，真实记录源站连接/robots 状态；对直连受阻的官方站点，通过管理端合规兜底上传入口（`POST /api/admin/documents/upload`）将埃及官方公报《2020 年第 151 号个人数据保护法》（扫描件，经 Tesseract `ara+eng` OCR 识别并标记 `ocr_required=true` / `needs_review=true`）及《2018 年第 175 号反网络和信息技术犯罪法》纳入对应官方站点下管理，并在 `metadata_source` 与流水线事件中如实标注来源出处与待人工复核状态。

| 序号 | 发布机构 (`publisher`) | 官方入口 (`entry_url` / `source_url`) | 文档类型 | 语言 | 文本来源 | 状态与说明 |
| :---: | :--- | :--- | :---: | :---: | :---: | :--- |
| 1 | 埃及国家电信监管局 NTRA | `https://www.tra.gov.eg/wp-content/uploads/2023/08/IoT-Framework-AR.pdf` | `REGULATION` | `ar` | `text_layer` | 官方直链已归档（31 页，SHA-256 `11566a8620fa...`），切分 20 条，状态 `pending_review` |
| 2 | 埃及通信与信息技术部 MCIT | `https://mcit.gov.eg/Upcont/Documents/Reports%20and%20Documents_1232021000_Law_No_151_2020_Personal_Data_Protection.pdf` | `LAW` | `ar` | `ocr` | 2020 年第 151 号《个人数据保护法》（31 页官方公报扫描件，SHA-256 `9b809f6d15e3...`），经 Tesseract 阿语 OCR 识别（平均置信度 75.1%），切分 13 条，标记 `ocr_required=true, review_status=pending_review` |
| 3 | 埃及内阁信息与决策支持中心 IDSC | `https://idsc.gov.eg/upload/DocumentLibrary/AttachmentA/10387/7.pdf` | `LAW` | `ar` | `text_layer` | 埃及数字技术/金融科技监管立法汇编（44 页原生阿语 PDF，HTTP 200 实抓归档，SHA-256 `e88076a37faf...`），切分 36 条，状态 `pending_review` |
| 4 | 埃及个人数据保护中心 PDPC | `https://pdpc.gov.eg/assets/pdf-data/Guidelines/DPOC.pdf` / `DSConsent.pdf` | `GUIDANCE` | `ar` | — | 源站连接超时，保留站点及 `CrawlRun` 失败原因记录（不伪造原文） |

### 2. 突尼斯（TN，目标 ≥ 3 份）

| 序号 | 发布机构 (`publisher`) | 官方入口 (`entry_url` / `source_url`) | 文档类型 | 语言 | 文本来源 | 状态与说明 |
| :---: | :--- | :--- | :---: | :---: | :---: | :--- |
| 1 | 突尼斯司法部 | `https://www.justice.gov.tn/fileadmin/medias/organisation_juridictionelle/Loi2004_63Arabe.pdf` | `LAW` | `ar` | `ocr` | 2004-63 号个人数据保护基本法阿语公报本（9 页，自定义 Type1 字体无 Unicode 映射，SHA-256 `7b12a8ddddba...`），经 Tesseract 阿语 OCR 识别（置信度 90.4%），切分 14 条，标记 `ocr_required=true` |
| 2 | 突尼斯国家统计局 INS | `https://www.ins.tn/sites/default/files-ftp3/files/2020-04/Loi%2063-2004%20Fr.pdf` | `LAW` | `fr` | `text_layer` | 2004-63 号个人数据保护基本法官方托管法语文本（19 页，SHA-256 `398b4b3499ff...`），原生文字层切分 100 条 |
| 3 | 突尼斯官方公报 JORT / 立法门户 | `https://legislation-securite.tn/wp-content/uploads/sites/5/2023/11/Decret-n%C2%B02007-3003-du-27-novembre-2007fr.pdf` | `REGULATION` | `fr` | `text_layer` | 2007-3003 号法令（规定 INPDP 运作程序，7 页，HTTP 200 实抓归档，SHA-256 `3df2d0562ab8...`），切分 16 条，状态 `pending_review` |
| 4 | 突尼斯国家个人数据保护局 INPDP | `https://www.inpdp.tn/ressources/decret_3003.pdf` | `REGULATION` | `fr` | — | 源站 TLS 握手超时，保留 `CrawlRun` 失败记录，由上项官方立法门户同源公报补齐 |

### 3. 摩洛哥（MA，目标 ≥ 4 份）

| 序号 | 发布机构 (`publisher`) | 官方入口 (`entry_url` / `source_url`) | 文档类型 | 语言 | 文本来源 | 状态与说明 |
| :---: | :--- | :--- | :---: | :---: | :---: | :--- |
| 1 | 摩洛哥国家个人数据保护委员会 CNDP | `https://www.cndp.ma/wp-content/uploads/2023/11/Loi-09-08-Fr.pdf` | `LAW` | `fr` | `text_layer` | 第 09-08 号个人数据保护法（29 页，SHA-256 `4b5e91054533...`），切分 68 条，`relevance=relevant` |
| 2 | 摩洛哥国家个人数据保护委员会 CNDP | `https://www.cndp.ma/wp-content/uploads/2023/06/Decret-2-09-165-FR.pdf` | `REGULATION` | `fr` | `ocr` | 第 2-09-165 号实施法令（5 页扫描件，SHA-256 `0a930a84d692...`），经 Tesseract 法语 OCR 识别（置信度 92.9%），切分 13 条，标记 `ocr_required=true, relevance=relevant` |
| 3 | 摩洛哥国家个人数据保护委员会 CNDP | `https://www.cndp.ma/wp-content/uploads/2023/11/CDAI-FORMULAIRE-DAI-Template-FRANCAIS.pdf` | `FORM` | `fr` | `text_layer` | CNDP 信息获取权申请表模板（4 页，SHA-256 `37710bb33ae3...`），S3 采集阶段标记 `relevance=irrelevant`（办事表格，供 S2 过滤，S3 不删除） |
| 4 | 摩洛哥总秘书处官方公报 SGG | `https://www.sgg.gov.ma/BO/bo_fr/2009/bo_5714_fr.pdf` | `LAW` | `fr` | `ocr` | 摩洛哥官方公报第 5714 期法文版（含第 09-08 号法律颁布诏书 Dahir n° 1-09-15，HTTP 200 实抓归档，SHA-256 `b03db6369837...`，置信度 91.8%，切分 38 条），标记 `ocr_required=true, relevance=relevant` |

### 4. 黎巴嫩（LB，目标 ≥ 2 份）

| 序号 | 发布机构 (`publisher`) | 官方入口 (`entry_url` / `source_url`) | 文档类型 | 语言 | 文本来源 | 状态与说明 |
| :---: | :--- | :--- | :---: | :---: | :---: | :--- |
| 1 | 黎巴嫩中央银行 BDL（官方托管） | `https://www.bdl.gov.lb/CB%20Com/Laws%20And%20Regulations/Laws/Law_81_AR%C2%A72572_1.pdf` | `LAW` | `ar` | `text_layer` | 2018 年第 81 号《电子交易与个人数据法》（43 页，SHA-256 `7c13a15bbbde...`），切分 133 条，`relevance=relevant` |
| 2 | 黎巴嫩议会 LP（官方托管） | `https://lp.gov.lb/backoffice/uploads/files/Laws%20whose%20implementation%20decrees%20have%20not%20been%20issued%20-%20update%20September%202%2C%202021.pdf` | `OTHER` | `ar` | `text_layer` | 黎巴嫩议会发布的《尚未颁布实施法令之法律官方统计清单（含第 81/2018 号法实施状态）》（90 页，SHA-256 `6898d1ad28e5...`，切分 171 条），修复 `robots.txt` 302 重定向处理后经 `PoliteFetcher` 实抓归档，`relevance=relevant` |
