"""六要素+十二关系知识图谱抽取(S2 大模型结构化抽取 + 类型约束矩阵 + 原文逐字校验 + 语义裁判)、
两层术语库与解读要点初始化、以及法规版本更新联动。

核心原则:
1. 文件级相关性(classify_document_relevance):先判定文件是否与“智能网联汽车数据、个人数据、
   跨境传输、自动驾驶测试准入”相关;不相关文件(如 Courts Reform Act、生效日期通知、申请表)
   标记为 out_of_scope,保留在文档库但不进入图谱抽取。
2. 条款级结构化抽取(extract_clause_entities_and_relations):使用大模型按固定 JSON Schema
   抽取真实六要素实体(COUNTRY/REGULATOR/INSTRUMENT/DATA_CATEGORY/OBLIGATION/PENALTY)与
   十二类语义关系,取代旧关键词规则。
3. 四重质量闸门(evaluate_kg_candidate):
   - 闸门 1: 文件相关性范围校验(out_of_scope 拒绝);
   - 闸门 2: 十二类关系源/目标节点类型约束矩阵(ALLOWED_RELATION_ENDPOINT_TYPES);
   - 闸门 3: 证据片段(evidence_quote)必须是条款原文(original_text)的逐字子串;
   - 闸门 4: 独立语义裁判(judge_extracted_relation,第二次大模型调用,prompt=judge-v2),
     输出 supports=yes/no/partial + reason;no 丢弃,partial 降级并标记,仅 yes 入图谱。
4. 结构边(PUBLISHES/SUPERVISES 机构-文件-国家元数据关系,is_structural=True)与条款语义边
   (is_structural=False)分层统计;旧 kg-rule-extractor-v1 边标记为 superseded 并从活跃视图隐藏。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.ingestion.models import Clause, Document, DocumentVersion
from app.knowledge.kg_run_state import begin_usage, end_usage, request_started, response_received
from app.knowledge.models import (
    ClauseTranslation,
    GlossaryTerm,
    GraphReviewStatus,
    KgEdge,
    KgNode,
    NodeType,
    RelationType,
    ReviewPoint,
    ReviewPointStatus,
    TranslationReviewStatus,
)
from app.knowledge.translator import translate_clause

logger = logging.getLogger(__name__)

EXTRACTOR_PROMPT_VERSION = "kg-extract-v2"
JUDGE_PROMPT_VERSION = "kg-judge-v2"
STRUCTURAL_EXTRACTOR_MODEL = "structural-metadata-v2"
LEGACY_RULE_EXTRACTOR_MODEL = "kg-rule-extractor-v1"

COUNTRY_NAMES: dict[str, tuple[str, str]] = {
    "SG": ("新加坡", "Republic of Singapore"),
    "EG": ("埃及", "Arab Republic of Egypt"),
    "TN": ("突尼斯", "Republic of Tunisia"),
    "MA": ("摩洛哥", "Kingdom of Morocco"),
    "LB": ("黎巴嫩", "Republic of Lebanon"),
}

# 两层术语库种子:country_code=None 为跨国通用核心概念;country_code='SG' 等为各国法定覆盖
SEED_GLOSSARY_TERMS: list[dict] = [
    # 通用核心概念层 (Priority 4 - 国际通行基准)
    {
        "concept_code": "personal_data",
        "country_code": None,
        "language": "en",
        "source_term": "personal data",
        "target_zh": "个人数据",
        "priority_level": 4,
        "statutory_definition": "与已识别或可识别自然人相关的任何信息。",
        "source_reference": "国际通行概念 (参考 GDPR Art. 4(1))",
        "review_status": "approved",
    },
    {
        "concept_code": "data_controller",
        "country_code": None,
        "language": "en",
        "source_term": "data controller",
        "target_zh": "数据控制者",
        "priority_level": 4,
        "statutory_definition": "单独或与他人共同决定个人数据处理目的和方式的组织或自然人。",
        "source_reference": "国际通行概念 (参考 GDPR Art. 4(7))",
        "review_status": "approved",
    },
    {
        "concept_code": "data_processor",
        "country_code": None,
        "language": "en",
        "source_term": "data processor",
        "target_zh": "数据处理者",
        "priority_level": 4,
        "statutory_definition": "代表数据控制者处理个人数据的自然人、法人或其他组织。",
        "source_reference": "国际通行概念 (参考 GDPR Art. 4(8))",
        "review_status": "approved",
    },
    {
        "concept_code": "cross_border_transfer",
        "country_code": None,
        "language": "en",
        "source_term": "transfer of personal data",
        "target_zh": "个人数据跨境传输",
        "priority_level": 4,
        "statutory_definition": "将个人数据传输或提供至本法域境外的接收方。",
        "source_reference": "国际通行概念",
        "review_status": "approved",
    },
    # 新加坡法定术语覆盖层 (Priority 1 & 2)
    {
        "concept_code": "personal_data",
        "country_code": "SG",
        "language": "en",
        "source_term": "personal data",
        "target_zh": "个人数据",
        "priority_level": 1,
        "statutory_definition": (
            "无论真实与否,可据此单独识别、或结合组织持有或可能接触的其他信息识别特定个人的数据。"
        ),
        "source_reference": "SG Personal Data Protection Act 2012 (PDPA) s 2(1)",
        "match_keywords": ["personal data", "identified", "individual"],
        "review_status": "approved",
    },
    {
        "concept_code": "data_intermediary",
        "country_code": "SG",
        "language": "en",
        "source_term": "data intermediary",
        "target_zh": "数据中介机构",
        "priority_level": 1,
        "statutory_definition": (
            "代表另一组织处理个人数据的组织(不含该组织的雇员);在满足法定条件下仅适用保护义务与留存限制义务。"
        ),
        "source_reference": "SG Personal Data Protection Act 2012 (PDPA) s 2(1) & s 4(2)",
        "match_keywords": ["data intermediary", "on behalf of another organisation"],
        "review_status": "approved",
    },
    {
        "concept_code": "data_protection_officer",
        "country_code": "SG",
        "language": "en",
        "source_term": "data protection officer",
        "target_zh": "数据保护官(DPO)",
        "priority_level": 1,
        "statutory_definition": "组织依法必须指定的一名或多名负责确保组织遵守 PDPA 的专责人员。",
        "source_reference": "SG Personal Data Protection Act 2012 (PDPA) s 11(3)",
        "match_keywords": ["designate one or more individuals", "business contact information"],
        "review_status": "approved",
    },
    {
        "concept_code": "notifiable_data_breach",
        "country_code": "SG",
        "language": "en",
        "source_term": "notifiable data breach",
        "target_zh": "须通报的数据泄露事件",
        "priority_level": 1,
        "statutory_definition": (
            "可能导致受影响个人遭受重大损害,或达到法定规模阈值(通常为 500 人以上)"
            "的数据泄露事件,须在 3 个日历日内向 PDPC 通报。"
        ),
        "source_reference": "SG PDPA Part 6A (s 26A - 26D)",
        "match_keywords": ["data breach", "notifiable", "Commission"],
        "review_status": "approved",
    },
    {
        "concept_code": "autonomous_motor_vehicle",
        "country_code": "SG",
        "language": "en",
        "source_term": "autonomous motor vehicle",
        "target_zh": "自动驾驶机动车",
        "priority_level": 1,
        "statutory_definition": (
            "装配自动驾驶系统(autonomous system)、可在无人工持续操作下行驶的机动车辆。"
        ),
        "source_reference": (
            "SG Road Traffic Act 1961 / Road Traffic (Autonomous Motor Vehicles) Rules 2017 r 2"
        ),
        "match_keywords": ["autonomous motor vehicle", "autonomous system"],
        "review_status": "approved",
    },
    {
        "concept_code": "autonomous_system",
        "country_code": "SG",
        "language": "en",
        "source_term": "autonomous system",
        "target_zh": "自动驾驶系统",
        "priority_level": 1,
        "statutory_definition": (
            "使机动车辆能够在无需自然人物理驾驶或监控的情况下运行的硬件与软件系统。"
        ),
        "source_reference": "SG Road Traffic (Autonomous Motor Vehicles) Rules 2017 r 2",
        "match_keywords": ["autonomous system", "without the active physical control"],
        "review_status": "approved",
    },
    {
        "concept_code": "critical_information_infrastructure",
        "country_code": "SG",
        "language": "en",
        "source_term": "critical information infrastructure",
        "target_zh": "关键信息基础设施(CII)",
        "priority_level": 1,
        "statutory_definition": (
            "对新加坡国家安全、国防、经济或公共安全至关重要的计算机或计算机系统。"
        ),
        "source_reference": "SG Cybersecurity Act 2018 s 7",
        "match_keywords": ["critical information infrastructure", "Commissioner"],
        "review_status": "approved",
    },
    # 法语/阿拉伯语四国关键法定术语
    {
        "concept_code": "personal_data",
        "country_code": "MA",
        "language": "fr",
        "source_term": "données à caractère personnel",
        "target_zh": "个人数据",
        "priority_level": 1,
        "statutory_definition": (
            "任何能够直接或间接识别自然人的信息(摩洛哥第 09-08 号法律第 1 条)。"
        ),
        "source_reference": "MA Loi n° 09-08 Art. 1",
        "review_status": "approved",
    },
    {
        "concept_code": "personal_data",
        "country_code": "TN",
        "language": "fr",
        "source_term": "données à caractère personnel",
        "target_zh": "个人数据",
        "priority_level": 1,
        "statutory_definition": (
            "无论来源或形式,能够直接或间接识别自然人身份的任何信息"
            "(突尼斯 2004-63 号组织法第 4 条)。"
        ),
        "source_reference": "TN Loi organique n° 2004-63 Art. 4",
        "review_status": "approved",
    },
    {
        "concept_code": "personal_data",
        "country_code": "LB",
        "language": "ar",
        "source_term": "البيانات ذات الطابع الشخصي",
        "target_zh": "个人数据",
        "priority_level": 1,
        "statutory_definition": (
            "黎巴嫩第 81/2018 号《电子交易与个人数据法》定义的任何可识别自然人的信息。"
        ),
        "source_reference": "LB Law No. 81/2018",
        "review_status": "approved",
    },
]

# 解读要点确认单(只把法规没有直接定义、但直接影响车企结论的少数关键问题提交律所确认)
SEED_REVIEW_POINTS: list[dict] = [
    {
        "country_code": "SG",
        "point_code": "SG-RP-001",
        "title": "新加坡智能网联汽车车载遥测/行车轨迹与座舱传感器数据的「个人数据」边界判定",
        "question": (
            "PDPA 第 2(1) 条对 personal data 采取宽泛的“结合其他信息可识别特定个人”标准。"
            "车企采集的 VIN 码、高精度行车轨迹、车外摄像头环视视频及车内 DMS 状态数据,"
            "在脱离车主实名账户但保留车辆唯一识别码(VIN)回传中国研发中心时,"
            "是否仍被 PDPC 认定为个人数据并触发第 26 条出境限制?"
        ),
        "impact_on_conclusion": (
            "直接决定车企回传车辆诊断与算法训练数据是否必须签署达到 PDPA 同等保护标准"
            "(Comparable Protection)的数据出境协议及履行告知同意。"
        ),
        "provisional_interpretation": (
            "按严口径处理:只要车企后台能通过 VIN 关联到车主或特定驾驶员,"
            "VIN 绑定的轨迹与车内外影像均按「个人数据」纳管;仅经不可逆脱敏聚合后的统计遥测可豁免。"
        ),
        "concept_code": "personal_data",
        "match_keywords": ["personal data", "transfer", "outside Singapore"],
    },
    {
        "country_code": "SG",
        "point_code": "SG-RP-002",
        "title": "新加坡 LTA 自动驾驶路测数据记录装置(黑匣子)数据的跨境研发回传许可条件",
        "question": (
            "《Road Traffic (Autonomous Motor Vehicles) Rules 2017》强制要求测试车辆配备"
            "数据记录器(data recorder),保存至少 3 年并按 LTA 要求提交。"
            "法规正文未直接写明该黑匣子原始传感器数据能否实时跨境同步至境外算法团队。"
            "实务中 LTA 在核发自动驾驶测试批准(Trial Approval)时是否附加本地化存储或脱敏前置条件?"
        ),
        "impact_on_conclusion": (
            "直接影响自动驾驶车企在新加坡开展 L3/L4 路测时的云端架构设计"
            "(本地云 vs 跨境直传)及 LTA 路测申请表承诺事项。"
        ),
        "provisional_interpretation": (
            "必须在新加坡境内保留完整可随时供 LTA 调取的 3 年法定期限副本;"
            "若需回传境外研发中心,须对车外行人/车牌做模糊化处理并遵守 PDPA 第 26 条跨境传输规则。"
        ),
        "concept_code": "autonomous_motor_vehicle",
        "match_keywords": ["autonomous", "recorder", "trial"],
    },
    {
        "country_code": "SG",
        "point_code": "SG-RP-003",
        "title": (
            "车企海外子公司与国内车联网云平台之间的「数据中介机构(Data Intermediary)」责任拆分"
        ),
        "question": (
            "PDPA 第 4(2) 条规定数据中介机构(Data Intermediary)仅承担保护义务(第 24 条)"
            "与留存限制义务(第 25 条)。若由新加坡本地经销商/子公司作为数据控制者,"
            "国内主机厂云平台仅受托提供车联网技术运维,国内主体能否主张适用 Data Intermediary 豁免?"
        ),
        "impact_on_conclusion": (
            "决定国内主机厂是否直接承担 PDPA 全套同意、访问更正及跨境合规主责,"
            "以及双方数据处理协议(DPA)的条款结构。"
        ),
        "provisional_interpretation": (
            "若国内主机厂将回传数据用于自身的自动驾驶模型训练或产品改进,"
            "则超出纯粹受托处理范围,不能仅以数据中介机构豁免,须作为共同/独立控制者合规。"
        ),
        "concept_code": "data_intermediary",
        "match_keywords": ["data intermediary", "on behalf of"],
    },
    {
        "country_code": "EG",
        "point_code": "EG-RP-001",
        "title": "埃及 NTRA 物联网(IoT)监管框架下车载 eSIM 与车联网平台本地化落地要求",
        "question": (
            "埃及 NTRA 发布的《IoT Regulatory Framework》对车联网/远程信息处理服务商"
            "提出了连接与平台管理要求,出口埃及的智能网联汽车使用漫游 eSIM "
            "还是必须接入埃及持牌电信运营商并部署本地节点?"
        ),
        "impact_on_conclusion": "直接影响埃及市场车联网激活方案、NTRA 型号核准与跨境数据回传路径。",
        "provisional_interpretation": (
            "长期商用车联网服务需通过埃及 NTRA 许可的本地电信运营商提供连接,"
            "涉及个人数据的跨境传输需同步满足埃及第 151/2020 号个人数据保护法许可证要求。"
        ),
        "concept_code": "personal_data",
        "match_keywords": ["انترنت الأشياء", "IoT"],
    },
]


# ==============================================================================
# S2: Pydantic JSON Schemas 与十二类关系类型约束矩阵
# ==============================================================================


class DocumentRelevanceVerdict(BaseModel):
    relevance: Literal["relevant", "related", "out_of_scope"]
    reason: str


class ExtractedEntity(BaseModel):
    type: NodeType
    name_zh: str = Field(min_length=1, max_length=255)
    name_orig: str = Field(default="", max_length=255)
    definition: str | None = None


class ExtractedRelation(BaseModel):
    type: RelationType
    source: str = Field(min_length=1, max_length=255)
    target: str = Field(min_length=1, max_length=255)
    evidence_quote: str = Field(min_length=1)
    confidence: float = Field(default=0.85, ge=0.0, le=1.0)


class ClauseExtractionPayload(BaseModel):
    entities: list[ExtractedEntity] = Field(default_factory=list)
    relations: list[ExtractedRelation] = Field(default_factory=list)


class SemanticJudgeVerdict(BaseModel):
    supports: Literal["yes", "no", "partial", "unavailable"]
    reason: str
    model_name: str | None = None


class CandidateEvaluationResult(BaseModel):
    admitted: bool
    stage: str
    aligned_quote: str | None = None
    confidence: float = 0.0
    judge_verdict: str = "no"
    judge_reason: str = ""
    judge_model: str | None = None
    review_status: str = GraphReviewStatus.PENDING_REVIEW.value


# 十二类语义关系的源/目标节点类型约束矩阵
ALLOWED_RELATION_ENDPOINT_TYPES: dict[RelationType, set[tuple[NodeType, NodeType]]] = {
    RelationType.PUBLISHES: {
        (NodeType.REGULATOR, NodeType.INSTRUMENT),
    },
    RelationType.AMENDS: {
        (NodeType.INSTRUMENT, NodeType.INSTRUMENT),
    },
    RelationType.REPEALS: {
        (NodeType.INSTRUMENT, NodeType.INSTRUMENT),
    },
    RelationType.GOVERNS: {
        (NodeType.INSTRUMENT, NodeType.DATA_CATEGORY),
        (NodeType.INSTRUMENT, NodeType.OBLIGATION),
        (NodeType.OBLIGATION, NodeType.DATA_CATEGORY),
        (NodeType.REGULATOR, NodeType.DATA_CATEGORY),
    },
    RelationType.PROHIBITS: {
        (NodeType.REGULATOR, NodeType.OBLIGATION),
        (NodeType.REGULATOR, NodeType.DATA_CATEGORY),
        (NodeType.INSTRUMENT, NodeType.OBLIGATION),
        (NodeType.INSTRUMENT, NodeType.DATA_CATEGORY),
    },
    RelationType.REQUIRES: {
        (NodeType.REGULATOR, NodeType.OBLIGATION),
        (NodeType.INSTRUMENT, NodeType.OBLIGATION),
    },
    RelationType.EXEMPTS: {
        (NodeType.INSTRUMENT, NodeType.OBLIGATION),
        (NodeType.INSTRUMENT, NodeType.DATA_CATEGORY),
        (NodeType.REGULATOR, NodeType.OBLIGATION),
    },
    RelationType.SUPERVISES: {
        (NodeType.REGULATOR, NodeType.COUNTRY),
        (NodeType.REGULATOR, NodeType.INSTRUMENT),
        (NodeType.REGULATOR, NodeType.OBLIGATION),
        (NodeType.REGULATOR, NodeType.DATA_CATEGORY),
    },
    RelationType.PENALIZES: {
        (NodeType.INSTRUMENT, NodeType.PENALTY),
        (NodeType.REGULATOR, NodeType.PENALTY),
        (NodeType.OBLIGATION, NodeType.PENALTY),
    },
    RelationType.APPLIES_TO_SCENARIO: {
        (NodeType.INSTRUMENT, NodeType.DATA_CATEGORY),
        (NodeType.INSTRUMENT, NodeType.OBLIGATION),
        (NodeType.OBLIGATION, NodeType.DATA_CATEGORY),
    },
    RelationType.RESTRICTS_CROSS_BORDER: {
        (NodeType.INSTRUMENT, NodeType.DATA_CATEGORY),
        (NodeType.INSTRUMENT, NodeType.OBLIGATION),
        (NodeType.REGULATOR, NodeType.DATA_CATEGORY),
        (NodeType.OBLIGATION, NodeType.DATA_CATEGORY),
    },
    RelationType.REQUIRES_ADMISSION: {
        (NodeType.INSTRUMENT, NodeType.OBLIGATION),
        (NodeType.REGULATOR, NodeType.OBLIGATION),
        (NodeType.INSTRUMENT, NodeType.DATA_CATEGORY),
    },
}

# 明确不属于“智能网联汽车数据、个人数据、跨境传输、自动驾驶测试准入”实质规范的文件标题模式
_OUT_OF_SCOPE_TITLE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"courts\s*\(civil and criminal justice\)\s*reform", re.IGNORECASE),
        "司法诉讼程序改革法及其生效通知，与智能网联汽车数据、个人数据保护、跨境传输或自动驾驶准入无关",
    ),
    (
        re.compile(r"statutes\s*\(miscellaneous amendments\)", re.IGNORECASE),
        "综合杂项法案修正案及生效通知，不属于智能网联汽车数据、个人数据跨境或自动驾驶专项法规",
    ),
    (
        re.compile(r"public sector\s*\(governance\)", re.IGNORECASE),
        "公共部门内部治理法案及生效通知，仅规范政府部门间内部治理，不适用商业车企数据合规",
    ),
    (
        re.compile(r"\(commencement\)\s*(?:\(no\.\s*\d+\)\s*)?notification", re.IGNORECASE),
        "仅规定法案生效日期的行政通知(Commencement Notification)，不含实质性合规权利义务或修订条款",
    ),
    (
        re.compile(r"formulaire", re.IGNORECASE),
        "办事申请表格/申报模板文件，仅保留在文档库归档，不进入语义关系图谱抽取",
    ),
    (
        re.compile(
            r"(?:埃及内阁信息与决策支持中心\s*IDSC|黎巴嫩议会\s*LP|摩洛哥总秘书处官方公报\s*SGG|突尼斯官方公报\s*JORT)",
            re.IGNORECASE,
        ),
        "官方立法门户首页导航索引页，非单行实体法规正文，保留归档但不进入语义图谱抽取",
    ),
]

# 仅生效日期通知的文本特征(不得冒充 AMENDS / REPEALS 等语义关系依据)
_COMMENCEMENT_ONLY_RE = re.compile(
    r"\b(?:comes?\s+into\s+operation\s+on|date\s+of\s+commencement|"
    r"appoints?\s+\d{1,2}\s+[A-Za-z]+\s+\d{4}\s+as\s+the\s+date\s+on\s+which)\b",
    re.IGNORECASE,
)


def _deterministic_doc_scope_check(
    title: str, document_type: str = "", source_url: str = ""
) -> tuple[str, str] | None:
    """对明显超出业务范围的文件(如 Courts Reform Act、生效通知、申请表等)执行确定性前置过滤。"""
    if (document_type or "").upper() == "FORM":
        return (
            "out_of_scope",
            "办事申请表格/模板文件(FORM)，仅保留在文档库归档，不进入知识图谱语义关系抽取",
        )
    combined = f"{title} {source_url}".strip()
    for pat, reason in _OUT_OF_SCOPE_TITLE_PATTERNS:
        if pat.search(combined):
            return ("out_of_scope", reason)
    return None


async def _call_llm_doc_relevance(
    title: str,
    document_type: str,
    country_code: str,
    sample_text: str,
    hint: str | None = None,
) -> DocumentRelevanceVerdict | None:
    """调用大模型对文件做三档相关性判定(核心 relevant / 相关 related / 范围外 out_of_scope)。"""
    settings = get_settings()
    if not settings.llm_api_key:
        return None

    system_prompt = (
        "你是跨境智能网联汽车与数据合规专家。请把给定法规文件分为三档:\n"
        "- relevant(核心): 与「智能网联汽车数据、车联网/物联网、个人数据保护、数据跨境传输、"
        "网络安全、自动驾驶道路测试或准入监管」直接相关的主法、核心条例、指南;\n"
        "- related(相关): 属于上述核心法规的配套法规体系(如罚则和解、执法机关名录、上诉程序、"
        "营销触达登记、法定机构名录等),与核心法规同一法律制度,内容较外围;\n"
        "- out_of_scope(范围外): 与上述主题无关的其他领域立法(如法院诉讼程序改革、"
        "公共部门内部治理、"
        "杂项修正案),或仅规定生效日期的通知,或空白申请表。\n"
        "原则:拿不准时不要判为 out_of_scope。\n"
        '请严格输出 JSON: {"relevance": "relevant" | "related" | "out_of_scope", '
        '"reason": "中文判定理由"}'
    )
    user_prompt = (
        f"国家: {country_code}\n"
        f"文件标题: {title}\n"
        f"文件类型: {document_type}\n"
        f"规则初判提示: {hint or '无'}\n"
        f"正文前段摘录:\n{sample_text[:1200]}"
    )
    payload: dict[str, Any] = {
        "model": settings.llm_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.0,
    }
    if "deepseek" in settings.llm_model.lower():
        payload["reasoning_effort"] = "low"

    headers = {
        "Authorization": f"Bearer {settings.llm_api_key}",
        "Content-Type": "application/json",
    }
    url = f"{settings.llm_base_url.rstrip('/')}/chat/completions"
    try:
        request_started()
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code != 200:
                return None
            response_payload = resp.json()
            response_received(response_payload)
            raw = response_payload["choices"][0]["message"]["content"]
            data = _parse_json_object(raw)
            rel = data.get("relevance", "relevant")
            if rel not in ("relevant", "related", "out_of_scope"):
                rel = "related"
            return DocumentRelevanceVerdict(
                relevance=rel,
                reason=str(data.get("reason") or "大模型相关性判定完成"),
            )
    except Exception as exc:
        logger.warning("LLM document relevance call failed for %s: %s", title, exc)
        return None


# 核心法规体系的同一法律制度配套法规(PDPA 罚则和解/上诉/DNC/名录通知等):不得仅凭大模型判为范围外
_RELATED_REGIME_HINT_RE = re.compile(
    r"\((?:composition of offences|appeal|do not call|prescribed healthcare|"
    r"prescribed law enforcement|statutory bodies|appointment of administration)",
    re.IGNORECASE,
)

# 规则对核心文件的初判提示(仅作为大模型提示与无模型时的兜底,不再短路大模型判定)
_CORE_TITLE_HINT_RE = re.compile(
    r"(?:personal\s+data\s+protection|autonomous\s+motor\s+vehicles|transfer\s+limitation|"
    r"key\s+concepts\s+in\s+the\s+pdpa|09-08|2004-63|151|81/2018|iot|"
    r"انترنت\s+الأشياء|حماية\s+البيانات|données\s+à\s+caractère\s+personnel)",
    re.IGNORECASE,
)


def _rule_relevance_hint(title: str) -> tuple[str, str] | None:
    if _RELATED_REGIME_HINT_RE.search(title or ""):
        return (
            "related",
            "核心法规体系的配套法规(罚则和解/上诉/营销登记/机构名录等),同一法律制度但内容较外围",
        )
    if _CORE_TITLE_HINT_RE.search(title or ""):
        return ("relevant", "标题命中个人数据保护/车联网/自动驾驶核心法规关键词")
    return None


async def classify_document_relevance(
    doc: Document, sample_clauses: list[Clause] | None = None
) -> tuple[str, str]:
    """文件级三档相关性: relevant(核心) / related(相关) / out_of_scope(范围外)。

    优先级: 人工覆盖(manual) > 明确无关的硬规则 > 已有大模型判定 > 大模型判定(规则仅作提示)。
    范围外文件保留在文档库与图谱节点中,只是不进入语义关系抽取。
    """
    if doc.relevance_source == "manual":
        return doc.relevance, doc.relevance_note or ""

    det = _deterministic_doc_scope_check(
        doc.title or "", doc.document_type or "", doc.source_url or ""
    )
    if det is not None:
        doc.relevance, doc.relevance_note = det
        doc.relevance_source = "rule"
        return det

    if doc.relevance_source == "llm" and doc.relevance in ("relevant", "related", "out_of_scope"):
        return doc.relevance, doc.relevance_note or ""

    hint = _rule_relevance_hint(doc.title or "")
    sample_text = "\n".join(c.original_text[:400] for c in (sample_clauses or [])[:3])
    llm_verdict = await _call_llm_doc_relevance(
        title=doc.title,
        document_type=doc.document_type,
        country_code=doc.country_code,
        sample_text=sample_text,
        hint=hint[1] if hint else None,
    )
    if llm_verdict is not None:
        rel, reason = llm_verdict.relevance, llm_verdict.reason
        if rel == "out_of_scope" and hint is not None:
            rel = hint[0] if hint[0] != "relevant" else "related"
            reason = f"{reason}(规则提示该文件属核心法规体系,保守保留进入抽取)"
        doc.relevance, doc.relevance_note = rel, reason
        doc.relevance_source = "llm"
        return rel, reason

    doc.relevance = "needs_review"
    doc.relevance_note = "大模型相关性判定不可用，保留原文与已有图谱，等待重试或人工确认"
    doc.relevance_source = "rule"
    return doc.relevance, doc.relevance_note


def align_verbatim_quote(original_text: str, candidate_quote: str) -> str | None:
    """校验或对齐 candidate_quote 为 original_text 的 100% 逐字子串。
    若无法在 original_text 中找到逐字对应的真实片段,返回 None(丢弃该边)。
    """
    if not original_text or not candidate_quote:
        return None
    orig = original_text
    cand = candidate_quote.strip().strip("\"'“”‘’")
    if len(cand) < 8:
        return None

    # 1. 直接逐字子串命中
    if cand in orig:
        return cand

    # 2. 忽略大小写但返回原文中的精确切片
    idx_ci = orig.lower().find(cand.lower())
    if idx_ci >= 0:
        sliced = orig[idx_ci : idx_ci + len(cand)]
        if sliced in orig:
            return sliced

    # 3. 空白符/换行差异对齐(如原文含换行而 LLM 输出为空格),返回原文精确切片
    tokens = [re.escape(tok) for tok in cand.split() if tok]
    if len(tokens) >= 3:
        pattern = r"\s+".join(tokens)
        try:
            m = re.search(pattern, orig, flags=re.IGNORECASE)
            if m:
                sliced = orig[m.start() : m.end()].strip()
                if len(sliced) >= 8 and sliced in orig:
                    return sliced
        except re.error:
            pass

    return None


# 各关系类型的多语种(英/法/阿/中)语义特征词,用于与类型约束矩阵配合拦截错配
_RELATION_SEMANTIC_PATTERNS: dict[RelationType, re.Pattern[str]] = {
    RelationType.AMENDS: re.compile(
        r"(?:\bamend(?:s|ed|ing|ment)?\b|\bsubstitut(?:e|ed|ing)\b|\binsert(?:s|ed|ing)\b|"
        r"\bdelet(?:e|ed|ing)\b|\bmodifi(?:e|é|er|cation)\b|\bremplac(?:é|er)\b|"
        r"تعديل|يعدل|تُعدل|يستبدل|修订|修改)",
        re.IGNORECASE,
    ),
    RelationType.REPEALS: re.compile(
        r"(?:\brepeal(?:s|ed|ing)?\b|\brevok(?:e|es|ed|ing|ation)\b|\bcancel(?:s|led|lation)?\b|"
        r"\babrog(?:e|é|er|ation)\b|\bannul(?:e|é|er|ation)\b|"
        r"إلغاء|يلغى|تلغى|تُلغى|ملغى|废止|撤销)",
        re.IGNORECASE,
    ),
    RelationType.PROHIBITS: re.compile(
        r"(?:\bmust\s+not\b|\bshall\s+not\b|\bno\s+person\s+shall\b|\bprohibit(?:s|ed|ion)?\b|"
        r"\bunlawful\b|\bforbidden\b|\bnot\s+transfer\b|\bnot\s+collect\b|\bnot\s+use\b|"
        r"\binterdit\b|\bne\s+peut\b|\bnul\s+ne\s+peut\b|"
        r"يحظر|لا\s+يجوز|يمنع|ممنوع|禁止|不得)",
        re.IGNORECASE,
    ),
    RelationType.REQUIRES: re.compile(
        r"(?:\bmust\b|\bshall\b|\brequire(?:s|d|ment|ments)?\b|\bobligation\b|\bduty\b|"
        r"\bensure\b|\bnotify\b|\bdesignate\b|\bappoint\b|\bretain\b|\bkeep\b|\bmaintain\b|"
        r"\bdoit\b|\bdoivent\b|\btenu(?:e|s)?\b|\bobligatoire\b|\bincombe\b|"
        r"يجب|يلتزم|يتعين|تلتزم|وجب|تخضع|يخضع|يشترط|يتوجب|应当|必须|强制)",
        re.IGNORECASE,
    ),
    RelationType.EXEMPTS: re.compile(
        r"(?:\bexempt(?:s|ed|ion|ions)?\b|\bexception(?:s)?\b|\bdoes\s+not\s+apply\b|"
        r"\bdo\s+not\s+apply\b|\bnot\s+required\b|\bwithout\s+(?:the\s+)?consent\b|"
        r"\bdata\s+intermediary\b|\bschedule\b|\bdispens(?:e|é)\b|\bne\s+s['’]applique\s+pas\b|"
        r"\bdérogation\b|استثناء|يعفى|معفى|لا\s+تسري|لا\s+يخضع|إعفاء|豁免|不适用|例外)",
        re.IGNORECASE,
    ),
    RelationType.PENALIZES: re.compile(
        r"(?:\boffence\b|\bfine(?:d|s)?\b|\bpenalt(?:y|ies)\b|\bimprisonment\b|"
        r"\bliable\s+on\s+conviction\b|\bfinancial\s+penalty\b|\bconviction\b|\bsanction(?:s)?\b|"
        r"\bamende(?:s)?\b|\bpuni(?:e|s)?\b|\bemprisonnement\b|\bpénalit(?:é|és)\b|\binfraction\b|"
        r"عقوبة|عقوبات|غرامة|يعاقب|حبس|سجن|مخالفة|罚款|罚金|处罚|监禁)",
        re.IGNORECASE,
    ),
    RelationType.RESTRICTS_CROSS_BORDER: re.compile(
        r"(?:\btransfer(?:s|red|ring)?\b.*\boutside\b|\boutside\s+singapore\b|"
        r"\bcross[- ]border\b|\bcountry\s+or\s+territory\s+outside\b|"
        r"\babroad\b|\bforeign\s+country\b|"
        r"\btransfert\b|\btransfér(?:er|é|és)\b|\bhors\s+du\s+territoire\b|"
        r"\bà\s+l['’]étranger\b|\b(?:pays|état|etat)\s+étranger\b|"
        r"خارج|عبر\s+الحدود|دولة\s+أجنبية|نقل\s+البيانات|نقل.*أجنبية|跨境|出境|境外)",
        re.IGNORECASE | re.DOTALL,
    ),
    RelationType.REQUIRES_ADMISSION: re.compile(
        r"(?:\bauthoris(?:e|ed|ation)\b|\bapprov(?:e|ed|al)\b|\btrial\b|\blicen[cs](?:e|ed|ing)\b|"
        r"\bpermit\b|\bregistration\b|\bapplication\b|\bprior\s+approval\b|"
        r"\bautorisation\b|\bdéclaration\s+préalable\b|\bhomologation\b|\bagrément\b|"
        r"ترخيص|تصريح|موافقة\s+مسبقة|تسجيل|اعتماد|准入|许可|批准|审批|备案|路测申请)",
        re.IGNORECASE,
    ),
}


def validate_relation_type_constraints(
    relation_type: RelationType | str,
    source_type: NodeType | str,
    target_type: NodeType | str,
    evidence_quote: str = "",
    source_name: str = "",
    target_name: str = "",
    document_title: str = "",
) -> tuple[bool, str]:
    """校验十二类语义关系的类型约束矩阵及前置领域规则。"""
    try:
        rel = (
            relation_type
            if isinstance(relation_type, RelationType)
            else RelationType(str(relation_type).upper())
        )
        src_t = (
            source_type if isinstance(source_type, NodeType) else NodeType(str(source_type).upper())
        )
        dst_t = (
            target_type if isinstance(target_type, NodeType) else NodeType(str(target_type).upper())
        )
    except ValueError as exc:
        return False, f"非法节点或关系枚举类型: {exc}"

    allowed_pairs = ALLOWED_RELATION_ENDPOINT_TYPES.get(rel, set())
    if (src_t, dst_t) not in allowed_pairs:
        return (
            False,
            f"违反类型约束矩阵: 关系 {rel.value} 不允许 {src_t.value} -> {dst_t.value} 组合",
        )

    if document_title:
        scope_check = _deterministic_doc_scope_check(document_title)
        if scope_check is not None:
            return False, f"文件超出业务范围({scope_check[0]}): {scope_check[1]}"

    if rel in (RelationType.AMENDS, RelationType.REPEALS):
        if (
            source_name
            and target_name
            and source_name.strip().lower() == target_name.strip().lower()
        ):
            return False, f"{rel.value} 关系的源法规与目标法规不能相同"
        if evidence_quote and _COMMENCEMENT_ONLY_RE.search(evidence_quote):
            # 如果只是生效日期通知而无实质条文修改,拒绝作为 AMENDS / REPEALS
            if not re.search(
                r"\b(?:is\s+amended\s+by|is\s+repealed|substituted|deleted|inserted)\b",
                evidence_quote,
                re.IGNORECASE,
            ):
                return (
                    False,
                    f"证据片段仅为生效日期通知(Commencement Notice)，不构成实质 {rel.value} 关系",
                )

    return True, "通过类型约束矩阵校验"


def _deterministic_semantic_judge(
    relation_type: RelationType,
    source_name: str,
    source_type: NodeType,
    target_name: str,
    target_type: NodeType,
    evidence_quote: str,
    document_title: str = "",
) -> SemanticJudgeVerdict:
    """确定性语义裁判基线:检查证据片段是否真实具备支撑该关系的法律语义。"""
    if document_title:
        scope_check = _deterministic_doc_scope_check(document_title)
        if scope_check is not None:
            return SemanticJudgeVerdict(
                supports="no",
                reason=f"来源文件属于超出范围文件({document_title}): {scope_check[1]}",
            )

    quote_clean = (evidence_quote or "").strip()
    if len(quote_clean) < 12:
        return SemanticJudgeVerdict(
            supports="no",
            reason="证据片段过短，无法独立构成完整的法律规范语义支撑",
        )

    # 拦截将纯生效日期通知当作修订/废止/义务/禁止等实质关系
    if _COMMENCEMENT_ONLY_RE.search(quote_clean) and not re.search(
        r"\b(?:is\s+amended\s+by|is\s+repealed|must|shall|personal\s+data|autonomous)\b",
        quote_clean,
        re.IGNORECASE,
    ):
        return SemanticJudgeVerdict(
            supports="no",
            reason="证据片段仅规定法规生效日期(Commencement)，未包含所声称的实体权利义务或修订语义",
        )

    # 拦截法院程序/民事诉讼条款冒充数据合规关系
    if re.search(
        r"\b(?:General\s+Division\s+of\s+ the\s+High\s+Court|Rules\s+of\s+Court|"
        r"civil\s+proceedings|appellate\s+court|magistrate)\b",
        quote_clean,
        re.IGNORECASE,
    ) and not re.search(
        r"\b(?:personal\s+data|autonomous|data\s+breach|commission)\b",
        quote_clean,
        re.IGNORECASE,
    ):
        return SemanticJudgeVerdict(
            supports="no",
            reason="证据片段属于普通法院诉讼管辖与司法程序规定，不支撑汽车数据或个人数据合规关系",
        )

    # 按关系类型核验核心法律动词/语义模式
    pat = _RELATION_SEMANTIC_PATTERNS.get(relation_type)
    if pat is not None and not pat.search(quote_clean):
        return SemanticJudgeVerdict(
            supports="no",
            reason=f"证据片段缺乏支撑 {relation_type.value} 关系的核心法律谓词或规范语义",
        )

    return SemanticJudgeVerdict(
        supports="yes",
        reason=(
            f"证据片段明确包含支撑 {source_name} -[{relation_type.value}]-> "
            f"{target_name} 的法律规范表述"
        ),
    )


async def _call_llm_judge(
    relation_type: RelationType,
    source_name: str,
    source_type: NodeType,
    target_name: str,
    target_type: NodeType,
    evidence_quote: str,
    document_title: str = "",
) -> SemanticJudgeVerdict | None:
    """第二次大模型调用(独立语义裁判,prompt=kg-judge-v2):
    判断 evidence_quote 是否真实支撑 source -[relation_type]-> target。
    """
    settings = get_settings()
    if not settings.llm_api_key:
        return None

    system_prompt = (
        f"你是跨境汽车数据合规知识图谱的独立语义裁判(版本 {JUDGE_PROMPT_VERSION})。\n"
        "你的唯一任务是严格核查给定的「法规原文摘录(evidence_quote)」是否在语义上真实支撑"
        "所声称的三元组关系: 源实体(source) -[关系(relation_type)]-> 目标实体(target)。\n"
        "裁判规则:\n"
        "1. 若证据摘录与该关系无关(如仅写明生效日期却声称修订 AMENDS、"
        "普通法院程序却声称数据出境限制、"
        '或者证据中完全没有提及与目标实体对应的概念/义务/罚则)，必须输出 "supports": "no"。\n'
        '2. 若证据摘录仅间接提及相关背景，但未直接确立该关系，输出 "supports": "partial"。\n'
        '3. 只有当证据摘录直接、明确地确立了该法律关系时，才输出 "supports": "yes"。\n'
        '请严格输出 JSON: {"supports": "yes" | "no" | "partial", "reason": "简明中文裁判理由"}'
    )
    user_prompt = (
        f"所属法规文件: {document_title or '官方法规'}\n"
        f"声称关系类型: {relation_type.value}\n"
        f"源节点: [{source_type.value}] {source_name}\n"
        f"目标节点: [{target_type.value}] {target_name}\n"
        f"法规原文摘录(evidence_quote):\n{evidence_quote}"
    )
    payload: dict[str, Any] = {
        "model": settings.llm_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.0,
    }
    if "deepseek" in settings.llm_model.lower():
        payload["reasoning_effort"] = "low"

    headers = {
        "Authorization": f"Bearer {settings.llm_api_key}",
        "Content-Type": "application/json",
    }
    url = f"{settings.llm_base_url.rstrip('/')}/chat/completions"
    try:
        request_started()
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code != 200:
                return None
            response_payload = resp.json()
            response_received(response_payload)
            raw = response_payload["choices"][0]["message"]["content"]
            data = _parse_json_object(raw)
            sup = str(data.get("supports", "no")).lower().strip()
            if sup not in ("yes", "no", "partial") or not str(data.get("reason") or "").strip():
                return None
            return SemanticJudgeVerdict(
                supports=sup,  # type: ignore[arg-type]
                reason=str(data.get("reason") or ""),
                model_name=str(response_payload.get("model") or settings.llm_model),
            )
    except Exception as exc:
        logger.warning("LLM semantic judge failed: %s", exc)
        return None


async def judge_extracted_relation(
    relation_type: RelationType,
    source_name: str,
    source_type: NodeType,
    target_name: str,
    target_type: NodeType,
    evidence_quote: str,
    document_title: str = "",
    use_llm: bool = True,
) -> SemanticJudgeVerdict:
    """结合确定性领域规则与独立大模型裁判(kg-judge-v2)输出 supports: yes/no/partial。"""
    det = _deterministic_semantic_judge(
        relation_type=relation_type,
        source_name=source_name,
        source_type=source_type,
        target_name=target_name,
        target_type=target_type,
        evidence_quote=evidence_quote,
        document_title=document_title,
    )
    if det.supports == "no":
        return det

    if use_llm:
        llm_v = await _call_llm_judge(
            relation_type=relation_type,
            source_name=source_name,
            source_type=source_type,
            target_name=target_name,
            target_type=target_type,
            evidence_quote=evidence_quote,
            document_title=document_title,
        )
        if llm_v is not None:
            return llm_v
        return SemanticJudgeVerdict(
            supports="unavailable", reason="独立语义裁判不可用；确定性关键词检查不能替代语义裁判"
        )

    return det


async def evaluate_kg_candidate(
    *,
    relation_type: RelationType | str,
    source_type: NodeType | str,
    source_name: str,
    target_type: NodeType | str,
    target_name: str,
    evidence_quote: str,
    clause_original_text: str,
    document_title: str = "",
    confidence: float = 0.85,
    use_llm_judge: bool = True,
) -> CandidateEvaluationResult:
    """对单条候选语义边执行 S2 四重质量闸门校验:
    1. 文件范围与类型约束矩阵;
    2. 条款原文逐字子串对齐;
    3. 独立语义裁判(yes 入库 / partial 降级并拒绝入主图 / no 丢弃)。
    """
    ok_type, type_reason = validate_relation_type_constraints(
        relation_type=relation_type,
        source_type=source_type,
        target_type=target_type,
        evidence_quote=evidence_quote,
        source_name=source_name,
        target_name=target_name,
        document_title=document_title,
    )
    if not ok_type:
        stage = "document_scope" if "超出业务范围" in type_reason else "type_constraint"
        return CandidateEvaluationResult(
            admitted=False,
            stage=stage,
            aligned_quote=None,
            confidence=0.0,
            judge_verdict="no",
            judge_reason=type_reason,
            review_status=GraphReviewStatus.REJECTED.value,
        )

    aligned = align_verbatim_quote(clause_original_text, evidence_quote)
    if aligned is None:
        return CandidateEvaluationResult(
            admitted=False,
            stage="verbatim_quote",
            aligned_quote=None,
            confidence=0.0,
            judge_verdict="no",
            judge_reason="evidence_quote 不是条款原文(original_text)的真实逐字子串",
            review_status=GraphReviewStatus.REJECTED.value,
        )

    rel_enum = (
        relation_type
        if isinstance(relation_type, RelationType)
        else RelationType(str(relation_type).upper())
    )
    src_enum = (
        source_type if isinstance(source_type, NodeType) else NodeType(str(source_type).upper())
    )
    dst_enum = (
        target_type if isinstance(target_type, NodeType) else NodeType(str(target_type).upper())
    )

    verdict = await judge_extracted_relation(
        relation_type=rel_enum,
        source_name=source_name,
        source_type=src_enum,
        target_name=target_name,
        target_type=dst_enum,
        evidence_quote=aligned,
        document_title=document_title,
        use_llm=use_llm_judge,
    )
    if verdict.supports in ("no", "unavailable"):
        return CandidateEvaluationResult(
            admitted=False,
            stage="semantic_judge_unavailable"
            if verdict.supports == "unavailable"
            else "semantic_judge",
            aligned_quote=aligned,
            confidence=0.0,
            judge_verdict=verdict.supports,
            judge_reason=verdict.reason,
            judge_model=verdict.model_name,
            review_status=GraphReviewStatus.REJECTED.value,
        )

    if verdict.supports == "partial":
        downgraded = round(min(confidence * 0.6, 0.55), 2)
        return CandidateEvaluationResult(
            admitted=False,
            stage="semantic_judge_partial",
            aligned_quote=aligned,
            confidence=downgraded,
            judge_verdict="partial",
            judge_reason=verdict.reason,
            judge_model=verdict.model_name,
            review_status=GraphReviewStatus.REJECTED.value,
        )

    return CandidateEvaluationResult(
        admitted=True,
        stage="passed",
        aligned_quote=aligned,
        confidence=round(max(0.0, min(1.0, confidence)), 2),
        judge_verdict="yes",
        judge_reason=verdict.reason,
        judge_model=verdict.model_name,
        review_status=GraphReviewStatus.PENDING_REVIEW.value,
    )


def _strip_json_fence(raw: str) -> str:
    txt = (raw or "").strip()
    if txt.startswith("```"):
        txt = re.sub(r"^```(?:json)?\s*", "", txt, flags=re.IGNORECASE)
        txt = re.sub(r"\s*```$", "", txt)
    return txt.strip()


def _parse_json_object(raw: str) -> dict[str, Any]:
    cleaned = _strip_json_fence(raw)
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        obj = json.loads(cleaned[start : end + 1])
        if isinstance(obj, dict):
            return obj
    raise ValueError("Unable to parse JSON object from LLM response")


# 同义实体跨条款归一映射表(确保同一概念跨条款合并为同一 node_key,同时保留各自抽取出的细粒度节点)
_CANONICAL_ENTITY_ALIASES: list[tuple[NodeType, re.Pattern[str], str, str, str]] = [
    (
        NodeType.DATA_CATEGORY,
        re.compile(
            r"^(?:个人数据|个人资料|personal\s+data|données\s+à\s+caractère\s+personnel|البيانات\s+الشخصية)$",
            re.I,
        ),
        "data:personal_data",
        "个人数据(Personal Data)",
        "Personal Data",
    ),
    (
        NodeType.DATA_CATEGORY,
        re.compile(
            r"(?:敏感个人数据|敏感数据|sensitive\s+personal\s+data|données\s+sensibles|بيانات\s+حساسة)",
            re.I,
        ),
        "data:sensitive_personal_data",
        "敏感个人数据(含生物识别与位置轨迹)",
        "Sensitive Personal Data",
    ),
    (
        NodeType.DATA_CATEGORY,
        re.compile(
            r"(?:数据记录器|黑匣子|data\s+recorder|black\s+box|自动驾驶传感器|av\s+sensor)",
            re.I,
        ),
        "data:av_recorder_and_sensor_data",
        "自动驾驶车载数据记录器(黑匣子)与传感器数据",
        "Autonomous Vehicle Data Recorder & Sensor Data",
    ),
    (
        NodeType.DATA_CATEGORY,
        re.compile(r"(?:物联网|车联网|iot|telematics|远程信息处理)", re.I),
        "data:iot_and_telematics_data",
        "车联网与物联网终端运行及连接数据(IoT/Telematics)",
        "IoT & Vehicle Telematics Data",
    ),
    (
        NodeType.DATA_CATEGORY,
        re.compile(r"(?:数据泄露|安全事件|data\s+breach|notifiable\s+data\s+breach)", re.I),
        "data:notifiable_breach_data",
        "须通报的数据泄露事件受影响个人数据",
        "Notifiable Data Breach Affected Personal Data",
    ),
]


def normalize_entity_node_key(
    node_type: NodeType, name_zh: str, name_orig: str | None = None
) -> tuple[str, str, str]:
    """将抽取出的实体归一为稳定的 (node_key, canonical_name_zh, canonical_name_orig)。
    同一概念跨条款自动合并,不同义务/罚则/数据类型生成独立语义键。
    """
    zh_clean = re.sub(r"\s+", " ", (name_zh or "").strip())
    orig_clean = re.sub(r"\s+", " ", (name_orig or zh_clean).strip())
    combined = f"{zh_clean} {orig_clean}".strip()

    for c_type, pat, key, canon_zh, canon_orig in _CANONICAL_ENTITY_ALIASES:
        if c_type == node_type and (
            pat.search(zh_clean) or pat.search(orig_clean) or pat.search(combined)
        ):
            return key, canon_zh, canon_orig

    prefix_map = {
        NodeType.COUNTRY: "country",
        NodeType.REGULATOR: "reg",
        NodeType.INSTRUMENT: "inst",
        NodeType.DATA_CATEGORY: "data",
        NodeType.OBLIGATION: "obl",
        NodeType.PENALTY: "pen",
    }
    prefix = prefix_map.get(node_type, "node")
    slug_src = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "_", zh_clean.lower()).strip("_")
    if not slug_src:
        slug_src = re.sub(r"[^a-z0-9]+", "_", orig_clean.lower()).strip("_") or "entity"
    return f"{prefix}:{slug_src[:56]}", zh_clean[:255], orig_clean[:255]


async def _call_llm_extract_clause(
    clause_text: str,
    clause_number: str | None,
    document_title: str,
    publisher_name: str,
    country_code: str,
) -> tuple[ClauseExtractionPayload, str] | None:
    """调用大模型从单条法规条款中按固定 JSON Schema 抽取六要素实体与十二类语义关系。"""
    settings = get_settings()
    if not settings.llm_api_key:
        return None

    system_prompt = (
        f"你是跨境智能网联汽车与数据合规知识图谱抽取引擎(版本 {EXTRACTOR_PROMPT_VERSION})。\n"
        "请从给定的官方法规条款原文中抽取真实存在的「六要素实体」与「十二类语义关系」。\n"
        "【六要素节点类型(NodeType)】:\n"
        "- REGULATOR: 监管机构/主管机关\n"
        "- INSTRUMENT: 法规文件/法案/条例/指南\n"
        "- DATA_CATEGORY: 具体数据类型(如个人数据、敏感个人数据、车载黑匣子记录器数据、"
        "车联网IoT数据、受托处理数据等)\n"
        "- OBLIGATION: 具体合规义务(如跨境传输同等保护义务、告知同意义务、数据泄露3日通报义务、"
        "自动驾驶路测事前批准义务、黑匣子数据3年留存义务、CNDP/INPDP跨境申报许可义务等)\n"
        "- PENALTY: 具体违规罚则(必须体现该条款规定的具体罚款金额、行政处罚或监禁刑期)\n"
        "【十二类关系枚举(RelationType)及强制类型约束】:\n"
        "- AMENDS: INSTRUMENT -> INSTRUMENT (仅当条款明确修订另一部法规时)\n"
        "- REPEALS: INSTRUMENT -> INSTRUMENT (仅当条款明确废止/撤销另一部法规或旧规定时)\n"
        "- GOVERNS: INSTRUMENT/OBLIGATION -> DATA_CATEGORY/OBLIGATION\n"
        "- PROHIBITS: REGULATOR/INSTRUMENT -> OBLIGATION/DATA_CATEGORY (含禁止/不得语义)\n"
        "- REQUIRES: REGULATOR/INSTRUMENT -> OBLIGATION (含必须/应当履行某项义务语义)\n"
        "- EXEMPTS: INSTRUMENT/REGULATOR -> OBLIGATION/DATA_CATEGORY (含豁免/例外/不适用语义)\n"
        "- PENALIZES: INSTRUMENT/REGULATOR/OBLIGATION -> PENALTY (含违法罚款/监禁/行政处罚语义)\n"
        "- APPLIES_TO_SCENARIO: INSTRUMENT/OBLIGATION -> DATA_CATEGORY/OBLIGATION\n"
        "- RESTRICTS_CROSS_BORDER: INSTRUMENT/REGULATOR/OBLIGATION -> DATA_CATEGORY/OBLIGATION "
        "(条款限制数据跨境/出境传输)\n"
        "- REQUIRES_ADMISSION: INSTRUMENT/REGULATOR -> OBLIGATION/DATA_CATEGORY "
        "(条款要求自动驾驶路测准入批准、IoT许可或数据处理事前申报审批)\n"
        "【硬性要求】:\n"
        "1. 每个关系的 evidence_quote 必须直接从给定「条款原文」中逐字复制(15~220字符)，"
        "严禁改写、翻译或拼接省略号！\n"
        "2. 关系中的 source 或 target 若指代本法规文件本身，请直接填写给定的「所属法规文件标题」；"
        "若指代发布/监管机构，填写给定的「发布机构」。\n"
        "3. 如果该条款只是目录、纯粹定义或无实质合规关系，请返回空列表。\n"
        "请严格按以下 JSON Schema 输出:\n"
        '{"entities": [{"type": "DATA_CATEGORY|OBLIGATION|PENALTY|INSTRUMENT|REGULATOR", '
        '"name_zh": "精炼中文名称", "name_orig": "原文对应术语", "definition": "简要说明"}], '
        '"relations": [{"type": "REQUIRES|...", "source": "源实体名或本法规标题", '
        '"target": "目标实体中文名", "evidence_quote": "逐字复制的原文子串", "confidence": 0.9}]}'
    )
    user_prompt = (
        f"国家: {country_code}\n"
        f"发布机构(REGULATOR): {publisher_name}\n"
        f"所属法规文件标题(INSTRUMENT): {document_title}\n"
        f"条号: {clause_number or '-'}\n"
        f"条款原文:\n{clause_text}"
    )

    payload: dict[str, Any] = {
        "model": settings.llm_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.1,
    }
    if "deepseek" in settings.llm_model.lower():
        payload["thinking"] = {"type": settings.llm_kg_extraction_thinking}
        if settings.llm_kg_extraction_thinking == "enabled":
            payload["reasoning_effort"] = "low"

    headers = {
        "Authorization": f"Bearer {settings.llm_api_key}",
        "Content-Type": "application/json",
    }
    url = f"{settings.llm_base_url.rstrip('/')}/chat/completions"

    for attempt in range(2):
        try:
            request_started()
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(url, headers=headers, json=payload)
                if resp.status_code != 200:
                    await asyncio.sleep(0.8 * (attempt + 1))
                    continue
                response_payload = resp.json()
                response_received(response_payload)
                raw = response_payload["choices"][0]["message"]["content"]
                data = _parse_json_object(raw)
                if not isinstance(data.get("entities"), list) or not isinstance(
                    data.get("relations"), list
                ):
                    raise ValueError(
                        "Extraction payload requires explicit entity and relation arrays"
                    )
                parsed = ClauseExtractionPayload.model_validate(data)
                return parsed, str(response_payload.get("model") or settings.llm_model)
        except (
            ValidationError,
            ValueError,
            json.JSONDecodeError,
            httpx.HTTPError,
            KeyError,
        ) as exc:
            logger.debug("Clause extraction attempt %d failed: %s", attempt + 1, exc)
            await asyncio.sleep(0.6 * (attempt + 1))
    return None


async def _find_matching_clause(
    session: AsyncSession, country_code: str, keywords: list[str]
) -> Clause | None:
    """在指定国家已解析的真实条款中查找包含关键词的证据条款。"""
    stmt = (
        select(Clause)
        .join(DocumentVersion, DocumentVersion.id == Clause.version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(
            Document.country_code == country_code.upper(),
            DocumentVersion.state == "parsed",
        )
        .order_by(Clause.ordinal)
        .limit(500)
    )
    clauses = list((await session.execute(stmt)).scalars().all())
    if not clauses:
        return None
    for kw in keywords:
        pat = re.compile(re.escape(kw), re.IGNORECASE)
        for c in clauses:
            if pat.search(c.original_text):
                return c
    return clauses[0]


async def seed_glossary_and_review_points(session: AsyncSession) -> dict[str, int]:
    """初始化两层法律术语库与解读要点确认单(幂等,自动关联库内真实证据条款)。"""
    terms_added = 0
    points_added = 0

    for item in SEED_GLOSSARY_TERMS:
        exists = (
            await session.execute(
                select(GlossaryTerm).where(
                    GlossaryTerm.concept_code == item["concept_code"],
                    GlossaryTerm.country_code == item["country_code"],
                    GlossaryTerm.language == item["language"],
                )
            )
        ).scalar_one_or_none()
        if exists is not None:
            continue

        evidence_clause = None
        if item.get("country_code") and item.get("match_keywords"):
            evidence_clause = await _find_matching_clause(
                session, item["country_code"], item["match_keywords"]
            )

        term = GlossaryTerm(
            concept_code=item["concept_code"],
            country_code=item["country_code"],
            language=item["language"],
            source_term=item["source_term"],
            target_zh=item["target_zh"],
            priority_level=item["priority_level"],
            statutory_definition=item["statutory_definition"],
            source_reference=item["source_reference"],
            evidence_clause_id=evidence_clause.id if evidence_clause else None,
            review_status="pending",
        )
        session.add(term)
        terms_added += 1

    await session.flush()

    for rp in SEED_REVIEW_POINTS:
        exists = (
            await session.execute(
                select(ReviewPoint).where(ReviewPoint.point_code == rp["point_code"])
            )
        ).scalar_one_or_none()
        if exists is not None:
            continue

        clause = await _find_matching_clause(
            session, rp["country_code"], rp.get("match_keywords", [])
        )
        term = (
            await session.execute(
                select(GlossaryTerm).where(
                    GlossaryTerm.concept_code == rp["concept_code"],
                    GlossaryTerm.country_code == rp["country_code"],
                )
            )
        ).scalar_one_or_none()
        if term is None:
            term = (
                await session.execute(
                    select(GlossaryTerm).where(
                        GlossaryTerm.concept_code == rp["concept_code"],
                        GlossaryTerm.country_code.is_(None),
                    )
                )
            ).scalar_one_or_none()

        session.add(
            ReviewPoint(
                country_code=rp["country_code"],
                point_code=rp["point_code"],
                title=rp["title"],
                question=rp["question"],
                impact_on_conclusion=rp["impact_on_conclusion"],
                provisional_interpretation=rp["provisional_interpretation"],
                clause_id=clause.id if clause else None,
                term_id=term.id if term else None,
                status=ReviewPointStatus.OPEN.value,
            )
        )
        points_added += 1

    await session.commit()
    return {"terms_added": terms_added, "review_points_added": points_added}


async def _upsert_node(
    session: AsyncSession,
    *,
    country_code: str,
    node_type: NodeType,
    node_key: str,
    name_zh: str,
    name_orig: str | None = None,
    summary: str | None = None,
    attributes: dict | None = None,
    document_id: uuid.UUID | None = None,
    evidence_clause_id: uuid.UUID | None = None,
    review_status: str = GraphReviewStatus.PENDING_REVIEW.value,
) -> KgNode:
    node = (
        await session.execute(
            select(KgNode).where(
                KgNode.country_code == country_code,
                KgNode.node_key == node_key,
            )
        )
    ).scalar_one_or_none()
    if node is None:
        attrs = dict(attributes or {})
        if evidence_clause_id:
            attrs["evidence_clause_ids"] = [str(evidence_clause_id)]
        node = KgNode(
            country_code=country_code,
            node_type=node_type.value,
            node_key=node_key,
            name_zh=name_zh[:255],
            name_orig=(name_orig or "")[:255] or None,
            summary=summary,
            attributes=attrs,
            document_id=document_id,
            evidence_clause_id=evidence_clause_id,
            review_status=review_status,
        )
        session.add(node)
        await session.flush()
    else:
        from app.knowledge.history import propose, snapshot

        if node.review_status in ("approved", "rejected"):
            proposed = dict(
                name_zh=name_zh[:255],
                name_orig=name_orig,
                summary=summary,
                attributes=attributes,
                evidence_clause_id=str(evidence_clause_id) if evidence_clause_id else None,
            )
            await propose(session, node, proposed)
            return node
        snapshot(session, node, "before_machine_revision")
        if name_zh[:255] != node.name_zh or (summary and summary != node.summary):
            await propose(
                session, node, dict(name_zh=name_zh[:255], name_orig=name_orig, summary=summary)
            )
        if evidence_clause_id and node.evidence_clause_id is None:
            node.evidence_clause_id = evidence_clause_id
        if node.review_status == GraphReviewStatus.SUPERSEDED.value:
            node.review_status = review_status
        merged_attrs = dict(node.attributes or {})
        if attributes:
            merged_attrs.update(attributes)
        if evidence_clause_id:
            ev_list = list(merged_attrs.get("evidence_clause_ids") or [])
            cid_str = str(evidence_clause_id)
            if cid_str not in ev_list:
                ev_list.append(cid_str)
            merged_attrs["evidence_clause_ids"] = ev_list
        node.attributes = merged_attrs
    return node


async def _upsert_edge(
    session: AsyncSession,
    *,
    country_code: str,
    source_node: KgNode,
    target_node: KgNode,
    relation_type: RelationType,
    evidence_clause: Clause,
    evidence_quote: str,
    extractor_model: str,
    confidence: float = 0.85,
    judge_verdict: str = "yes",
    judge_reason: str | None = None,
    is_structural: bool = False,
    review_status: str = GraphReviewStatus.PENDING_REVIEW.value,
) -> KgEdge | None:
    aligned = align_verbatim_quote(evidence_clause.original_text, evidence_quote)
    if aligned is None:
        if is_structural:
            aligned = evidence_clause.original_text[:200].strip()
        else:
            return None

    edge = (
        await session.execute(
            select(KgEdge)
            .where(
                KgEdge.source_node_id == source_node.id,
                KgEdge.target_node_id == target_node.id,
                KgEdge.relation_type == relation_type.value,
                KgEdge.evidence_clause_id == evidence_clause.id,
                KgEdge.review_status != GraphReviewStatus.SUPERSEDED.value,
            )
            .order_by(
                (KgEdge.review_status == "approved").desc(), KgEdge.created_at.desc(), KgEdge.id
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if edge is None:
        edge = KgEdge(
            country_code=country_code,
            source_node_id=source_node.id,
            target_node_id=target_node.id,
            relation_type=relation_type.value,
            evidence_clause_id=evidence_clause.id,
            evidence_quote=aligned,
            extractor_model=extractor_model,
            confidence=confidence,
            judge_verdict=judge_verdict,
            judge_reason=judge_reason,
            is_structural=is_structural,
            review_status=review_status,
        )
        session.add(edge)
        await session.flush()
    else:
        from app.knowledge.history import propose, snapshot

        proposed = dict(
            evidence_quote=aligned,
            extractor_model=extractor_model,
            confidence=confidence,
            judge_verdict=judge_verdict,
            judge_reason=judge_reason,
            is_structural=is_structural,
        )
        if edge.review_status in ("approved", "rejected"):
            if any(getattr(edge, key) != value for key, value in proposed.items()):
                await propose(session, edge, proposed)
            return edge
        if all(getattr(edge, key) == value for key, value in proposed.items()):
            return edge
        snapshot(session, edge, "before_machine_revision")
        edge.review_status = "superseded"
        edge = KgEdge(
            country_code=country_code,
            source_node_id=source_node.id,
            target_node_id=target_node.id,
            relation_type=relation_type.value,
            evidence_clause_id=evidence_clause.id,
            review_status=review_status,
            **proposed,
        )
        session.add(edge)
        await session.flush()

    return edge


def _score_clause_for_extraction(clause: Clause) -> int:
    """对条款所含合规要素信号打分,优先抽取包含实质权利义务、出境、准入、豁免、修订、罚则的条款。"""
    txt = (clause.original_text or "").strip()
    if len(txt) < 40:
        return -1
    score = 0
    for pat in _RELATION_SEMANTIC_PATTERNS.values():
        if pat.search(txt):
            score += 3
    if re.search(
        r"(?:personal\s+data|autonomous|data\s+intermediary|data\s+breach|transfer|iot|"
        r"données\s+à\s+caractère\s+personnel|حماية\s+البيانات|انترنت\s+الأشياء)",
        txt,
        re.IGNORECASE,
    ):
        score += 2
    return score


def _select_candidate_clauses(clauses: list[Clause], max_clauses: int = 18) -> list[Clause]:
    """从一份相关法规中选出最具实质规范价值的条款送入大模型抽取(兼顾罚则、跨境、准入、豁免、修订等多样性)。"""
    scored: list[tuple[int, int, Clause]] = []
    for idx, c in enumerate(clauses):
        sc = _score_clause_for_extraction(c)
        if sc >= 0:
            scored.append((sc, -idx, c))
    scored.sort(reverse=True)

    selected_ids: set[uuid.UUID] = set()
    selected: list[Clause] = []

    priority_rels = [
        RelationType.PENALIZES,
        RelationType.RESTRICTS_CROSS_BORDER,
        RelationType.REQUIRES_ADMISSION,
        RelationType.EXEMPTS,
        RelationType.AMENDS,
        RelationType.REPEALS,
        RelationType.PROHIBITS,
        RelationType.REQUIRES,
    ]
    for rtype in priority_rels:
        if len(selected) >= max_clauses:
            break
        pat = _RELATION_SEMANTIC_PATTERNS.get(rtype)
        if pat is None:
            continue
        for _sc, _neg_idx, c in scored:
            if c.id not in selected_ids and pat.search(c.original_text or ""):
                selected.append(c)
                selected_ids.add(c.id)
                break

    for _sc, _neg_idx, c in scored:
        if len(selected) >= max_clauses:
            break
        if c.id not in selected_ids:
            selected.append(c)
            selected_ids.add(c.id)

    selected.sort(key=lambda x: x.ordinal)
    return selected


async def supersede_unreviewed_graph_edges(session: AsyncSession, country_code: str) -> int:
    """重跑抽取前,将本国尚未人工复核(pending_review)的语义边与标题推断边标为 superseded。

    已 approved/rejected 的人工复核结果不动,保证 extract-kg 可完整重跑且不累积重复边。
    """
    cc = country_code.upper()
    res = await session.execute(
        update(KgEdge)
        .where(
            KgEdge.country_code == cc,
            KgEdge.review_status == GraphReviewStatus.PENDING_REVIEW.value,
            or_(KgEdge.is_structural.is_(False), KgEdge.judge_verdict == "title_inferred"),
        )
        .values(review_status=GraphReviewStatus.SUPERSEDED.value)
    )
    await session.flush()
    return int(res.rowcount or 0)


_AMENDMENT_TOKEN_RE = re.compile(r"\(amendment(?:\s+no\.?\s*\d+)?\)", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
# 生效通知/勘误/过渡与废止令等非实质性文本,不参与"修正案->主法"的标题推断
_NON_SUBSTANTIVE_LEAF_RE = re.compile(
    r"commencement|corrigend|saving|transitional|revocation|cancellation", re.IGNORECASE
)


def _title_leaf(title: str) -> str:
    """ "主法 - 子法规" 形式标题取子法规部分。"""
    return (title or "").split(" - ")[-1].strip()


def _normalize_instrument_title(title: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", _title_leaf(title).lower()).strip()


def _resolve_instrument_target(
    name: str,
    index: list[tuple[str, KgNode, Document]],
    exclude_id: uuid.UUID | None = None,
    min_ratio: float = 0.88,
) -> KgNode | None:
    """Only unique normalized exact titles resolve; similar years/Amendments are not identity."""
    norm = _normalize_instrument_title(name)
    if not norm:
        return None
    matches = [
        node
        for cand_norm, node, _doc in index
        if norm == cand_norm and (exclude_id is None or node.id != exclude_id)
    ]
    return matches[0] if len(matches) == 1 else None


def _amendment_base_key(leaf: str) -> str:
    """去掉年份与 (Amendment ...) 标记后的标题键,用于把修正案与被修正的主法规配对。"""
    stripped = _AMENDMENT_TOKEN_RE.sub(" ", leaf)
    stripped = re.sub(r"\(\d{4}\s+revised\s+edition\)", " ", stripped, flags=re.IGNORECASE)
    stripped = _YEAR_RE.sub(" ", stripped)
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", stripped.lower()).strip()


async def _add_title_inferred_amend_edges(
    session: AsyncSession,
    cc: str,
    index: list[tuple[str, KgNode, Document]],
    inst_node_by_doc: dict[uuid.UUID, KgNode],
) -> int:
    """由标题层级确定性推断 AMENDS 结构边: 修正案 -> 最近的更早版被修正法规。

    推断边标记 judge_verdict=title_inferred、状态 pending_review,不冒充条款证据,需人工复核。
    """
    added = 0
    leaves = {doc.id: _title_leaf(doc.title) for _n, _node, doc in index}
    for _norm, node, doc in index:
        leaf = leaves[doc.id]
        if _NON_SUBSTANTIVE_LEAF_RE.search(leaf) or not _AMENDMENT_TOKEN_RE.search(leaf):
            continue
        amend_years = _YEAR_RE.findall(leaf)
        if not amend_years:
            continue
        amend_year = int(amend_years[0])
        base_key = _amendment_base_key(leaf)
        if not base_key:
            continue
        candidates: list[tuple[int, KgNode]] = []
        for _n2, node2, doc2 in index:
            if doc2.id == doc.id:
                continue
            leaf2 = leaves[doc2.id]
            if (
                _NON_SUBSTANTIVE_LEAF_RE.search(leaf2)
                or _AMENDMENT_TOKEN_RE.search(leaf2)
                or _amendment_base_key(leaf2) != base_key
            ):
                continue
            years2 = _YEAR_RE.findall(leaf2)
            if years2 and int(years2[0]) < amend_year:
                candidates.append((int(years2[0]), node2))
        if not candidates:
            continue
        candidates.sort(key=lambda t: t[0], reverse=True)
        target = candidates[0][1]

        exists = (
            await session.execute(
                select(KgEdge.id).where(
                    KgEdge.source_node_id == node.id,
                    KgEdge.target_node_id == target.id,
                    KgEdge.relation_type == RelationType.AMENDS.value,
                    KgEdge.review_status != GraphReviewStatus.SUPERSEDED.value,
                )
            )
        ).first()
        if exists:
            continue
        first_clause = (
            await session.execute(
                select(Clause)
                .join(DocumentVersion, DocumentVersion.id == Clause.version_id)
                .where(DocumentVersion.document_id == doc.id)
                .order_by(DocumentVersion.version_no.desc(), Clause.ordinal)
                .limit(1)
            )
        ).scalar_one_or_none()
        if first_clause is None:
            continue
        edge = await _upsert_edge(
            session,
            country_code=cc,
            source_node=inst_node_by_doc.get(doc.id, node),
            target_node=target,
            relation_type=RelationType.AMENDS,
            evidence_clause=first_clause,
            evidence_quote=first_clause.original_text[:200].strip(),
            extractor_model=STRUCTURAL_EXTRACTOR_MODEL,
            confidence=0.8,
            judge_verdict="title_inferred",
            judge_reason="由修正案/被修正法规的标题层级与年份推断,非条款语义证据,待人工核对",
            is_structural=True,
            review_status=GraphReviewStatus.PENDING_REVIEW.value,
        )
        if edge is not None:
            added += 1
    return added


async def supersede_legacy_rule_edges(
    session: AsyncSession, country_code: str | None = None
) -> int:
    """将旧 kg-rule-extractor-v1 规则引擎产出的边标记为 superseded(保留历史行,不物理删除)。"""
    from app.knowledge.history import propose, snapshot

    stmt = select(KgEdge).where(
        KgEdge.extractor_model == LEGACY_RULE_EXTRACTOR_MODEL, KgEdge.review_status != "superseded"
    )
    if country_code:
        stmt = stmt.where(KgEdge.country_code == country_code.upper())
    rows = (await session.scalars(stmt)).all()
    changed = 0
    for edge in rows:
        if edge.review_status in ("approved", "rejected"):
            await propose(
                session,
                edge,
                {
                    "action": "quarantine_legacy_rule_edge",
                    "reason": "legacy rules are not independent semantic verification",
                },
            )
        else:
            snapshot(session, edge, "before_legacy_quarantine")
            edge.review_status = "superseded"
            changed += 1
    await session.flush()
    return changed


async def extract_knowledge_for_country(
    session: AsyncSession,
    country_code: str,
    translate_sample_per_doc: int = 0,
    max_clauses_per_doc: int | None = None,
    concurrency: int = 8,
) -> dict:
    """Stage a replacement atomically. No failed/partial model batch can retire the old graph."""
    from sqlalchemy import text

    from app.knowledge.history import payload_hash
    from app.knowledge.models import KnowledgeAuditRecord

    await seed_glossary_and_review_points(session)
    cc = country_code.upper()
    lock_key = int.from_bytes(cc.encode(), "big")
    acquired = await session.scalar(
        text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": lock_key}
    )
    if not acquired:
        return {"status": "busy", "reason": "country_extraction_already_running"}
    transaction = await session.begin_nested()
    usage_token, usage = begin_usage()
    clock = time.perf_counter()
    try:
        result = await _extract_knowledge_for_country_impl(
            session, cc, translate_sample_per_doc, max_clauses_per_doc, concurrency
        )
        if result.get("status") == "failed":
            await transaction.rollback()
        else:
            await transaction.commit()
    except Exception as exc:
        await transaction.rollback()
        result = {
            "status": "failed",
            "reason": type(exc).__name__,
            "previous_graph_preserved": True,
            "semantic_edges_added": 0,
        }
    finally:
        end_usage(usage_token)
    result["duration_ms"] = round((time.perf_counter() - clock) * 1000)
    result["extraction_thinking"] = get_settings().llm_kg_extraction_thinking
    result["model_usage"] = usage | {
        "calls_without_reported_usage": usage["requests"] - usage["responses_with_usage"]
    }
    for cache in result.pop("_clause_cache_records", []):
        session.add(
            KnowledgeAuditRecord(
                target_type="clauses",
                target_id=uuid.UUID(cache["clause_id"]),
                action="kg_clause_result",
                payload=cache,
                content_hash=payload_hash(cache),
            )
        )
    for record in result.pop("_candidate_evaluations", []):
        session.add(
            KnowledgeAuditRecord(
                target_type="clauses",
                target_id=uuid.UUID(record["clause_id"]),
                action="kg_candidate_evaluation",
                payload=record,
                content_hash=payload_hash(record),
            )
        )
    result["batch_id"] = str(uuid.uuid4())
    session.add(
        KnowledgeAuditRecord(
            target_type="kg_country",
            target_id=uuid.uuid5(uuid.NAMESPACE_URL, "cyshuhe:kg:" + cc),
            action="graph_rebuild_result",
            payload={"country_code": cc, **result},
            content_hash=payload_hash({"country_code": cc, **result}),
        )
    )
    await session.commit()
    return result


async def _extract_knowledge_for_country_impl(
    session: AsyncSession,
    country_code: str,
    translate_sample_per_doc: int = 6,
    max_clauses_per_doc: int | None = None,
    concurrency: int = 8,
) -> dict[str, int]:
    """对指定国家已解析的官方法规执行 S2 六要素+十二关系知识图谱抽取:
    1. 旧 kg-rule-extractor-v1 边全部标为 superseded;
    2. 对每份文档执行相关性判定(classify_document_relevance),out_of_scope 文件跳过图谱抽取;
    3. 对 relevant 文件建立结构边(REGULATOR->INSTRUMENT, REGULATOR->COUNTRY);
    4. 对条款调用大模型抽取六要素实体与十二类关系,经类型约束 + 逐字校验 + 语义裁判后入图谱。
    """
    cc = country_code.upper()
    from app.ingestion.safety import holds_for_documents

    superseded_count = await supersede_legacy_rule_edges(session, cc)
    superseded_unreviewed = (
        0  # retire only successfully processed evidence, after all calls complete
    )

    country_zh, country_orig = COUNTRY_NAMES.get(cc, (cc, cc))
    country_node = await _upsert_node(
        session,
        country_code=cc,
        node_type=NodeType.COUNTRY,
        node_key=f"country:{cc}",
        name_zh=country_zh,
        name_orig=country_orig,
        summary=f"{country_zh}跨境数据与智能网联汽车合规司法辖区",
        review_status=GraphReviewStatus.PENDING_REVIEW.value,
    )

    docs = list(
        (
            await session.execute(
                select(Document).where(Document.country_code == cc).order_by(Document.discovered_at)
            )
        )
        .scalars()
        .all()
    )

    terms = list(
        (
            await session.execute(
                select(GlossaryTerm).where(
                    (GlossaryTerm.country_code == cc) | (GlossaryTerm.country_code.is_(None))
                )
            )
        )
        .scalars()
        .all()
    )

    from app.sources.models import SourceSite

    translated_count = 0
    doc_counts: dict[str, int] = {}
    inst_title_index: list[tuple[str, KgNode, Document]] = []
    relevant_docs_info: list[
        tuple[Document, DocumentVersion, list[Clause], KgNode, KgNode, str]
    ] = []
    doc_tier: dict[uuid.UUID, str] = {}
    inst_node_by_doc: dict[uuid.UUID, KgNode] = {}
    inst_by_title: dict[str, KgNode] = {}

    held = await holds_for_documents(session, [doc.id for doc in docs])
    for doc in docs:
        if doc.id in held:
            doc_counts["safety_hold"] = doc_counts.get("safety_hold", 0) + 1
            continue
        ver = (
            await session.execute(
                select(DocumentVersion)
                .where(DocumentVersion.document_id == doc.id, DocumentVersion.state == "parsed")
                .order_by(DocumentVersion.version_no.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if ver is None:
            continue

        clauses = list(
            (
                await session.execute(
                    select(Clause).where(Clause.version_id == ver.id).order_by(Clause.ordinal)
                )
            )
            .scalars()
            .all()
        )
        if not clauses:
            continue

        prior_scope = (doc.relevance, doc.relevance_note, doc.relevance_source)
        rel_status, rel_note = await classify_document_relevance(doc, clauses[:3])
        if prior_scope != (doc.relevance, doc.relevance_note, doc.relevance_source):
            from app.knowledge.history import propose

            proposed_scope = dict(
                relevance=doc.relevance,
                relevance_note=doc.relevance_note,
                relevance_source=doc.relevance_source,
            )
            doc.relevance, doc.relevance_note, doc.relevance_source = prior_scope
            await propose(session, doc, proposed_scope)
        if rel_status == "needs_review":
            return {
                "status": "failed",
                "reason": "document_relevance_unavailable",
                "document_id": str(doc.id),
                "previous_graph_preserved": True,
                "semantic_edges_added": 0,
            }
        in_extraction_scope = rel_status in ("relevant", "related")
        doc_counts[rel_status] = doc_counts.get(rel_status, 0) + 1
        if not in_extraction_scope:
            # 范围外文件保留图谱节点(带范围标签),但清除此前可能由它产生的未复核语义边
            clause_ids_of_doc = [c.id for c in clauses]
            await session.execute(
                update(KgEdge)
                .where(
                    KgEdge.evidence_clause_id.in_(clause_ids_of_doc),
                    KgEdge.is_structural.is_(False),
                    KgEdge.review_status == GraphReviewStatus.PENDING_REVIEW.value,
                )
                .values(review_status=GraphReviewStatus.SUPERSEDED.value)
            )

        first_clause = clauses[0]
        site = await session.get(SourceSite, doc.site_id)
        publisher_name = site.publisher if site else f"{country_zh}官方发布机构"
        reg_slug = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "_", publisher_name.lower()).strip("_")[:48]
        reg_key = f"reg:{reg_slug}"
        reg_node = await _upsert_node(
            session,
            country_code=cc,
            node_type=NodeType.REGULATOR,
            node_key=reg_key,
            name_zh=publisher_name,
            name_orig=publisher_name,
            summary=f"{country_zh}法规发布与监管主管机构",
            evidence_clause_id=first_clause.id,
            review_status=GraphReviewStatus.PENDING_REVIEW.value,
        )

        inst_key = f"inst:{str(doc.id)[:12]}"
        inst_node = await _upsert_node(
            session,
            country_code=cc,
            node_type=NodeType.INSTRUMENT,
            node_key=inst_key,
            name_zh=doc.title[:255],
            name_orig=doc.title[:255],
            summary=f"类型: {doc.document_type} | 语种: {doc.language}",
            attributes={
                "document_type": doc.document_type,
                "source_url": doc.source_url,
                "version_no": ver.version_no,
                "legal_status": ver.legal_status,
                "scope": rel_status,
                "scope_note": rel_note,
                "scope_source": doc.relevance_source,
            },
            document_id=doc.id,
            evidence_clause_id=first_clause.id,
            review_status=GraphReviewStatus.PENDING_REVIEW.value,
        )
        inst_by_title[doc.title.lower().strip()] = inst_node
        inst_node_by_doc[doc.id] = inst_node
        doc_tier[doc.id] = rel_status
        inst_title_index.append((_normalize_instrument_title(doc.title), inst_node, doc))

        # 结构边 1: 监管机构 PUBLISHES 法规文件 (is_structural=True)
        await _upsert_edge(
            session,
            country_code=cc,
            source_node=reg_node,
            target_node=inst_node,
            relation_type=RelationType.PUBLISHES,
            evidence_clause=first_clause,
            evidence_quote=first_clause.original_text[:200].strip(),
            extractor_model=STRUCTURAL_EXTRACTOR_MODEL,
            confidence=1.0,
            judge_verdict="structural",
            judge_reason="基于官方站点与法规元数据确立的机构发布结构关系",
            is_structural=True,
            review_status=GraphReviewStatus.PENDING_REVIEW.value,
        )
        # 结构边 2: 监管机构 SUPERVISES 国家司法辖区 (is_structural=True)
        await _upsert_edge(
            session,
            country_code=cc,
            source_node=reg_node,
            target_node=country_node,
            relation_type=RelationType.SUPERVISES,
            evidence_clause=first_clause,
            evidence_quote=first_clause.original_text[:200].strip(),
            extractor_model=STRUCTURAL_EXTRACTOR_MODEL,
            confidence=1.0,
            judge_verdict="structural",
            judge_reason="基于官方站点与司法辖区确立的监管结构关系",
            is_structural=True,
            review_status=GraphReviewStatus.PENDING_REVIEW.value,
        )

        # 对前 N 条核心条款补齐受控翻译(范围外文件不消耗模型额度)
        for c in clauses[:translate_sample_per_doc] if in_extraction_scope else []:
            existing_tr = (
                await session.execute(
                    select(ClauseTranslation.id).where(
                        ClauseTranslation.clause_id == c.id,
                        ClauseTranslation.review_status != TranslationReviewStatus.SUPERSEDED.value,
                        ClauseTranslation.engine == "llm",
                    )
                )
            ).first()
            if not existing_tr:
                await translate_clause(session, c, cc, terms)
                translated_count += 1

        if in_extraction_scope:
            relevant_docs_info.append((doc, ver, clauses, reg_node, inst_node, publisher_name))

    # 并发调用大模型抽取各相关文档的候选条款并执行语义裁判(核心文件预算大,相关文件预算小)
    sem = asyncio.Semaphore(max(1, concurrency))
    extraction_tasks: list[tuple[Document, Clause, KgNode, KgNode, str]] = []
    for doc, _ver, clauses, reg_node, inst_node, pub_name in relevant_docs_info:
        cand_clauses = (
            clauses
            if max_clauses_per_doc is None
            else _select_candidate_clauses(clauses, max_clauses=max_clauses_per_doc)
        )
        for c in cand_clauses:
            extraction_tasks.append((doc, c, reg_node, inst_node, pub_name))

    evaluation_records = []
    failure_records = []
    from app.ingestion.archive import ArchiveStore
    from app.knowledge.history import payload_hash
    from app.knowledge.models import KnowledgeAuditRecord

    def cache_key(doc, clause, publisher):
        return payload_hash(
            dict(
                contract="kg-rebuild-v3",
                source_hash=clause.text_hash,
                version_id=str(clause.version_id),
                title=doc.title,
                publisher=publisher,
                scope=(doc.relevance, doc.relevance_source),
                model=get_settings().llm_model,
                base_url=get_settings().llm_base_url,
                extractor_prompt=EXTRACTOR_PROMPT_VERSION,
                judge_prompt=JUDGE_PROMPT_VERSION,
                extraction_thinking=get_settings().llm_kg_extraction_thinking,
            )
        )

    keys = {
        clause.id: cache_key(doc, clause, publisher)
        for doc, clause, _reg, _inst, publisher in extraction_tasks
    }
    cached, cache_records, completed, cache_hits = {}, [], 0, 0
    if keys:
        for record in (
            await session.scalars(
                select(KnowledgeAuditRecord)
                .where(
                    KnowledgeAuditRecord.target_id.in_(keys),
                    KnowledgeAuditRecord.action == "kg_clause_result",
                )
                .order_by(KnowledgeAuditRecord.created_at.desc())
            )
        ).all():
            if record.payload.get("cache_key") == keys[record.target_id]:
                cached.setdefault(record.target_id, record.payload)

    async def _extract_and_judge_one(
        doc: Document, clause: Clause, reg_node: KgNode, inst_node: KgNode, pub_name: str
    ) -> (
        tuple[
            Document,
            Clause,
            KgNode,
            KgNode,
            str,
            ClauseExtractionPayload,
            list[
                tuple[
                    ExtractedRelation,
                    CandidateEvaluationResult,
                    NodeType,
                    str,
                    str,
                    NodeType,
                    str,
                    str,
                ]
            ],
        ]
        | None
    ):
        nonlocal completed, cache_hits
        async with sem:
            from app.knowledge.translator import split_clause_into_chunks

            if ArchiveStore.sha256(clause.original_text.encode("utf-8")) != clause.text_hash:
                failure_records.append(
                    dict(
                        clause_id=str(clause.id),
                        step="source_integrity",
                        reason="text_hash_mismatch",
                    )
                )
                return None
            if prior := cached.get(clause.id):
                try:
                    restored = []
                    for item in prior["validated_relations"]:
                        rel = ExtractedRelation.model_validate(item["relation"])
                        evaluation = CandidateEvaluationResult.model_validate(item["evaluation"])
                        st, dt = NodeType(item["source_type"]), NodeType(item["target_type"])
                        ok, _ = validate_relation_type_constraints(
                            relation_type=rel.type,
                            source_type=st,
                            target_type=dt,
                            evidence_quote=evaluation.aligned_quote or "",
                            source_name=item["source_name"],
                            target_name=item["target_name"],
                            document_title=doc.title,
                        )
                        if not (
                            ok
                            and evaluation.admitted
                            and evaluation.judge_verdict == "yes"
                            and evaluation.judge_model
                            and evaluation.aligned_quote
                            and evaluation.aligned_quote in clause.original_text
                        ):
                            raise ValueError("cached evidence/judge invalid")
                        restored.append(
                            (
                                rel,
                                evaluation,
                                st,
                                item["source_name"],
                                item["source_orig"],
                                dt,
                                item["target_name"],
                                item["target_orig"],
                            )
                        )
                    parsed = ClauseExtractionPayload.model_validate(prior["extraction_payload"])
                    cache_hits += 1
                    completed += 1
                    return (
                        doc,
                        clause,
                        reg_node,
                        inst_node,
                        prior["extractor_model"],
                        parsed,
                        restored,
                    )
                except (ValueError, KeyError, ValidationError):
                    pass  # Recompute invalid caches; never downgrade the verification gate.

            # Long source clauses stay intact. Every chunk is evaluated against its real source ID.
            entities, relations, models = [], [], []
            for chunk in split_clause_into_chunks(clause.original_text, max_chars=2400):
                res = await _call_llm_extract_clause(
                    clause_text=chunk,
                    clause_number=clause.clause_number,
                    document_title=doc.title,
                    publisher_name=pub_name,
                    country_code=cc,
                )
                if res is None:
                    failure_records.append(
                        {
                            "clause_id": str(clause.id),
                            "step": "extraction",
                            "reason": "model_unavailable_or_invalid_schema",
                        }
                    )
                    return None
                chunk_payload, chunk_model = res
                entities.extend(chunk_payload.entities)
                relations.extend(chunk_payload.relations)
                models.append(chunk_model)
            payload = ClauseExtractionPayload(entities=entities, relations=relations)
            model_name = ",".join(dict.fromkeys(models))[:128]

            # 建立本条款局部实体索引
            entity_lookup: dict[str, ExtractedEntity] = {}
            for ent in payload.entities:
                entity_lookup[ent.name_zh.strip().lower()] = ent
                if ent.name_orig:
                    entity_lookup[ent.name_orig.strip().lower()] = ent

            validated_rels: list[
                tuple[
                    ExtractedRelation,
                    CandidateEvaluationResult,
                    NodeType,
                    str,
                    str,
                    NodeType,
                    str,
                    str,
                ]
            ] = []

            top_rels = sorted(payload.relations, key=lambda r: r.confidence, reverse=True)
            for rel in top_rels:
                src_raw = rel.source.strip()
                dst_raw = rel.target.strip()

                # 解析源节点类型与名称
                src_ent = entity_lookup.get(src_raw.lower())
                if src_raw.lower() == doc.title.lower().strip() or src_raw in (
                    "本法",
                    "本法规",
                    "本条例",
                    "本指南",
                ):
                    src_type = NodeType.INSTRUMENT
                    src_zh = doc.title
                    src_orig = doc.title
                elif src_raw.lower() == pub_name.lower().strip():
                    src_type = NodeType.REGULATOR
                    src_zh = pub_name
                    src_orig = pub_name
                elif src_ent is not None:
                    src_type = src_ent.type
                    src_zh = src_ent.name_zh
                    src_orig = src_ent.name_orig or src_ent.name_zh
                else:
                    continue  # An unresolved source cannot be guessed to mean this law.

                # 解析目标节点类型与名称
                dst_ent = entity_lookup.get(dst_raw.lower())
                if dst_ent is not None:
                    dst_type = dst_ent.type
                    dst_zh = dst_ent.name_zh
                    dst_orig = dst_ent.name_orig or dst_ent.name_zh
                elif rel.type in (RelationType.AMENDS, RelationType.REPEALS):
                    dst_type = NodeType.INSTRUMENT
                    dst_zh = dst_raw
                    dst_orig = dst_raw
                elif rel.type == RelationType.PENALIZES:
                    dst_type = NodeType.PENALTY
                    dst_zh = dst_raw
                    dst_orig = dst_raw
                elif rel.type in (RelationType.REQUIRES, RelationType.REQUIRES_ADMISSION):
                    dst_type = NodeType.OBLIGATION
                    dst_zh = dst_raw
                    dst_orig = dst_raw
                else:
                    dst_type = NodeType.DATA_CATEGORY
                    dst_zh = dst_raw
                    dst_orig = dst_raw

                eval_res = await evaluate_kg_candidate(
                    relation_type=rel.type,
                    source_type=src_type,
                    source_name=src_zh,
                    target_type=dst_type,
                    target_name=dst_zh,
                    evidence_quote=rel.evidence_quote,
                    clause_original_text=clause.original_text,
                    document_title=doc.title,
                    confidence=rel.confidence,
                    use_llm_judge=True,
                )
                evaluation_records.append(
                    {
                        "clause_id": str(clause.id),
                        "source_hash": clause.text_hash,
                        "model": model_name,
                        "relation": rel.type.value,
                        "source_name": src_zh,
                        "target_name": dst_zh,
                        "evidence_quote": rel.evidence_quote,
                        "evaluation": eval_res.model_dump(),
                    }
                )
                if eval_res.stage == "semantic_judge_unavailable":
                    failure_records.append(
                        {
                            "clause_id": str(clause.id),
                            "step": "semantic_judge",
                            "reason": "independent_judge_unavailable",
                        }
                    )
                    return None
                if eval_res.admitted and eval_res.aligned_quote:
                    validated_rels.append(
                        (rel, eval_res, src_type, src_zh, src_orig, dst_type, dst_zh, dst_orig)
                    )

            cache_records.append(
                dict(
                    clause_id=str(clause.id),
                    source_hash=clause.text_hash,
                    cache_key=keys[clause.id],
                    extractor_model=model_name,
                    extraction_payload=payload.model_dump(mode="json"),
                    validated_relations=[
                        dict(
                            relation=rel.model_dump(mode="json"),
                            evaluation=ev.model_dump(mode="json"),
                            source_type=st.value,
                            source_name=sz,
                            source_orig=so,
                            target_type=dt.value,
                            target_name=dz,
                            target_orig=do,
                        )
                        for rel, ev, st, sz, so, dt, dz, do in validated_rels
                    ],
                )
            )
            completed += 1
            if completed % 20 == 0 or completed == len(extraction_tasks):
                logger.warning(
                    "KG %s: %s/%s clauses completed, cached=%s",
                    cc,
                    completed,
                    len(extraction_tasks),
                    cache_hits,
                )
            return (doc, clause, reg_node, inst_node, model_name, payload, validated_rels)

    batch_results = await asyncio.gather(
        *[_extract_and_judge_one(d, c, rn, in_n, pn) for d, c, rn, in_n, pn in extraction_tasks]
    )

    if any(item is None for item in batch_results):
        return {
            "status": "failed",
            "reason": "extractor_or_judge_unavailable",
            "failures": failure_records,
            "_candidate_evaluations": evaluation_records,
            "_clause_cache_records": cache_records,
            "cache_hits": cache_hits,
            "attempted_clauses": len(extraction_tasks),
            "failed_clauses": sum(item is None for item in batch_results),
            "previous_graph_preserved": True,
            "semantic_edges_added": 0,
        }

    # All requested extraction/judge calls completed. Retire only pending edges in this scope.
    processed_ids = [clause.id for _doc, clause, _reg, _inst, _pub in extraction_tasks]
    prior = (
        (
            await session.scalars(
                select(KgEdge).where(
                    KgEdge.country_code == cc,
                    KgEdge.evidence_clause_id.in_(processed_ids),
                    KgEdge.review_status == "pending_review",
                    KgEdge.is_structural.is_(False),
                )
            )
        ).all()
        if processed_ids
        else []
    )
    from app.knowledge.history import snapshot

    for edge in prior:
        snapshot(session, edge, "before_successful_graph_replacement")
        edge.review_status = "superseded"
    superseded_unreviewed = len(prior)

    semantic_edges_added = 0
    seen_semantic_ids = set(
        (
            await session.scalars(
                select(KgEdge.id).where(KgEdge.country_code == cc, KgEdge.is_structural.is_(False))
            )
        ).all()
    )
    for item in batch_results:
        if item is None:
            continue
        doc, clause, reg_node, inst_node, model_name, payload, validated_rels = item
        if not validated_rels:
            continue

        # 仅写入与通过校验的语义边关联的实体节点(避免产生孤立垃圾节点)
        local_nodes: dict[tuple[NodeType, str], KgNode] = {}
        used_entities = {(st, sz.lower()) for _, _, st, sz, _, _, _, _ in validated_rels}
        used_entities |= {(dt, dz.lower()) for _, _, _, _, _, dt, dz, _ in validated_rels}
        for ent in payload.entities:
            if (ent.type, ent.name_zh.strip().lower()) not in used_entities:
                continue
            if ent.type in (NodeType.DATA_CATEGORY, NodeType.OBLIGATION, NodeType.PENALTY):
                nkey, czh, corig = normalize_entity_node_key(ent.type, ent.name_zh, ent.name_orig)
                node_obj = await _upsert_node(
                    session,
                    country_code=cc,
                    node_type=ent.type,
                    node_key=nkey,
                    name_zh=czh,
                    name_orig=corig,
                    summary="机器抽取实体，待人工核对；请对照关联条款证据。",
                    attributes={
                        "model_definition_proposal": ent.definition,
                        "definition_status": "pending_review",
                    },
                    evidence_clause_id=clause.id,
                    review_status=GraphReviewStatus.PENDING_REVIEW.value,
                )
                local_nodes[(ent.type, ent.name_zh.strip().lower())] = node_obj

        for rel, eval_res, src_type, src_zh, src_orig, dst_type, dst_zh, dst_orig in validated_rels:
            # 确定 source KgNode
            if src_type == NodeType.INSTRUMENT:
                src_node = inst_by_title.get(
                    src_orig.lower().strip()
                ) or _resolve_instrument_target(src_orig, inst_title_index)
                if src_node is None:
                    continue
            elif (
                src_type == NodeType.REGULATOR
                and src_orig.lower().strip()
                == (reg_node.name_orig or reg_node.name_zh).lower().strip()
            ):
                src_node = reg_node
            else:
                src_node = local_nodes.get((src_type, src_zh.strip().lower()))
                if src_node is None:
                    nkey, czh, corig = normalize_entity_node_key(src_type, src_zh, src_orig)
                    src_node = await _upsert_node(
                        session,
                        country_code=cc,
                        node_type=src_type,
                        node_key=nkey,
                        name_zh=czh,
                        name_orig=corig,
                        evidence_clause_id=clause.id,
                        review_status=GraphReviewStatus.PENDING_REVIEW.value,
                    )

            # 确定 target KgNode
            if dst_type == NodeType.INSTRUMENT:
                # 仅关联本国已入库、标题能确定匹配的法规节点;匹配不到就丢弃,不做任意兜底
                dst_node = inst_by_title.get(dst_zh.lower().strip())
                if dst_node is None:
                    dst_node = _resolve_instrument_target(
                        dst_zh, inst_title_index, exclude_id=src_node.id
                    )
                if dst_node is None or dst_node.id == src_node.id:
                    continue
            elif dst_type == NodeType.REGULATOR:
                key, name_zh, name_orig = normalize_entity_node_key(dst_type, dst_zh, dst_orig)
                dst_node = await _upsert_node(
                    session,
                    country_code=cc,
                    node_type=dst_type,
                    node_key=key,
                    name_zh=name_zh,
                    name_orig=name_orig,
                    evidence_clause_id=clause.id,
                    review_status="pending_review",
                )
            elif dst_type == NodeType.COUNTRY:
                dst_node = country_node
            else:
                dst_node = local_nodes.get((dst_type, dst_zh.strip().lower()))
                if dst_node is None:
                    nkey, czh, corig = normalize_entity_node_key(dst_type, dst_zh, dst_orig)
                    dst_node = await _upsert_node(
                        session,
                        country_code=cc,
                        node_type=dst_type,
                        node_key=nkey,
                        name_zh=czh,
                        name_orig=corig,
                        evidence_clause_id=clause.id,
                        review_status=GraphReviewStatus.PENDING_REVIEW.value,
                    )

            edge_obj = await _upsert_edge(
                session,
                country_code=cc,
                source_node=src_node,
                target_node=dst_node,
                relation_type=rel.type,
                evidence_clause=clause,
                evidence_quote=eval_res.aligned_quote or rel.evidence_quote,
                extractor_model=model_name,
                confidence=eval_res.confidence,
                judge_verdict=eval_res.judge_verdict,
                judge_reason=eval_res.judge_reason,
                is_structural=False,
                review_status=GraphReviewStatus.PENDING_REVIEW.value,
            )
            if edge_obj is not None and edge_obj.id not in seen_semantic_ids:
                semantic_edges_added += 1
                seen_semantic_ids.add(edge_obj.id)

    # 修正案 -> 被修正主法: 由标题层级确定性推断的结构边(待核对,不冒充条款证据)
    inferred_amend_edges = await _add_title_inferred_amend_edges(
        session, cc, inst_title_index, inst_node_by_doc
    )

    # 将没有任何活跃边连接的旧孤立节点标记为 superseded,确保活跃图谱干净
    from app.knowledge.graph_evidence import active_edge_filters

    active_edges = list(
        (
            await session.execute(
                select(KgEdge).where(
                    KgEdge.country_code == cc,
                    *active_edge_filters(),
                )
            )
        )
        .scalars()
        .all()
    )
    connected_ids = {e.source_node_id for e in active_edges} | {
        e.target_node_id for e in active_edges
    }
    all_country_nodes = list(
        (
            await session.execute(
                select(KgNode).where(
                    KgNode.country_code == cc,
                    KgNode.review_status != GraphReviewStatus.SUPERSEDED.value,
                )
            )
        )
        .scalars()
        .all()
    )
    for n in all_country_nodes:
        if (
            n.id not in connected_ids
            and n.node_type != NodeType.COUNTRY.value
            and n.review_status == "pending_review"
        ):
            snapshot(session, n, "before_isolated_node_retirement")
            n.review_status = GraphReviewStatus.SUPERSEDED.value

    await session.flush()

    active_nodes_after = len(
        (
            await session.execute(
                select(KgNode.id).where(
                    KgNode.country_code == cc,
                    KgNode.review_status != GraphReviewStatus.SUPERSEDED.value,
                )
            )
        )
        .scalars()
        .all()
    )
    active_edges_after = len(active_edges)
    return {
        "status": "completed",
        "_candidate_evaluations": evaluation_records,
        "_clause_cache_records": cache_records,
        "cache_hits": cache_hits,
        "sampled": max_clauses_per_doc is not None,
        "attempted_clauses": len(extraction_tasks),
        "failed_clauses": 0,
        "nodes_total": active_nodes_after,
        "edges_total": active_edges_after,
        "semantic_edges_added": semantic_edges_added,
        "title_inferred_amend_edges": inferred_amend_edges,
        "superseded_legacy_edges": superseded_count,
        "superseded_unreviewed_edges": superseded_unreviewed,
        "clauses_translated": translated_count,
        "docs_relevant": doc_counts.get("relevant", 0),
        "docs_related": doc_counts.get("related", 0),
        "docs_out_of_scope": doc_counts.get("out_of_scope", 0),
        "docs_safety_held": doc_counts.get("safety_hold", 0),
    }


async def handle_new_version_linkage(
    session: AsyncSession, new_version: DocumentVersion
) -> dict[str, int]:
    """版本更新联动:
    当某份 Document 产生新版本(version_no > 1)且已解析出条款时,
    对比上一版本条款哈希;对变更或删除的旧条款,将其关联的译文与图谱边标记为 stale(待重核),
    并对未变化的条款复用已复核译文。
    """
    if new_version.version_no <= 1:
        return {"stale_translations": 0, "stale_edges": 0, "reused_translations": 0}

    prev_version = (
        await session.execute(
            select(DocumentVersion)
            .where(
                DocumentVersion.document_id == new_version.document_id,
                DocumentVersion.version_no < new_version.version_no,
                DocumentVersion.state == "parsed",
            )
            .order_by(DocumentVersion.version_no.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if prev_version is None:
        return {"stale_translations": 0, "stale_edges": 0, "reused_translations": 0}

    old_clauses = list(
        (
            await session.execute(
                select(Clause).where(Clause.version_id == prev_version.id).order_by(Clause.ordinal)
            )
        )
        .scalars()
        .all()
    )
    new_clauses = list(
        (
            await session.execute(
                select(Clause).where(Clause.version_id == new_version.id).order_by(Clause.ordinal)
            )
        )
        .scalars()
        .all()
    )
    new_by_hash = {c.text_hash: c for c in new_clauses}

    stale_tr = 0
    stale_edges = 0
    reused_tr = 0

    for old_c in old_clauses:
        tr = (
            await session.execute(
                select(ClauseTranslation)
                .where(
                    ClauseTranslation.clause_id == old_c.id,
                    ClauseTranslation.is_current.is_(True),
                    ClauseTranslation.review_status != TranslationReviewStatus.SUPERSEDED.value,
                )
                .order_by(ClauseTranslation.updated_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        edges = list(
            (
                await session.execute(
                    select(KgEdge).where(
                        KgEdge.evidence_clause_id == old_c.id,
                        KgEdge.review_status != GraphReviewStatus.SUPERSEDED.value,
                    )
                )
            )
            .scalars()
            .all()
        )
        matched_new = new_by_hash.get(old_c.text_hash)
        if matched_new is not None:
            if tr is not None:
                exists_new_tr = (
                    await session.execute(
                        select(ClauseTranslation.id).where(
                            ClauseTranslation.clause_id == matched_new.id,
                            ClauseTranslation.review_status
                            != TranslationReviewStatus.SUPERSEDED.value,
                        )
                    )
                ).first()
                if not exists_new_tr:
                    session.add(
                        ClauseTranslation(
                            clause_id=matched_new.id,
                            translated_text=tr.translated_text,
                            engine=tr.engine,
                            model_name=tr.model_name,
                            prompt_version=tr.prompt_version,
                            tokens_in=0,  # Reuse is not a new paid model call.
                            tokens_out=0,
                            duration_ms=0,
                            glossary_hits=tr.glossary_hits,
                            unmatched_terms=tr.unmatched_terms,
                            quality_check_passed=tr.quality_check_passed,
                            quality_issues=tr.quality_issues,
                            review_status=tr.review_status,
                            reviewer_id=tr.reviewer_id,
                            reviewer_note=tr.reviewer_note,
                            reviewed_at=tr.reviewed_at,
                            updated_at=datetime.now(UTC),
                        )
                    )
                    reused_tr += 1
        else:
            if tr is not None and tr.review_status != TranslationReviewStatus.STALE.value:
                from app.knowledge.history import snapshot

                snapshot(session, tr, "before_version_stale")
                tr.review_status = TranslationReviewStatus.STALE.value
                stale_tr += 1
            for e in edges:
                if e.review_status != GraphReviewStatus.STALE.value:
                    from app.knowledge.history import snapshot

                    snapshot(session, e, "before_version_stale")
                    e.review_status = GraphReviewStatus.STALE.value
                    stale_edges += 1

    await session.flush()
    return {
        "stale_translations": stale_tr,
        "stale_edges": stale_edges,
        "reused_translations": reused_tr,
    }
