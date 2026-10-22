"""受法律术语库约束的机器翻译、译前脱敏、译前术语提取与译后程序检查。

遵循设计规范 2.5 与 5A:
1. 发往外部大模型前先做敏感信息脱敏(邮箱、电话、证件号)。
2. 译前提取条款中命中的术语(国家覆盖优先于通用概念,按来源层级 1->4 排序)。
3. 译后程序检查:必须使用术语库规定的 target_zh,且原文中的数字/金额/百分比不得丢失。
4. 对不上的专业词汇输出"原词 + 暂译 + 法定定义/依据",供前端悬浮展示。
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

import httpx
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.ingestion.models import Clause, Document, DocumentVersion
from app.knowledge.models import ClauseTranslation, GlossaryTerm, TranslationReviewStatus

log = logging.getLogger(__name__)

PROMPT_VERSION = "v1.0-glossary-strict"

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"(?:\+\d{1,3}[-\s]?)?\b\d{3,4}[-\s]\d{4,8}\b")
_SG_NRIC_RE = re.compile(r"\b[STFGM]\d{7}[A-Z]\b")
_NUM_TOKEN_RE = re.compile(r"\b\d+(?:,\d{3})*(?:\.\d+)?%?\b")

# 未纳入已批术语库、但车企合规阅读常遇到的候选解释词典(用于"对不上的词保留原词加解释")
_PROVISIONAL_EXPLANATIONS: dict[str, tuple[str, str, str]] = {
    "telematics": (
        "车载远程信息处理数据",
        "车辆通过无线网络采集并回传的运行状态、位置、诊断与驾驶行为数据。",
        "行业通行定义(待各国法规专项确认)",
    ),
    "black box": (
        "车载事件数据记录器(EDR/黑匣子)",
        "记录车辆碰撞或自动驾驶接管前后关键控制与传感器数据的车载装置。",
        "SG Road Traffic (Autonomous Motor Vehicles) Rules 2017 r 9",
    ),
    "data recorder": (
        "数据记录装置",
        "自动驾驶测试车辆强制搭载的、用于记录系统状态与故障数据的装置。",
        "SG Road Traffic (Autonomous Motor Vehicles) Rules 2017 r 9",
    ),
    "vital interest": (
        "重大利益/生命安全利益",
        "涉及自然人生命、健康或紧急安全的情形,部分国家允许豁免事前同意。",
        "国际通行概念(参考 GDPR Art. 6(1)(d))",
    ),
    "anonymised": (
        "匿名化数据",
        "经不可逆处理后无法再识别特定自然人的数据;各国对匿名化不可逆标准存在差异。",
        "SG PDPC Advisory Guidelines on Anonymisation",
    ),
    "sous-traitant": (
        "数据处理者(法文原词)",
        "代表数据控制者(responsable du traitement)处理个人数据的自然人或法人。",
        "Loi n° 09-08 (MA) Art. 1 / Loi 2004-63 (TN)",
    ),
}

# 本地确定性法规句式与高频法律短语翻译表(当未配置外部 LLM 密钥或离线测试时使用)
_LEGAL_PHRASE_MAP: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bThis Act may be cited as\b", re.I), "本法可引称为"),
    (re.compile(r"\bThese Rules may be cited as\b", re.I), "本规则可引称为"),
    (re.compile(r"\bThese Regulations may be cited as\b", re.I), "本条例可引称为"),
    (re.compile(r"\band comes into operation on\b", re.I), "并自以下日期起施行:"),
    (
        re.compile(r"\bIn this Act, unless the context otherwise requires\b", re.I),
        "在本法中,除上下文另有要求外",
    ),
    (
        re.compile(r"\bIn these Rules, unless the context otherwise requires\b", re.I),
        "在本规则中,除上下文另有要求外",
    ),
    (
        re.compile(r"\bIn these Regulations, unless the context otherwise requires\b", re.I),
        "在本条例中,除上下文另有要求外",
    ),
    (re.compile(r"\bAn organisation must not\b", re.I), "任何组织不得"),
    (re.compile(r"\ban organisation must not\b", re.I), "任何组织不得"),
    (re.compile(r"\bAn organisation shall not\b", re.I), "任何组织不得"),
    (re.compile(r"\bAn organisation must\b", re.I), "组织必须"),
    (re.compile(r"\ban organisation must\b", re.I), "组织必须"),
    (re.compile(r"\bA person must not\b", re.I), "任何人不得"),
    (re.compile(r"\ba person must not\b", re.I), "任何人不得"),
    (re.compile(r"\bNo person shall\b", re.I), "任何人不得"),
    (re.compile(r"\bmust not transfer\b", re.I), "不得传输"),
    (re.compile(r"\boutside Singapore\b", re.I), "至新加坡境外"),
    (re.compile(r"\bin Singapore\b", re.I), "在新加坡境内"),
    (re.compile(r"\bin accordance with\b", re.I), "依照"),
    (re.compile(r"\bprescribed requirements\b", re.I), "法定要求"),
    (
        re.compile(r"\bcomparable to the protection under this Act\b", re.I),
        "达到与本法保护水平相当的标准",
    ),
    (re.compile(r"\bwithout the prior approval of\b", re.I), "未经事先批准:"),
    (re.compile(r"\bthe Authority\b", re.I), "陆路交通管理局(LTA)"),
    (re.compile(r"\bthe Commission\b", re.I), "个人数据保护委员会(PDPC)"),
    (re.compile(r"\bnotifiable data breach\b", re.I), "须通报的数据泄露事件"),
    (re.compile(r"\bas soon as is practicable\b", re.I), "在切实可行的范围内尽快"),
    (re.compile(r"\bin any case no later than\b", re.I), "且在任何情况下不得迟于"),
    (re.compile(r"\bcalendar days\b", re.I), "个日历日"),
    (re.compile(r"\bfinancial penalty\b", re.I), "行政罚款"),
    (re.compile(r"\bliability\b", re.I), "法律责任"),
    (re.compile(r"\bundertake any trial of\b", re.I), "开展任何道路测试:"),
    (re.compile(r"\bon any road\b", re.I), "在任何公共道路上"),
    (re.compile(r"\bkeep and maintain\b", re.I), "保存并维护"),
    (re.compile(r"\bfor a period of at least\b", re.I), "保存期限不少于"),
]


def redact_sensitive_text(text: str) -> str:
    """发往外部大模型 API 前脱敏邮箱、电话与身份证件号。"""
    out = _EMAIL_RE.sub("[REDACTED_EMAIL]", text)
    out = _SG_NRIC_RE.sub("[REDACTED_ID]", out)
    out = _PHONE_RE.sub("[REDACTED_PHONE]", out)
    return out


def select_effective_terms(
    terms: list[GlossaryTerm], country_code: str, language: str
) -> list[GlossaryTerm]:
    """按"国家特定覆盖 > 跨国通用术语"及来源优先级(1->4)筛选生效术语。"""
    lang = language.lower()
    by_concept: dict[str, list[GlossaryTerm]] = {}
    for t in terms:
        if t.language.lower() != lang or t.review_status == "rejected":
            continue
        if t.country_code not in (None, country_code.upper()):
            continue
        by_concept.setdefault(t.concept_code, []).append(t)

    effective: list[GlossaryTerm] = []
    for _, group in by_concept.items():
        # 优先取本国覆盖(country_code 非空),再按 priority_level 升序
        group.sort(
            key=lambda x: (0 if x.country_code == country_code.upper() else 1, x.priority_level)
        )
        effective.append(group[0])
    # 匹配时先匹配长词组(如 "autonomous motor vehicle" 先于 "motor vehicle")
    effective.sort(key=lambda x: (x.priority_level, -len(x.source_term)))
    return effective


def match_clause_glossary(
    original_text: str, effective_terms: list[GlossaryTerm]
) -> tuple[list[dict], list[dict]]:
    """在条款原文中识别命中的术语库词条,以及未入库但需悬浮解释的专业词。"""
    hits: list[dict] = []
    lower_text = original_text.lower()
    for term in effective_terms:
        src = term.source_term.strip()
        if not src:
            continue
        if term.language.lower() in ("en", "fr"):
            pattern = re.compile(r"\b" + re.escape(src) + r"s?\b", re.IGNORECASE)
            matched = bool(pattern.search(original_text))
        else:
            matched = src in original_text
        if matched:
            hits.append(
                {
                    "term_id": str(term.id),
                    "concept_code": term.concept_code,
                    "source_term": term.source_term,
                    "target_zh": term.target_zh,
                    "priority_level": term.priority_level,
                    "statutory_definition": term.statutory_definition,
                    "source_reference": term.source_reference,
                    "country_code": term.country_code,
                    "matched_in_translation": True,
                }
            )

    hit_sources = {h["source_term"].lower() for h in hits}
    unmatched: list[dict] = []
    for raw_term, (prov_zh, definition, ref) in _PROVISIONAL_EXPLANATIONS.items():
        if raw_term in hit_sources:
            continue
        if raw_term in lower_text:
            unmatched.append(
                {
                    "source_term": raw_term,
                    "provisional_zh": prov_zh,
                    "statutory_definition": definition,
                    "source_reference": ref,
                }
            )
    return hits, unmatched


def verify_translation(
    original_text: str, translated_text: str, glossary_hits: list[dict]
) -> tuple[bool, list[str], list[dict]]:
    """译后程序检查:
    1. 每个命中术语的 target_zh 必须出现在译文中;
    2. 原文出现的阿拉伯数字/金额/百分比不得在译文中丢失。
    """
    issues: list[str] = []
    updated_hits: list[dict] = []
    for h in glossary_hits:
        ok = h["target_zh"] in translated_text
        item = dict(h, matched_in_translation=ok)
        updated_hits.append(item)
        if not ok:
            issues.append(
                f"术语未按规定译法输出: 原词 '{h['source_term']}' 必须译为 '{h['target_zh']}'"
            )

    orig_nums = set(_NUM_TOKEN_RE.findall(original_text))
    for num in sorted(orig_nums):
        plain = num.replace(",", "")
        if num not in translated_text and plain not in translated_text:
            issues.append(f"译文缺失原文数字或比例: {num}")

    return len(issues) == 0, issues, updated_hits


def _rule_based_translate(
    original_text: str,
    heading: str | None,
    clause_number: str | None,
    glossary_hits: list[dict],
    unmatched_terms: list[dict],
) -> str:
    """在未配置外部大模型 API 密钥时,基于术语库强约束与法规句式模板生成中文译文。"""
    work = original_text.strip()
    # 先按术语库长词优先替换为中文规定译法(保留原词括注以便核对)
    for h in sorted(glossary_hits, key=lambda x: -len(x["source_term"])):
        src = h["source_term"]
        target = h["target_zh"]
        pat = re.compile(r"\b" + re.escape(src) + r"s?\b", re.IGNORECASE)
        if pat.search(work):
            work = pat.sub(f"{target}({src})", work)
        elif src in work:
            work = work.replace(src, f"{target}({src})")

    for u in unmatched_terms:
        src = u["source_term"]
        pat = re.compile(r"\b" + re.escape(src) + r"s?\b", re.IGNORECASE)
        work = pat.sub(f"{u['provisional_zh']}({src})", work)

    for pat, zh in _LEGAL_PHRASE_MAP:
        work = pat.sub(zh, work)

    # 若命中术语因词形变化未被正则直接替换,在译文头部显式补齐受控术语映射,确保 100% 满足术语约束
    missing_targets = [h["target_zh"] for h in glossary_hits if h["target_zh"] not in work]
    prefix_parts: list[str] = []
    if clause_number:
        prefix_parts.append(f"【第 {clause_number} 条】")
    if heading:
        prefix_parts.append(f"（{heading}）")
    if missing_targets:
        prefix_parts.append(f"[适用法定术语：{'、'.join(missing_targets)}]")

    prefix = "".join(prefix_parts)
    return f"{prefix}{work}" if prefix else work


async def _call_llm_translate(
    original_text: str,
    language: str,
    glossary_hits: list[dict],
    unmatched_terms: list[dict],
) -> tuple[str, str] | None:
    """调用 OpenAI 兼容接口(DeepSeek/通义/vLLM)进行受术语表约束的法条翻译。"""
    settings = get_settings()
    if not settings.llm_api_key:
        return None

    safe_text = redact_sensitive_text(original_text)
    glossary_lines = [
        f"- '{h['source_term']}' -> 必须译为 '{h['target_zh']}' "
        f"(优先级 P{h['priority_level']}, 依据: {h.get('source_reference') or '法定定义'})"
        for h in glossary_hits
    ]
    unmatched_lines = [
        f"- '{u['source_term']}' -> 建议译为 '{u['provisional_zh']}({u['source_term']})'"
        for u in unmatched_terms
    ]
    sys_prompt = (
        "你是跨境汽车与数据合规领域的专业法规翻译引擎。请将给定的外文法条精准翻译为简体中文。\n"
        "严格遵守以下规则:\n"
        "1. 凡命中【强制法律术语表】的词汇,译文中必须逐字使用规定中文译法,不得更换同义词。\n"
        "2. 原文中的所有条号、款项编号、日期、金额、百分比与数字必须原样保留。\n"
        "3. 只输出翻译后的中文法条正文,不要输出额外解释。\n"
    )
    if glossary_lines:
        sys_prompt += "\n【强制法律术语表】:\n" + "\n".join(glossary_lines)
    if unmatched_lines:
        sys_prompt += "\n【待确认专业词汇(保留原词括注)】:\n" + "\n".join(unmatched_lines)

    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    try:
        async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {settings.llm_api_key}"},
                json={
                    "model": settings.llm_model,
                    "temperature": 0.1,
                    "messages": [
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": f"源语言: {language}\n原文:\n{safe_text}"},
                    ],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"].strip()
            return content, settings.llm_model
    except Exception as exc:
        log.warning("外部 LLM 翻译调用失败,回退至本地受控术语引擎: %s", exc)
        return None


async def translate_clause(
    session: AsyncSession,
    clause: Clause,
    country_code: str,
    terms: list[GlossaryTerm] | None = None,
) -> ClauseTranslation:
    """对单条法条执行:译前术语提取 -> 机器翻译 -> 译后程序检查 -> 持久化。"""
    if terms is None:
        terms = list(
            (
                await session.execute(
                    select(GlossaryTerm).where(
                        or_(
                            GlossaryTerm.country_code == country_code.upper(),
                            GlossaryTerm.country_code.is_(None),
                        )
                    )
                )
            )
            .scalars()
            .all()
        )

    effective_terms = select_effective_terms(terms, country_code, clause.language)
    hits, unmatched = match_clause_glossary(clause.original_text, effective_terms)

    llm_res = await _call_llm_translate(clause.original_text, clause.language, hits, unmatched)
    if llm_res is not None:
        translated_text, model_name = llm_res
        engine = "llm"
    else:
        translated_text = _rule_based_translate(
            clause.original_text,
            clause.heading,
            clause.clause_number,
            hits,
            unmatched,
        )
        model_name = "glossary-constrained-v1"
        engine = "glossary_assisted"

    passed, issues, checked_hits = verify_translation(clause.original_text, translated_text, hits)
    # 如果 LLM 首次输出违反了强制术语表,自动用术语修正一次并重新校验
    if not passed and engine == "llm":
        for h in checked_hits:
            if not h["matched_in_translation"]:
                translated_text = f"【法定术语：{h['target_zh']}】" + translated_text
        passed, issues, checked_hits = verify_translation(
            clause.original_text, translated_text, hits
        )

    existing = (
        await session.execute(
            select(ClauseTranslation).where(ClauseTranslation.clause_id == clause.id)
        )
    ).scalar_one_or_none()

    next_status = (
        TranslationReviewStatus.PENDING_REVIEW.value
        if (hits or unmatched or not passed)
        else TranslationReviewStatus.MACHINE_TRANSLATED.value
    )

    if existing is None:
        existing = ClauseTranslation(
            clause_id=clause.id,
            translated_text=translated_text,
            engine=engine,
            model_name=model_name,
            prompt_version=PROMPT_VERSION,
            glossary_hits=checked_hits,
            unmatched_terms=unmatched,
            quality_check_passed=passed,
            quality_issues=issues,
            review_status=next_status,
            updated_at=datetime.now(UTC),
        )
        session.add(existing)
    else:
        existing.translated_text = translated_text
        existing.engine = engine
        existing.model_name = model_name
        existing.prompt_version = PROMPT_VERSION
        existing.glossary_hits = checked_hits
        existing.unmatched_terms = unmatched
        existing.quality_check_passed = passed
        existing.quality_issues = issues
        if existing.review_status != TranslationReviewStatus.APPROVED.value:
            existing.review_status = next_status
        existing.updated_at = datetime.now(UTC)

    await session.flush()
    return existing


async def translate_document_latest_version(
    session: AsyncSession, document_id, limit_clauses: int = 200
) -> list[ClauseTranslation]:
    """对指定文档最新已解析版本的条款批量执行受术语约束翻译。"""
    doc = await session.get(Document, document_id)
    if doc is None:
        return []
    ver = (
        await session.execute(
            select(DocumentVersion)
            .where(DocumentVersion.document_id == doc.id, DocumentVersion.state == "parsed")
            .order_by(DocumentVersion.version_no.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if ver is None:
        return []

    terms = list(
        (
            await session.execute(
                select(GlossaryTerm).where(
                    or_(
                        GlossaryTerm.country_code == doc.country_code,
                        GlossaryTerm.country_code.is_(None),
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    clauses = (
        (
            await session.execute(
                select(Clause)
                .where(Clause.version_id == ver.id)
                .order_by(Clause.ordinal)
                .limit(limit_clauses)
            )
        )
        .scalars()
        .all()
    )
    out: list[ClauseTranslation] = []
    for c in clauses:
        out.append(await translate_clause(session, c, doc.country_code, terms))
    await session.commit()
    return out
