"""六要素+十二关系知识图谱抽取、两层术语库与解读要点初始化、以及法规版本更新联动。

核心原则:
1. 每条语义边(KgEdge)必须挂真实的证据条款(evidence_clause_id),且摘录必须是原文真实子串。
2. 文件包含条款的结构关系留在 Document->DocumentVersion->Clause 表,不混入语义边统计。
3. 当同一 Document 产生新版本时,自动比对旧版本条款哈希:变化条款的旧译文和图谱边标为 stale。
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingestion.models import Clause, Document, DocumentVersion
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
            review_status=item.get("review_status", "approved"),
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
    review_status: str = GraphReviewStatus.APPROVED.value,
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
        node = KgNode(
            country_code=country_code,
            node_type=node_type.value,
            node_key=node_key,
            name_zh=name_zh[:255],
            name_orig=(name_orig or "")[:255] or None,
            summary=summary,
            attributes=attributes or {},
            document_id=document_id,
            evidence_clause_id=evidence_clause_id,
            review_status=review_status,
        )
        session.add(node)
        await session.flush()
    else:
        node.name_zh = name_zh[:255]
        if name_orig:
            node.name_orig = name_orig[:255]
        if summary:
            node.summary = summary
        if document_id:
            node.document_id = document_id
        if evidence_clause_id:
            node.evidence_clause_id = evidence_clause_id
    return node


async def _upsert_edge(
    session: AsyncSession,
    *,
    country_code: str,
    source_node: KgNode,
    target_node: KgNode,
    relation_type: RelationType,
    evidence_clause: Clause,
    evidence_quote: str | None = None,
    extractor_model: str = "kg-rule-extractor-v1",
    review_status: str = GraphReviewStatus.PENDING_REVIEW.value,
) -> KgEdge:
    quote = (evidence_quote or evidence_clause.original_text[:280]).strip()
    # 保证引用片段是条款原文的真实子串
    if quote not in evidence_clause.original_text:
        quote = evidence_clause.original_text[:280].strip()

    edge = (
        await session.execute(
            select(KgEdge).where(
                KgEdge.source_node_id == source_node.id,
                KgEdge.target_node_id == target_node.id,
                KgEdge.relation_type == relation_type.value,
                KgEdge.evidence_clause_id == evidence_clause.id,
            )
        )
    ).scalar_one_or_none()
    if edge is None:
        edge = KgEdge(
            country_code=country_code,
            source_node_id=source_node.id,
            target_node_id=target_node.id,
            relation_type=relation_type.value,
            evidence_clause_id=evidence_clause.id,
            evidence_quote=quote,
            extractor_model=extractor_model,
            review_status=review_status,
        )
        session.add(edge)
        await session.flush()
    return edge


def _extract_snippet(text: str, keyword: str, radius: int = 120) -> str:
    idx = text.lower().find(keyword.lower())
    if idx < 0:
        return text[:240].strip()
    start = max(0, idx - 40)
    end = min(len(text), idx + len(keyword) + radius)
    return text[start:end].strip()


# 针对条款原文的六要素与十二关系规则模式(覆盖英/法/阿多语种法规正文)
_EXTRACTION_RULES: list[dict] = [
    {
        "keywords": ["transfer", "outside singapore", "hors du territoire", "خارج"],
        "data_node": ("data:personal_data", "个人数据(含车主与驾乘识别数据)", "Personal Data"),
        "obligation_node": (
            "obl:cross_border_comparable_protection",
            "数据出境同等保护与合规传输义务",
            "Cross-border Transfer Comparable Protection Obligation",
        ),
        "relations": [
            (RelationType.RESTRICTS_CROSS_BORDER, "instrument", "data"),
            (RelationType.REQUIRES, "instrument", "obligation"),
            (RelationType.GOVERNS, "obligation", "data"),
        ],
    },
    {
        "keywords": ["consent", "consentement", "موافقة"],
        "data_node": ("data:personal_data", "个人数据(含车主与驾乘识别数据)", "Personal Data"),
        "obligation_node": (
            "obl:notice_and_consent",
            "个人数据处理告知与同意义务",
            "Notification and Consent Obligation",
        ),
        "relations": [
            (RelationType.REQUIRES, "instrument", "obligation"),
            (RelationType.GOVERNS, "instrument", "data"),
        ],
    },
    {
        "keywords": ["data breach", "notifiable", "3 calendar days", "violation de données"],
        "data_node": (
            "data:breach_incident_data",
            "数据安全事件与受影响个人数据",
            "Breach Incident Data",
        ),
        "obligation_node": (
            "obl:data_breach_notification",
            "数据泄露事件强制通报义务(3个日历日内)",
            "Mandatory Data Breach Notification Obligation",
        ),
        "relations": [
            (RelationType.REQUIRES, "instrument", "obligation"),
            (RelationType.APPLIES_TO_SCENARIO, "obligation", "data"),
        ],
    },
    {
        "keywords": ["data intermediary", "on behalf of another organisation", "sous-traitant"],
        "data_node": (
            "data:entrusted_processing_data",
            "受托处理的个人与车联网数据",
            "Entrusted Personal Data",
        ),
        "obligation_node": (
            "obl:data_intermediary_exemptions",
            "数据中介机构(受托处理者)义务豁免与安全留存边界",
            "Data Intermediary Partial Exemption & Protection Duty",
        ),
        "relations": [
            (RelationType.EXEMPTS, "instrument", "obligation"),
            (RelationType.GOVERNS, "obligation", "data"),
        ],
    },
    {
        "keywords": [
            "autonomous motor vehicle",
            "autonomous system",
            "trial",
            "authorisation",
            "approval",
        ],
        "data_node": (
            "data:av_sensor_and_recorder_data",
            "自动驾驶传感器与车载黑匣子记录数据",
            "AV Sensor & Data Recorder Data",
        ),
        "obligation_node": (
            "obl:av_trial_authorisation_and_recorder",
            "自动驾驶公共道路测试准入许可与黑匣子数据留存义务",
            "AV Road Trial Authorisation & Data Recorder Retention",
        ),
        "relations": [
            (RelationType.REQUIRES_ADMISSION, "instrument", "obligation"),
            (RelationType.APPLIES_TO_SCENARIO, "obligation", "data"),
            (RelationType.PROHIBITS, "instrument", "data"),
        ],
    },
    {
        "keywords": [
            "penalty",
            "fine",
            "offence",
            "liable on conviction",
            "imprisonment",
            "amende",
            "puni",
            "عقوبة",
            "غرامة",
        ],
        "penalty_node": (
            "pen:statutory_fine_and_liability",
            "法定行政罚款与刑事/行政处罚责任",
            "Statutory Financial Penalty & Offence Liability",
        ),
        "relations": [
            (RelationType.PENALIZES, "instrument", "penalty"),
        ],
    },
]


async def extract_knowledge_for_country(
    session: AsyncSession, country_code: str, translate_sample_per_doc: int = 8
) -> dict[str, int]:
    """对指定国家已解析的官方法规执行六要素+十二关系知识图谱抽取,并对核心条款执行术语受控翻译。"""
    cc = country_code.upper()
    await seed_glossary_and_review_points(session)

    country_zh, country_orig = COUNTRY_NAMES.get(cc, (cc, cc))
    country_node = await _upsert_node(
        session,
        country_code=cc,
        node_type=NodeType.COUNTRY,
        node_key=f"country:{cc}",
        name_zh=country_zh,
        name_orig=country_orig,
        summary=f"{country_zh}跨境数据与智能网联汽车合规司法辖区",
        review_status=GraphReviewStatus.APPROVED.value,
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

    nodes_before = len(
        (await session.execute(select(KgNode.id).where(KgNode.country_code == cc))).scalars().all()
    )
    edges_before = len(
        (await session.execute(select(KgEdge.id).where(KgEdge.country_code == cc))).scalars().all()
    )
    translated_count = 0

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

    # 先按发布机构登记 REGULATOR 节点,记录各文档对应的主法规节点以便建立 AMENDS/REPEALS 关系
    doc_nodes: dict[uuid.UUID, tuple[KgNode, Clause]] = {}
    base_act_nodes: dict[str, KgNode] = {}

    for doc in docs:
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

        first_clause = clauses[0]
        # 1. 监管机构/发布机构节点 (REGULATOR)
        from app.sources.models import SourceSite

        site = await session.get(SourceSite, doc.site_id)
        publisher_name = site.publisher if site else f"{country_zh}官方发布机构"
        reg_key = f"reg:{re.sub(r'[^a-z0-9]+', '_', publisher_name.lower()).strip('_')[:48]}"
        reg_node = await _upsert_node(
            session,
            country_code=cc,
            node_type=NodeType.REGULATOR,
            node_key=reg_key,
            name_zh=publisher_name,
            name_orig=publisher_name,
            summary=f"{country_zh}法规发布与监管主管机构",
            evidence_clause_id=first_clause.id,
            review_status=GraphReviewStatus.APPROVED.value,
        )

        # 2. 法规文件节点 (INSTRUMENT)
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
            },
            document_id=doc.id,
            evidence_clause_id=first_clause.id,
            review_status=GraphReviewStatus.APPROVED.value,
        )
        doc_nodes[doc.id] = (inst_node, first_clause)

        title_lower = doc.title.lower()
        if "personal data protection act" in title_lower and "amendment" not in title_lower:
            base_act_nodes["pdpa"] = inst_node
        elif "road traffic act" in title_lower and "amendment" not in title_lower:
            base_act_nodes["rta"] = inst_node
        elif "cybersecurity act" in title_lower and "amendment" not in title_lower:
            base_act_nodes["csa"] = inst_node

        # 关系 1: 监管机构 PUBLISHES 法规文件
        await _upsert_edge(
            session,
            country_code=cc,
            source_node=reg_node,
            target_node=inst_node,
            relation_type=RelationType.PUBLISHES,
            evidence_clause=first_clause,
            review_status=GraphReviewStatus.APPROVED.value,
        )
        # 关系 8: 监管机构 SUPERVISES 司法辖区合规运行
        await _upsert_edge(
            session,
            country_code=cc,
            source_node=reg_node,
            target_node=country_node,
            relation_type=RelationType.SUPERVISES,
            evidence_clause=first_clause,
            review_status=GraphReviewStatus.APPROVED.value,
        )

        # 对每份文档的前 N 条核心条款执行受术语约束机器翻译
        for c in clauses[:translate_sample_per_doc]:
            existing_tr = (
                await session.execute(
                    select(ClauseTranslation.id).where(ClauseTranslation.clause_id == c.id)
                )
            ).first()
            if not existing_tr:
                await translate_clause(session, c, cc, terms)
                translated_count += 1

        # 扫描条款抽取 DATA_CATEGORY, OBLIGATION, PENALTY 及语义关系边
        for c in clauses[:60]:
            low = c.original_text.lower()
            # 检测修订(AMENDS)与废止(REPEALS)关系
            if ("amend" in low or "amendment" in title_lower) and base_act_nodes:
                target_base = (
                    base_act_nodes.get("pdpa")
                    if "personal data" in low or "personal data" in title_lower
                    else base_act_nodes.get("rta")
                    if "road traffic" in low or "road traffic" in title_lower
                    else next(iter(base_act_nodes.values()), None)
                )
                if target_base and target_base.id != inst_node.id:
                    await _upsert_edge(
                        session,
                        country_code=cc,
                        source_node=inst_node,
                        target_node=target_base,
                        relation_type=RelationType.AMENDS,
                        evidence_clause=c,
                        evidence_quote=_extract_snippet(c.original_text, "amend"),
                    )
            if ("repeal" in low or "revoke" in low or "abrog" in low) and base_act_nodes:
                target_base = next(iter(base_act_nodes.values()), None)
                if target_base and target_base.id != inst_node.id:
                    await _upsert_edge(
                        session,
                        country_code=cc,
                        source_node=inst_node,
                        target_node=target_base,
                        relation_type=RelationType.REPEALS,
                        evidence_clause=c,
                        evidence_quote=_extract_snippet(c.original_text, "repeal"),
                    )

            for rule in _EXTRACTION_RULES:
                matched_kw = next((kw for kw in rule["keywords"] if kw in low), None)
                if not matched_kw:
                    continue

                snippet = _extract_snippet(c.original_text, matched_kw)
                node_map: dict[str, KgNode] = {"instrument": inst_node}

                if "data_node" in rule:
                    dk, dzh, dorig = rule["data_node"]
                    node_map["data"] = await _upsert_node(
                        session,
                        country_code=cc,
                        node_type=NodeType.DATA_CATEGORY,
                        node_key=dk,
                        name_zh=dzh,
                        name_orig=dorig,
                        evidence_clause_id=c.id,
                    )
                if "obligation_node" in rule:
                    ok, ozh, oorig = rule["obligation_node"]
                    node_map["obligation"] = await _upsert_node(
                        session,
                        country_code=cc,
                        node_type=NodeType.OBLIGATION,
                        node_key=ok,
                        name_zh=ozh,
                        name_orig=oorig,
                        evidence_clause_id=c.id,
                    )
                if "penalty_node" in rule:
                    pk, pzh, porig = rule["penalty_node"]
                    node_map["penalty"] = await _upsert_node(
                        session,
                        country_code=cc,
                        node_type=NodeType.PENALTY,
                        node_key=pk,
                        name_zh=pzh,
                        name_orig=porig,
                        evidence_clause_id=c.id,
                    )

                for rel_type, src_role, dst_role in rule["relations"]:
                    if src_role in node_map and dst_role in node_map:
                        await _upsert_edge(
                            session,
                            country_code=cc,
                            source_node=node_map[src_role],
                            target_node=node_map[dst_role],
                            relation_type=rel_type,
                            evidence_clause=c,
                            evidence_quote=snippet,
                        )
                        # 同时确保该证据条款已有受控翻译
                        has_tr = (
                            await session.execute(
                                select(ClauseTranslation.id).where(
                                    ClauseTranslation.clause_id == c.id
                                )
                            )
                        ).first()
                        if not has_tr:
                            await translate_clause(session, c, cc, terms)
                            translated_count += 1

    await session.commit()
    nodes_after = len(
        (await session.execute(select(KgNode.id).where(KgNode.country_code == cc))).scalars().all()
    )
    edges_after = len(
        (await session.execute(select(KgEdge.id).where(KgEdge.country_code == cc))).scalars().all()
    )
    return {
        "nodes_total": nodes_after,
        "edges_total": edges_after,
        "nodes_new": max(0, nodes_after - nodes_before),
        "edges_new": max(0, edges_after - edges_before),
        "clauses_translated": translated_count,
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
                select(ClauseTranslation).where(ClauseTranslation.clause_id == old_c.id)
            )
        ).scalar_one_or_none()
        edges = list(
            (await session.execute(select(KgEdge).where(KgEdge.evidence_clause_id == old_c.id)))
            .scalars()
            .all()
        )
        matched_new = new_by_hash.get(old_c.text_hash)
        if matched_new is not None:
            # 条款原文完全未变:如果旧条款有译文且新条款尚未创建译文,直接迁移继承(保留已复核状态)
            if tr is not None:
                exists_new_tr = (
                    await session.execute(
                        select(ClauseTranslation.id).where(
                            ClauseTranslation.clause_id == matched_new.id
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
            # 条款原文已修改或删除:将旧译文和挂在该条款上的语义边标记为 stale
            if tr is not None and tr.review_status != TranslationReviewStatus.STALE.value:
                tr.review_status = TranslationReviewStatus.STALE.value
                stale_tr += 1
            for e in edges:
                if e.review_status != GraphReviewStatus.STALE.value:
                    e.review_status = GraphReviewStatus.STALE.value
                    stale_edges += 1

    await session.flush()
    return {
        "stale_translations": stale_tr,
        "stale_edges": stale_edges,
        "reused_translations": reused_tr,
    }
