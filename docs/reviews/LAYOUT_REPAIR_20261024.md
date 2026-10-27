# 剩余两条：原文解析、费用表切分与翻译修订

日期：2026-10-24。分支：`codex/remediate-s0-s3-s1-s2`。

## 结论与计数口径

两处解析问题已修复，在隔离副本 `remediation_20261023` 中新增两份页级解析修订和对应双语修订。20 个语言单元使用真实 DeepSeek 调用完成翻译，逐块及整单元按原有标准重新核验，20/20 通过；53 个数据字段原样保留并逐字段核验。这 53 个字段**不计为翻译通过**。

原始 PDF、旧条款、失败译文、图谱证据和审批记录全部保留。新修订均为 `pending_review`，`active=false`，`human_reviewed=false`，`usable_as_legal_basis=false`。旧的两条整条译文继续显示历史失败；没有将它们改成“翻译通过”，也没有把页级修订冒充原条款的完整重译。机器检查不代替人工语义或法律审核。

原先 108 条的历史统计仍为：88 条独立译文通过、18 条有真实上下文修订、本次 2 条有结构化页级修订。不能将此写成“108 条独立译文全部通过”。五国图谱既有重建结果没有在本次替换或重新批准，人工验收状态继续未通过。

## 两个问题如何修复

| 原记录 | 原问题 | 新的解析与核验 |
|---|---|---|
| SG `f0fd255b-0f1f-4ce5-80bc-da26ae8f56cd`，物理第 16 页 | 第三附表第 7 行与落款、姓名、文号拼成同一条款；数字及标识符挤占语言比例 | 保留第二附表 6 行、第三附表 7 行及各自表头、引用条例编号；落款独立。4 个语言单元通过原有翻译检查，17 个数据字段原样保留。`and above` 保持与第三附表第 7 行同一块、相同原文位置。表格行不冒充法律条号。 |
| EG `41ed474f-6426-49ea-9f61-d9d614bf74e5`，物理第 40 页、印刷页 33 | PDF 内容流使英文词序和链接倒序；原条款只截取该页部分参考文献 | 根据 PDF 字形位置恢复英文单栏阅读顺序，保留该页参考文献 14–29 共 16 项及 19 个链接字段；16 个语言单元通过原有翻译检查，36 个数据字段原样保留。此页是参考文献，不认定为埃及法律正文或有效法条。 |

两份原文件 SHA-256：

- SG：`ac1b9cd1914d7c00df391323f3d0952e0381974dab951e7782b4f4b895b8f75f`。
- EG：`e88076a37faf78473dcef5f67cf7ee7d34bd9960c38dcaf8d3a92e2f80785c06`。

英文阅读顺序修复先核对所有可见字形索引与非空字符总量，避免漏字、重复和任意补字；阿语及混合语言页不使用 LTR 重排。自动解析只对检测到倒序链接的英文参考文献页触发，其他正常页面沿用原路径。表格识别要求完整的两列表头及至少两行金额数据，普通附表仍按正常法条切分。跨页复杂表格未被宣称全面支持。

EG 两个 U+FFFE 源字符来自字体解码缺失。恢复为连字符前，必须与同页至少两个已正确编码的 `-` 字形在每点 12 像素渲染下具有完全一致的二值墨迹、尺寸。记录原读取值、字形索引、位置、对照字形和墨迹哈希；原始 PDF 与原始读取文本不改动。没有字形证明则拒绝修复，未根据 URL 语义猜字。链接换行及源文件自带的标点原样留存，未声称这些历史链接当前均可访问。

## 校验标准与数据保护

没有降低 CJK≥30%、数字、币种/金额、日期、术语及逐块校验标准；没有加入凑字说明。补充了对 `21st day of November 2013` 这种明确完整日期的识别与保护，提示版本为 `translate-v8`，新增反例确认改成错误月份仍被拒绝。原有短表格扩写反例与 12 项独立审查反例断言保留。

数字单元格、URL、签署人原名、文号、参考文献序号属于原文数据字段，只能原样复制；这些字段有源位置、类别和哈希，缺失、换序、错挂或改值均拒绝。语言单元仍独立通过原翻译闸门；每项记录实际模型、输入/输出、重试、tokens 和耗时。新页级结果显式写 `whole_clause_translation_passed=false`，不改变原条款的状态。

全量逐行哈希对比结果：

| 表 | 本次执行前已有记录 | 旧记录改变/丢失 | 新增 |
|---|---:|---:|---:|
| documents / document_versions | 各 80 | 0 | 0 |
| clauses | 2,607 | 0 | 0 |
| clause_translations | 3,464 | 0 | 0 |
| kg_edges / kg_nodes | 3,850 / 2,419 | 0 | 0 |
| glossary_terms | 14 | 0 | 0 |
| knowledge_audit_records | 11,610 | 0 | 4 |
| knowledge_change_proposals | 470 | 0 | 2 |

80 份归档重新读取并校验哈希全部通过。另与最初开发数据库对比，受保护的原文、证据和审批字段仍一致。新提案没有覆盖旧提案，未继承旧审批。

真实调用：`deepseek-flash`，20 个语言单元，输入 15,612 tokens、输出 12,116 tokens，总执行 87.221 秒。再次执行同源、同解析、同提示版本及术语快照时，2/2 命中已有修订，模型调用 0、数据库新增 0。密钥只从进程环境读取，没有写入代码、文档或运行产物。

## 回归与查看方式

| 检查 | 结果 |
|---|---|
| 后端全量 `pytest -q --tb=short` | **262 passed，112.21 秒**，含原有 12 项审查反例与新增 12 项解析/保护用例 |
| 针对解析、原反例、翻译模块的先行回归 | 30 passed，12.13 秒 |
| `ruff check app tests` 与本次两个 CLI 脚本 | 通过 |
| `pnpm typecheck`、`pnpm -r build` | 通过；构建仍有既有大资源包提示 |
| `node scripts/test-workflow-layout.mjs` | 5 passed |
| 许可证门禁 | 263 个依赖通过，无新增依赖 |
| 真实 PDF 重解析、重新逐单元校验、逐行保护核验 | 通过；见下方 JSON |
| 浏览器只读验证 | 管理员登录、复核列表、目标原页解析与双语历史可打开；没有点击审批或写入法律复核结论 |

`scripts/verify_ui_clickthrough.py` 第 78–81 行会点击“复核通过”，本次没有运行该会改变审批状态的旧脚本。使用浏览器只读验证替代，保持用户要求的审批记录与待核对状态。

本地隔离管理端：`http://127.0.0.1:5184/review-queue`。选择国家，在目标条款的“更多 → 原页解析修订（待核对、未启用）”查看 `source_parse_revision` 与 `parsed_source_translation`。历史失败行保留；新修订未启用为法律依据。原数据库及生产服务器未操作。

证据：

- [真实模型调用、原文位置及逐单元结果](layout-20261024-real-run.json)
- [真实 PDF 与本轮逐行保护核验](layout-20261024-verified.json)
- [与原始开发数据库的保护字段对比](layout-20261024-preservation.json)
- [真实数据幂等复用核验](layout-20261024-idempotence.json)
- [SG 原页渲染](layout-20261024-SG-source-page16.png)、[EG 原页渲染](layout-20261024-EG-source-page40.png)
- [浏览器修订历史截图](layout-20261024-review-history.png)
- [逐项双语待核对清单](layout-20261024-bilingual-review.md)

## 复现与备份

代码提交：`1d5872e`（解析、表格边界、日期校验和保护回归）。实测记录与复核清单另作后续提交。

从仓库根目录运行，数据库和归档须指向隔离副本，密钥预先通过环境变量提供：

```powershell
$env:CYSH_DATABASE_URL='postgresql+asyncpg://postgres@127.0.0.1:3739/remediation_20261023'
$env:CYSH_ARCHIVE_DIR='C:/Users/Jin/.cyshuhe-dev/remediation_20261023/archive'
# CYSH_JWT_SECRET、CYSH_LLM_API_KEY、CYSH_LLM_MODEL 使用当前进程环境。
& 'C:/Users/Jin/.venvs/cyshuhe/Scripts/python.exe' scripts/repair_remaining_source_layouts.py --json '<新的运行记录路径.json>'
& 'C:/Users/Jin/.venvs/cyshuhe/Scripts/python.exe' scripts/verify_layout_repair.py --run docs/reviews/layout-20261024-real-run.json --before-row-hashes 'C:/Users/Jin/.cyshuhe-dev/backups/20261024-layout-S0/before-row-hashes.json' --archive-root $env:CYSH_ARCHIVE_DIR --json '<新的核验记录路径.json>'
```

执行前备份：`C:/Users/Jin/.cyshuhe-dev/backups/20261024-layout-S0`，21 表、80 个归档，dump SHA-256：`11a8f43548c0513f1e2bff32f888ee32b1497dd81febba5e650595e826233b72`。本轮逐行基线 `before-row-hashes.json` 也保存在该目录，SHA-256：`9f7fe2d149e8c621e8a474fd73dd16d3d893f9ece55933871268dad15a60aa70`。

修复后完整备份：`C:/Users/Jin/.cyshuhe-dev/backups/20261024-layout-final`，21 表、80 个归档，备份目录验证通过；dump SHA-256：`64c71933b9f6bf77234eb1c45dc52b6dd876aa95a818f2c3f0b80b5d10a0b60a`。

待办只保留真实限制：这两份修订的人工语言/法律复核及启用决定尚未完成；页级解析修订目前通过历史入口查看，未自动迁移成替换旧条款的活动版本。禁止直接据此更改已有报告引用或继承审批。
