# S4：工作流版本与实际执行

## 操作方式

管理端“智能体工作台 → 配置版本”选择要维护的**可执行版本**，“配置与评测 → 导出工作流配置”取得完整 JSON。线下修改允许的参数，再通过“导入配置”保存。

导入不会激活。核对节点详情中的只读参数与实际模型 System Prompt 后，平台管理员点击“激活此版本”。新启动的任务使用该版本；已运行任务继续使用启动时固定的版本。选中以前的可执行版本点击“回滚到此版本”，只影响后续启动，不改写旧任务、轨迹或报告。

升级前的历史配置不能证明当时实际执行参数。迁移保留其原内容并标明历史展示版本，另创建待激活 S4 基线；须管理员显式激活才可启动新分析。不能将旧版号码推测回填到历史任务/轨迹。

## 配置白名单

完整节点/工具/拓扑仍随包导出，用于校验不可变边界。以下是**唯一可变的执行字段**，其余保持导出原值；字段不可遗漏或新增。所有数值使用 JSON 数值，不接受字符串或布尔值代替。

| 节点 | 配置包中的位置 | 范围 / 用途 |
| --- | --- | --- |
| MCP 检索 | `parameters.top_k`、`parameters.timeout` | 每维度1–30个候选；总检索时限1–300秒 |
| 八维分析 | `model_name`、`system_prompt`、`parameters.temperature`、`parameters.timeout` | 模型标识1–128字符；提示词1–16000字符；温度0–2；单维度含重试总时限1–300秒 |
| 引用核验 | `parameters.max_backtrack_rounds` | 0–3轮补证；0不跳过核验，失败结论仍被剔除 |
| 报告编制 | `parameters.timeout` | 1–300秒，不含后续更新检查时限 |
| 更新检查 | `parameters.enabled`、`parameters.timeout` | 唯一允许关闭的节点；关闭时写 skipped，不执行工具；超时1–300秒 |
| 输入、事实收集 | 无 | 必填事实与安全边界固定；不假装使用模型参数 |

示例：导出包中 `node_mcp_retriever.parameters.top_k` 改为7，`node_dimension_analyst.parameters.temperature` 改为0.3，并修改该分析节点的 `system_prompt`。其他字段原样保留。

`code_entrypoint`、`mcp_tools`、节点/连接拓扑、节点身份、必填事实、八维度契约、核验开关及固定参数修改均拒绝。`activate_immediately=true` 返回400。密钥和模型服务 URL 仍由服务器环境变量控制，不在配置包中提供。

非模型节点的文字是“执行说明”，不会发送给模型。只有分析节点的 System Prompt 参与模型请求。报告编制目前仍为确定性程序，不因设置提示词而新增一次模型调用。

## API 与追溯

- 导入：`POST /api/admin/agent-studio/workflow/import`，请求包含 `dsl_payload`、`change_note`，返回新 UUID，`is_active=false`。
- 激活/回滚：`POST /api/admin/agent-studio/workflow/versions/{UUID}/activate`。兼容旧版本号路径；均仅平台管理员可写，复核员仍只读。
- 查询：`GET /api/admin/agent-studio/workflow?version_no=N`、`/workflow/versions`、`/workflow/export?version_no=N`。
- 新任务和每条步骤均含 `workflow_version_id`；任务还有 `workflow_version_no`。轨迹输入的 `workflow_execution` 保存版本号、UUID和本节点实际参数。
- 查看任务时画布加载其执行配置；手动查看其他配置时提示与任务不同，节点统计不混入其他配置的轨迹。历史空ID明确显示“未记录”。
- 激活切换串行化，数据库限制一个工作流只能有一个激活版本；配置正文不可原位修改/删除，激活前后保留审计快照。未来改变不可变执行契约时须升级 `RUNTIME_SCHEMA` 并创建新基线，不能静默改变已存版本的含义。

## 验证与环境

后端完整回归293项通过，含12项原审查反例；新增 HTTP 假服务测试检查实际模型请求体、top_k、运行中固定版本、相同输入两次不同版本、回滚与旧轨迹保留。没有以真实法律准确率解释这些工程测试。

新增浏览器回归 `scripts/verify_s4_ui.mjs` 使用已登录的 CUA Tab 句柄执行导入、显式激活、回滚；只能在本地隔离管理端 `http://127.0.0.1:5184/agent-studio` 使用。前置条件：已有一个激活的可执行版本、无未关闭导入弹窗。脚本追加一个配置版本并恢复原激活版本，不创建任务、不调用模型、不审批。结果见 `docs/reviews/s4-20261024-ui-regression.json`。

0016 目前只应用到隔离数据库 `remediation_20261023`。保留四份新增配置版本及审计用于复现，当前激活基线为v17；原开发库和生产服务器未升级。人工语言/语义/法律验收继续待核对。
