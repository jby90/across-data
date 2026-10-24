from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.models import (
    AgentWorkflowConfig,
)

WORKFLOW_KEY = "compliance_8dim_multi_agent"

EIGHT_DIMENSIONS_SPEC: list[tuple[str, str]] = [
    ("D1", "属地数据法规体系概述"),
    ("D2", "汽车核心数据定义与分类"),
    ("D3", "数据跨境传输合规路径与前置条件"),
    ("D4", "用户授权、告知、同意的合规要求细则"),
    ("D5", "数据存储、留存、销毁时效与规范"),
    ("D6", "违规行为分级、处罚标准、监管追责机制"),
    ("D7", "属地市场准入认证、申报材料、流程节点要求"),
    ("D8", "企业合规整改建议与落地执行方案"),
]

COUNTRY_NAMES_ZH = {
    "SG": "新加坡",
    "EG": "埃及",
    "TN": "突尼斯",
    "MA": "摩洛哥",
    "LB": "黎巴嫩",
}

SCENARIO_LABELS = {
    "autonomous_trial": "L3/L4 自动驾驶公共道路测试与远程监控回传",
    "connected_vehicle_cloud": "智能网联乘用车车联网遥测与跨境云平台同步",
    "ota_diagnostics": "跨境远程诊断、OTA 升级包分发与故障日志回传",
    "cockpit_biometrics": "智能座舱语音交互、DMS 驾驶员状态识别与个人数据出境",
}

REQUIRED_FACTS_SPEC: list[dict[str, Any]] = [
    {
        "fact_key": "exterior_video_and_gnss",
        "dimension_code": "D2",
        "question_zh": "车辆是否采集车外高清摄像头视频（含街景行人面孔、周边车牌）及高精度 GNSS 轨迹数据？是否在车端或边缘端执行实时匿名化/脱敏？",
        "why_needed": "决定是否触发新加坡 PDPA 个人数据认定（人脸/车牌可识别自然人）及 LTA 路测传感器记录规范。",
        "suggested_options": [
            "采集 8 路环视视频与厘米级 GNSS，车端对人脸/车牌实时模糊处理后回传研发云",
            "采集原始视频与高精度轨迹，未脱敏直接回传境外算法训练中心",
            "仅在车端本地黑匣子闭环存储，不向车外传输原始视频流",
        ],
    },
    {
        "fact_key": "cross_border_mechanism",
        "dimension_code": "D3",
        "question_zh": "跨境数据接收方所在国家/地区，以及拟采用的合法出境保障机制（如集团内部具有约束力的协议、标准合同条款 DPA、转移影响评估 TIA 或属地监管事先许可）？",
        "why_needed": "用于对照新加坡 PDPA 第 26 条（Transfer Limitation Obligation）、突尼斯 INPDP 事先授权、埃及 PDPC 跨境许可证等核心条款。",
        "suggested_options": [
            "传输至中国重庆/上海研发中心，已签署对标 PDPA 第三附表保护标准的跨境数据处理协议 (DPA) 并完成 TIA",
            "传输至境外云平台，尚未签署跨境数据保护合同或申请属地监管许可",
            "采用本地云专区存储，仅向境外传输聚合脱敏后的统计分析结果",
        ],
    },
    {
        "fact_key": "consent_and_notice",
        "dimension_code": "D4",
        "question_zh": "面向车主、乘客、路测安全员及车外公众的隐私告知与同意获取方式是什么？是否任命属地数据保护官 (DPO)？",
        "why_needed": "对应 PDPA 第 13-15 条（同意与告知义务）与第 11(3) 条（强制指定 DPO 并公开联系方式）。",
        "suggested_options": [
            "车机首次激活弹窗明示同意 + 车身张贴路测视觉采集告示 + 已任命新加坡本地 DPO 并公示邮箱",
            "仅在购车合同通用条款中默认勾选，尚未单独告知跨境接收方且未任命本地 DPO",
        ],
    },
    {
        "fact_key": "retention_and_blackbox",
        "dimension_code": "D5",
        "question_zh": "车载数据记录器（黑匣子/EDR）故障与接管数据的本地法定留存期限，以及云端个人数据的到期销毁策略？",
        "why_needed": "新加坡《道路交通(自动驾驶汽车)规则》强制要求路测记录留存不少于 3 年，而 PDPA 第 25 条要求目的达成后立即停止留存或匿名化。",
        "suggested_options": [
            "LTA 自动驾驶黑匣子安全与接管日志在新加坡境内加密留存 3 年；研发诊断个人数据满 180 天自动销毁",
            "所有采集数据无固定留存期限，长期保存在云端对象存储中",
        ],
    },
    {
        "fact_key": "local_admission_and_safety_operator",
        "dimension_code": "D7",
        "question_zh": "是否已向属地交通与通信主管部门（如新加坡 LTA、埃及 NTRA）提交道路测试许可或车联网通信终端型号核准，并配置持证安全员与第三方责任险？",
        "why_needed": "决定 D7 属地市场准入认证、申报材料与流程节点的合规缺口。",
        "suggested_options": [
            "已准备 LTA 自动驾驶路测申请包、配备经培训的隨车安全员 (Safety Operator) 并投保第三方责任险",
            "处于前期法规调研阶段，尚未提交属地路测准入申请与车载通信核准",
        ],
    },
]


DEFAULT_WORKFLOW_NODES: list[dict[str, Any]] = [
    {
        "node_id": "node_start",
        "title": "业务场景输入触发器",
        "subtitle": "Start · Scenario & Facts Input",
        "node_type": "trigger",
        "category_label": "输入触发器",
        "icon": "VideoPlay",
        "color": "#2563eb",
        "position_x": 60,
        "position_y": 190,
        "readonly_locked": True,
        "lock_reason": "生产运行锁定：仅允许查看流程与连接详情，禁止在线调试模型参数或修改节点代码",
        "model_provider": "System Built-in",
        "model_name": "JSON-Schema-Gateway-v1",
        "parameters": {
            "strict_tenant_isolation": True,
            "supported_countries": ["SG", "EG", "TN", "MA", "LB"],
            "require_auth_role": ["org_user", "org_admin", "platform_admin"],
        },
        "system_prompt": "# 触发器节点（无 LLM 调用）\n接收企业用户提交的目标国家(country_code)、车型(vehicle_model)、业务场景(scenario_type)及初始事实字典(facts)，注入租户隔离上下文(org_id)。",
        "code_entrypoint": "app.analysis.workflow_engine:trigger_entry",
        "mcp_tools": [],
        "input_variables": [
            {
                "name": "country_code",
                "type": "string",
                "desc": "目标出海国家二字码 (SG/EG/TN/MA/LB)",
            },
            {"name": "vehicle_model", "type": "string", "desc": "受评智能网联/自动驾驶车型"},
            {"name": "scenario_type", "type": "string", "desc": "业务场景枚举"},
            {"name": "facts", "type": "object", "desc": "用户初始输入的业务事实键值对"},
        ],
        "output_variables": [
            {"name": "task_context", "type": "object", "desc": "标准化分析上下文对象"},
        ],
        "responsibility_desc": "接收车企用户的合规评估请求，完成租户隔离绑定与输入结构预校验，并流转至信息采集与追问智能体。",
    },
    {
        "node_id": "node_fact_collector",
        "title": "信息采集与追问 Agent",
        "subtitle": "Fact Collector & Sufficiency Guard",
        "node_type": "collector",
        "category_label": "信息采集智能体",
        "icon": "ChatDotRound",
        "color": "#0891b2",
        "position_x": 350,
        "position_y": 190,
        "readonly_locked": True,
        "lock_reason": "生产运行锁定：仅允许查看流程与连接详情，禁止在线调试模型参数或修改节点代码",
        "model_provider": "规则校验(不调用大模型)",
        "model_name": "Rule-Schema-Validator",
        "parameters": {
            "temperature": 0.1,
            "top_p": 0.9,
            "max_tokens": 2048,
            "sufficiency_threshold": 100,
            "block_on_missing_facts": True,
        },
        "system_prompt": (
            "你是【车研数合·信息采集与追问智能体】。你的职责是对照八维度《汽车数据跨境信息采集规范 Schema》，"
            "逐项检查用户提交的业务事实是否涵盖：\n"
            "1) 车外视觉/GNSS 传感器采集与脱敏方式(D2)\n"
            "2) 跨境传输目的国与合法出境保障机制(D3)\n"
            "3) 车主/安全员/公众告知同意及属地 DPO 任命(D4)\n"
            "4) 车载黑匣子法定留存年限与个人数据销毁策略(D5)\n"
            "5) 属地交通/通信主管部门准入申报与安全员配置(D7)\n"
            "若存在任一缺失项，严禁直接编造结论，必须输出结构化追问清单(missing_facts)引导用户补充；"
            "仅当完备度评分 sufficiency_score == 100 时放行进入法规检索阶段。"
        ),
        "code_entrypoint": "app.analysis.workflow_engine:evaluate_fact_sufficiency",
        "mcp_tools": [
            {
                "tool_name": "validate_fact_schema",
                "display_name": "八维度事实完备度校验器",
                "description": "校验当前任务事实字典是否满足八维度必填字段规范，返回完备度百分比与追问问题列表。",
                "input_schema": {"facts": "dict", "country_code": "str"},
                "output_schema": {
                    "sufficiency_score": "int",
                    "missing_facts": "list[FollowUpQuestion]",
                },
            }
        ],
        "input_variables": [
            {"name": "task_context", "type": "object", "desc": "来自触发器的任务上下文"},
        ],
        "output_variables": [
            {"name": "sufficiency_score", "type": "number", "desc": "事实完备度评分 (0-100)"},
            {"name": "missing_facts", "type": "array", "desc": "待向用户追问的缺失项列表"},
            {"name": "verified_facts", "type": "object", "desc": "通过完备性校验的结构化事实"},
        ],
        "responsibility_desc": "按八维度信息采集规范评估输入事实是否充足；若不足则暂停流水线并向前端弹出交互式追问卡片，满 100% 后移交检索智能体。",
    },
    {
        "node_id": "node_mcp_retriever",
        "title": "MCP 法规混合检索 Agent",
        "subtitle": "Effective Law & KG RAG Retriever",
        "node_type": "retriever",
        "category_label": "法规检索智能体",
        "icon": "Search",
        "color": "#7c3aed",
        "position_x": 650,
        "position_y": 190,
        "readonly_locked": True,
        "lock_reason": "生产运行锁定：仅允许查看流程与连接详情，禁止在线调试模型参数或修改节点代码",
        "model_provider": "关键词检索(不调用大模型)",
        "model_name": "Keyword-Retriever-v1",
        "parameters": {
            "temperature": 0.0,
            "top_k_clauses": 15,
            "only_effective_versions": True,
            "require_source_hash_integrity": True,
        },
        "system_prompt": (
            "你是【车研数合·MCP 法规混合检索与时效核验智能体】。\n"
            "你必须且只能在目标国家(country_code)的现行有效法规版本内检索证据条款。\n"
            "1. 先调用 `check_source_availability` 核验该国法规原文归档、SHA-256 完整性及人工复核状态；\n"
            "2. 调用 `search_effective_clauses` 与 `query_kg_obligations` 获取候选条款、中文对照与六要素知识图谱义务边；\n"
            "3. 调用 `verify_clause_effective_date` 确认每条候选条款在评估基准日处于有效状态。"
        ),
        "code_entrypoint": "app.analysis.workflow_engine:execute_mcp_retrieval",
        "mcp_tools": [
            {
                "tool_name": "check_source_availability",
                "display_name": "国别法源可用性与哈希完整性前置检查",
                "description": "核对目标国家已入库官方原文数量、SHA-256 归档状态及外文 OCR/翻译复核资格。",
                "input_schema": {"country_code": "str"},
                "output_schema": {
                    "docs_count": "int",
                    "clauses_count": "int",
                    "legal_basis_ready": "bool",
                },
            },
            {
                "tool_name": "search_effective_clauses",
                "display_name": "限定国家与有效期的法规条款混合检索",
                "description": "在目标国家有效法规版本中检索与汽车数据、个人信息、跨境传输、路测准入相关的条款原文与术语受控译文。",
                "input_schema": {
                    "country_code": "str",
                    "keywords": "list[str]",
                    "only_effective": "bool",
                },
                "output_schema": {"candidate_clauses": "list[ClauseBundle]"},
            },
            {
                "tool_name": "query_kg_obligations",
                "display_name": "知识图谱六要素与十二关系联查",
                "description": "查询条款挂接的数据类型、合规义务、数据出境限制、准入要求与违规罚则关系边。",
                "input_schema": {"country_code": "str", "clause_ids": "list[uuid]"},
                "output_schema": {"kg_edges": "list[KgEdgeBundle]"},
            },
            {
                "tool_name": "verify_clause_effective_date",
                "display_name": "条款生效日期与现行版本核验工具",
                "description": "核验指定条款所属法规版本的公布日期、生效日期与现行状态(in_force)。",
                "input_schema": {"version_id": "uuid", "as_of_date": "str"},
                "output_schema": {"is_effective": "bool", "version_no": "int", "source_url": "str"},
            },
        ],
        "input_variables": [
            {"name": "verified_facts", "type": "object", "desc": "已完备的业务事实"},
            {"name": "country_code", "type": "string", "desc": "目标出海国家"},
        ],
        "output_variables": [
            {"name": "source_check", "type": "object", "desc": "法源可用性检查结果"},
            {
                "name": "retrieved_clauses",
                "type": "array",
                "desc": "核验有效的法规条款与图谱证据包",
            },
        ],
        "responsibility_desc": "通过内置 MCP 工具箱检索目标国有效法规条款与知识图谱语义边，并执行法源哈希完整性与时效前置校验。",
    },
    {
        "node_id": "node_dimension_analyst",
        "title": "八维度合规矩阵分析 Agent",
        "subtitle": "8-Dimension Compliance Matrix Analyst",
        "node_type": "analyst",
        "category_label": "合规分析智能体",
        "icon": "DataAnalysis",
        "color": "#ea580c",
        "position_x": 960,
        "position_y": 90,
        "readonly_locked": True,
        "lock_reason": "生产运行锁定：仅允许查看流程与连接详情，禁止在线调试模型参数或修改节点代码",
        "model_provider": "DeepSeek(OpenAI 兼容接口)",
        "model_name": "deepseek-flash",
        "parameters": {
            "temperature": 0.1,
            "max_tokens": 6000,
            "reasoning_effort": "low",
            "forbid_ungrounded_claims": True,
            "fallback_label_on_missing_evidence": "待核查 (SOURCE_EVIDENCE_PENDING)",
        },
        "system_prompt": (
            "你是【车研数合·八维度合规矩阵分析智能体】。\n"
            "请严格依据检索智能体交付的 `retrieved_clauses` 对照用户的 `verified_facts`，"
            "输出：\n"
            "1) 结构化合规分析矩阵（每行包含：业务事实｜适用条款｜合规义务｜风险等级｜判断依据原文子串）；\n"
            "2) D1~D8 八个维度的法规对标分析草稿。\n"
            "【铁律】：严禁引用未在 `retrieved_clauses` 中出现的法规或条款；每条结论必须附带原文精确子串 `basis_quote`；"
            "若某国或某维度缺乏已核验原文依据，必须如实标注为 `待核查 (SOURCE_EVIDENCE_PENDING)`，绝不编造法律结论。当收到质检智能体的退回意见时，针对退回问题重新分析并逐字补充原文片段。"
        ),
        "code_entrypoint": "app.analysis.workflow_engine:execute_dimension_analysis",
        "mcp_tools": [],
        "input_variables": [
            {"name": "verified_facts", "type": "object", "desc": "结构化业务事实"},
            {"name": "retrieved_clauses", "type": "array", "desc": "检索到的有效条款证据包"},
            {
                "name": "backtrack_feedback",
                "type": "object",
                "desc": "来自质检/编制节点的退回补证问题（如有）",
            },
        ],
        "output_variables": [
            {"name": "findings_matrix", "type": "array", "desc": "五列合规判定矩阵"},
            {"name": "dimension_drafts", "type": "array", "desc": "D1-D8 八维度分析初稿与引用清单"},
        ],
        "responsibility_desc": "对照业务事实与有效条款生成五列合规判定矩阵及 D1-D8 八维度分析，并在收到退回指令时定向补充法条依据。",
    },
    {
        "node_id": "node_citation_verifier",
        "title": "依据子串核验与退回质检 Agent",
        "subtitle": "Programmatic Citation & Substring Gate",
        "node_type": "verifier",
        "category_label": "质检核验智能体",
        "icon": "CircleCheck",
        "color": "#dc2626",
        "position_x": 960,
        "position_y": 310,
        "readonly_locked": True,
        "lock_reason": "生产运行锁定：仅允许查看流程与连接详情，禁止在线调试模型参数或修改节点代码",
        "model_provider": "Deterministic Verifier + Guard",
        "model_name": "Substring-Exact-Matcher-v2",
        "parameters": {
            "require_exact_substring": True,
            "verify_numbers_and_currency": True,
            "max_backtrack_loops": 2,
        },
        "system_prompt": (
            "你是【车研数合·程序化依据核验与退回质检智能体】。\n"
            "你对分析智能体提交的每一条 `findings_matrix` 和 `dimension_drafts` 执行三项硬性程序校验：\n"
            "1) 引用片段 `quote_original` 必须是数据库中对应 `Clause.original_text` 的真实连续子串；\n"
            "2) 引用的条号、版本号、官方 URL 必须与入库记录完全一致；\n"
            "3) 涉及罚款金额（如 1,000,000 新元 / 10%）、留存年限（如 3 years）等数字必须与原文一致。\n"
            "若发现任一维度存在未挂接原文子串的断言，立即触发 `BACKTRACK_REVISE` 退回给【八维度合规矩阵分析 Agent】要求补充依据！"
        ),
        "code_entrypoint": "app.analysis.workflow_engine:execute_citation_verification",
        "mcp_tools": [
            {
                "tool_name": "verify_quote_substring",
                "display_name": "条款原文子串与数字精确校验工具",
                "description": "程序化比对引用片段是否真实存在于 Clause.original_text 中，并校验金额/期限数字。",
                "input_schema": {"clause_id": "uuid", "quote_original": "str"},
                "output_schema": {"is_substring": "bool", "numbers_matched": "bool"},
            }
        ],
        "input_variables": [
            {"name": "findings_matrix", "type": "array", "desc": "待核验的合规判定矩阵"},
            {"name": "dimension_drafts", "type": "array", "desc": "待核验的八维度章节草稿"},
        ],
        "output_variables": [
            {
                "name": "verification_passed",
                "type": "boolean",
                "desc": "是否全部通过子串与数字核验",
            },
            {"name": "backtrack_issue", "type": "string", "desc": "退回补充依据的具体问题描述"},
            {"name": "verified_dimensions", "type": "array", "desc": "通过核验的八维度章节"},
        ],
        "responsibility_desc": "对每条法条引用执行程序化原文子串与数字校验；发现缺失依据或失配时自动退回分析智能体补证。",
    },
    {
        "node_id": "node_report_compiler",
        "title": "八维度国别报告编制 Agent",
        "subtitle": "8-Dimension Report Compiler & Exporter",
        "node_type": "compiler",
        "category_label": "报告编制智能体",
        "icon": "DocumentChecked",
        "color": "#16a34a",
        "position_x": 1270,
        "position_y": 190,
        "readonly_locked": True,
        "lock_reason": "生产运行锁定：仅允许查看流程与连接详情，禁止在线调试模型参数或修改节点代码",
        "model_provider": "确定性编制(不调用大模型)",
        "model_name": "Report-Compiler-v1",
        "parameters": {
            "immutable_versioning": True,
            "support_split_dimension_view": True,
            "export_formats": ["docx", "pdf"],
        },
        "system_prompt": (
            "你是【车研数合·八维度国别报告编制智能体】。\n"
            "你将通过程序化核验的 D1~D8 章节汇编为不可变的国别合规评估报告版本(ReportVersion)，"
            "为每处法规引用生成可点击角标(如 `[SG-PDPA-s26]`)，绑定条款 ID、版本号与知识图谱关联面板；"
            "同时支持整份报告浏览、按单维度(D1~D8)独立拆分查看、版本差异对比(Diff)及 Word/PDF 导出。"
        ),
        "code_entrypoint": "app.analysis.workflow_engine:compile_eight_dimension_report",
        "mcp_tools": [
            {
                "tool_name": "compile_report_version",
                "display_name": "生成不可变八维度报告版本",
                "description": "将 D1-D8 章节、角标引用与执行摘要固化为不可变报告版本，支持整份与按维度拆分导出。",
                "input_schema": {"task_id": "uuid", "dimensions": "list[Dimension]"},
                "output_schema": {"report_version_id": "uuid", "version_no": "int"},
            }
        ],
        "input_variables": [
            {"name": "verified_dimensions", "type": "array", "desc": "已核验的 D1-D8 章节数据"},
            {"name": "findings_matrix", "type": "array", "desc": "已核验的五列判定矩阵"},
        ],
        "output_variables": [
            {"name": "report_version", "type": "object", "desc": "不可变八维度报告版本对象"},
        ],
        "responsibility_desc": "汇编生成含可点击法规角标的八维度完整报告与单维度拆分视图，固化不可变版本并提供 Word/PDF 导出。",
    },
    {
        "node_id": "node_update_linkage",
        "title": "法规修订 72h 影响联动 Agent",
        "subtitle": "72h Legal Amendment Impact Watcher",
        "node_type": "linkage",
        "category_label": "修订联动智能体",
        "icon": "Bell",
        "color": "#ca8a04",
        "position_x": 1270,
        "position_y": 380,
        "readonly_locked": True,
        "lock_reason": "生产运行锁定：仅允许查看流程与连接详情，禁止在线调试模型参数或修改节点代码",
        "model_provider": "Event-Driven Graph Watcher",
        "model_name": "Amendment-Impact-Tracker-v1",
        "parameters": {
            "sla_hours": 72,
            "auto_flag_affected_dimensions": True,
        },
        "system_prompt": (
            "你是【车研数合·法规修订 72 小时影响联动智能体】。\n"
            "当 M1 法规知识库检测到某部法规发生版本修订(DocumentVersion 新增或条款哈希变更)时，"
            "通过引用依赖链逆向定位受影响的知识图谱边、分析任务判定矩阵行及报告维度章节(如 D3 跨境传输)，"
            "在 72 小时闭环时效内向报告注入 `stale_impacts` 复核提示，绝不静默篡改历史已固化报告正文。"
        ),
        "code_entrypoint": "app.analysis.workflow_engine:check_report_amendment_impacts",
        "mcp_tools": [
            {
                "tool_name": "check_country_report_updates",
                "display_name": "法规版本修订影响追踪工具",
                "description": "比对报告所引用的条款版本与知识库最新法规版本，定位受修订影响的报告维度并生成复核告警。",
                "input_schema": {"task_id": "uuid", "country_code": "str"},
                "output_schema": {"stale_impacts": "list[StaleImpactAlert]"},
            }
        ],
        "input_variables": [
            {
                "name": "report_version",
                "type": "object",
                "desc": "已生成的报告版本及其引用条款清单",
            },
        ],
        "output_variables": [
            {
                "name": "stale_impacts",
                "type": "array",
                "desc": "受法规修订影响的报告章节复核提示清单",
            },
        ],
        "responsibility_desc": "监听法规版本修订并在 72 小时内精准定位受影响的报告维度章节，生成可视化复核提示。",
    },
]

DEFAULT_WORKFLOW_EDGES: list[dict[str, Any]] = [
    {
        "edge_id": "edge_start_to_collector",
        "source_node_id": "node_start",
        "target_node_id": "node_fact_collector",
        "edge_type": "forward",
        "label": "提交场景与初始事实",
        "condition_desc": "用户发起分析或提交补充追问事实后触发",
        "animated": False,
    },
    {
        "edge_id": "edge_collector_to_retriever",
        "source_node_id": "node_fact_collector",
        "target_node_id": "node_mcp_retriever",
        "edge_type": "forward",
        "label": "完备度 100% 放行",
        "condition_desc": "sufficiency_score == 100（必填事实齐全）",
        "animated": False,
    },
    {
        "edge_id": "edge_retriever_to_analyst",
        "source_node_id": "node_mcp_retriever",
        "target_node_id": "node_dimension_analyst",
        "edge_type": "forward",
        "label": "交付有效条款与图谱边",
        "condition_desc": "完成法源完整性与现行有效期核验后交接",
        "animated": False,
    },
    {
        "edge_id": "edge_analyst_to_verifier",
        "source_node_id": "node_dimension_analyst",
        "target_node_id": "node_citation_verifier",
        "edge_type": "forward",
        "label": "提交矩阵与八维初稿",
        "condition_desc": "提交五列判定矩阵及 D1-D8 章节引用",
        "animated": False,
    },
    {
        "edge_id": "edge_verifier_backtrack_analyst",
        "source_node_id": "node_citation_verifier",
        "target_node_id": "node_dimension_analyst",
        "edge_type": "backtrack",
        "label": "退回分析补充依据 (BACKTRACK)",
        "condition_desc": "发现缺失法条依据或原文子串/数字核验未通过时强制退回补证",
        "animated": True,
    },
    {
        "edge_id": "edge_verifier_to_compiler",
        "source_node_id": "node_citation_verifier",
        "target_node_id": "node_report_compiler",
        "edge_type": "forward",
        "label": "子串核验 100% 通过",
        "condition_desc": "所有法条引用均为真实原文子串且条号/金额一致",
        "animated": False,
    },
    {
        "edge_id": "edge_compiler_to_linkage",
        "source_node_id": "node_report_compiler",
        "target_node_id": "node_update_linkage",
        "edge_type": "event_trigger",
        "label": "绑定 72h 修订监测",
        "condition_desc": "报告版本生成后注册条款版本依赖监听",
        "animated": False,
    },
]


def evaluate_fact_sufficiency(
    country_code: str, facts: dict[str, Any]
) -> tuple[int, list[dict[str, Any]]]:
    """根据八维度信息采集规范评估事实完备度，返回 (0-100 评分, 待追问列表)。"""
    missing: list[dict[str, Any]] = []
    fulfilled_count = 0
    total_required = len(REQUIRED_FACTS_SPEC)
    for spec in REQUIRED_FACTS_SPEC:
        key = spec["fact_key"]
        val = str((facts or {}).get(key) or "").strip()
        is_ok = len(val) >= 4
        if is_ok:
            fulfilled_count += 1
        missing.append(
            {
                "fact_key": key,
                "dimension_code": spec["dimension_code"],
                "question_zh": spec["question_zh"],
                "why_needed": spec["why_needed"],
                "suggested_options": spec["suggested_options"],
                "current_value": val if is_ok else None,
                "is_fulfilled": is_ok,
            }
        )
    score = int(round((fulfilled_count / total_required) * 100)) if total_required else 100
    return score, missing


def export_workflow_dsl(cfg: AgentWorkflowConfig) -> dict[str, Any]:
    """导出兼容 Dify DSL 风格的多智能体编排配置文件(用于长久维护、跨环境迁移与版本备份)。"""
    return {
        "kind": "cyshuhe.agent_workflow",
        "schema_version": cfg.dsl_version,
        "runtime_schema_version": cfg.runtime_schema_version,
        "exported_at": datetime.now(UTC).isoformat(),
        "security_policy": {
            "readonly_locked": True,
            "allow_runtime_parameter_mutation": False,
            "allow_inline_code_execution_edit": False,
            "lock_notice": "生产安全锁定：管理端仅开放查看工作流拓扑、连接关系与节点配置详情，禁止在线直接调试模型参数或修改节点代码；配置变更须通过受控配置包导入并留存版本审计。",
        },
        "app": {
            "workflow_key": cfg.workflow_key,
            "name": cfg.name,
            "description": cfg.description,
            "version_no": cfg.version_no,
            "change_note": cfg.change_note or "初始生产编排配置",
        },
        "workflow": {
            "graph": {
                "nodes": cfg.nodes_json,
                "edges": cfg.edges_json,
            },
            "report_dimensions": [
                {"code": code, "title": title} for code, title in EIGHT_DIMENSIONS_SPEC
            ],
            "required_facts_schema": REQUIRED_FACTS_SPEC,
        },
    }


async def validate_and_import_workflow_dsl(
    session: AsyncSession,
    dsl_payload: dict[str, Any],
    change_note: str,
    user_id: uuid.UUID | None,
    activate_immediately: bool = False,
) -> AgentWorkflowConfig:
    from app.analysis.workflow_runtime import import_version

    if activate_immediately:
        raise ValueError("导入不能自动激活；请保存后由管理员显式激活版本")
    return await import_version(session, dsl_payload, change_note, user_id)


async def ensure_workflow_config(session: AsyncSession) -> AgentWorkflowConfig:
    from app.analysis.workflow_runtime import ensure_baseline

    return await ensure_baseline(session)
