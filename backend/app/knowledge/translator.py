"""受法律术语库约束的真实大模型机器翻译、译前脱敏、译前术语提取、长条款分块与译后程序检查。

遵循 S1 规范:
1. 发往外部大模型前先做敏感信息脱敏(邮箱、电话、证件号)。
2. 译前提取条款中命中的术语(国家覆盖优先于通用概念);保留术语命中统计,不再产出本地假译文。
3. 分块:长条款按句/款拆分,每块 <= 约 1500 字符,保持顺序合并。
4. 提示词(translate-v2):法律翻译、逐句忠实、不增不减、保留条号与数字、术语表强制。
5. 调用 OpenAI 兼容接口(沿用 settings.llm_*);并发用 asyncio.Semaphore(默认 4)限流,带指数退避重试。
6. 译后程序检查(任一失败则重译一次,仍失败标 translation_failed 并记录原因):
   ① CJK 占比 >= 30%(阿语/法语/英文原文都应翻成中文);
   ② 原文中的数字、金额、百分比、年份、条号在译文中全部出现;
   ③ 术语表要求的译法出现;
   ④ 译文不等于原文。
7. 状态机:machine_translated / translation_failed / pending_review / approved /
   rejected / stale / not_translated / superseded。
8. 成本核算:记录 engine=llm、model_name、prompt_version、tokens_in、tokens_out、duration_ms。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.ingestion.models import Clause, Document, DocumentVersion
from app.knowledge.models import ClauseTranslation, GlossaryTerm, TranslationReviewStatus
from app.knowledge.numeric_integrity import (
    NUMERIC,
    canonical,
    dates,
    missing_amounts,
    normalize,
    tokens,
)

log = logging.getLogger(__name__)

PROMPT_VERSION = "translate-v2"
MAX_CHUNK_CHARS = 1500
DEFAULT_CONCURRENCY = 4

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"(?:\+\d{1,3}[-\s]?)?\b\d{3,4}[-\s]\d{4,8}\b")
_SG_NRIC_RE = re.compile(r"\b[STFGM]\d{7}[A-Z]\b")
_NUM_TOKEN_RE = NUMERIC
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
_WS_RE = re.compile(r"\s+")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。！？；;])\s+")

_EASTERN_ARABIC_MAP = str.maketrans(
    "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹％",
    "01234567890123456789%",
)

# 进程级并发信号量(按需绑定当前事件循环)
_CONCURRENCY_LIMIT: int = DEFAULT_CONCURRENCY
_SEMAPHORE: asyncio.Semaphore | None = None
_SEMAPHORE_LOOP_ID: int | None = None


def set_translation_concurrency(concurrency: int) -> None:
    """设置翻译并发限流上限(默认 4)。"""
    global _CONCURRENCY_LIMIT, _SEMAPHORE, _SEMAPHORE_LOOP_ID
    _CONCURRENCY_LIMIT = max(1, int(concurrency))
    _SEMAPHORE = None
    _SEMAPHORE_LOOP_ID = None


def _get_llm_semaphore() -> asyncio.Semaphore:
    global _SEMAPHORE, _SEMAPHORE_LOOP_ID
    try:
        loop_id = id(asyncio.get_running_loop())
    except RuntimeError:
        loop_id = None
    if _SEMAPHORE is None or _SEMAPHORE_LOOP_ID != loop_id:
        _SEMAPHORE = asyncio.Semaphore(_CONCURRENCY_LIMIT)
        _SEMAPHORE_LOOP_ID = loop_id
    return _SEMAPHORE


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


def redact_sensitive_text(text: str) -> str:
    """发往外部大模型 API 前脱敏邮箱、电话与身份证件号。"""
    out = _EMAIL_RE.sub("[REDACTED_EMAIL]", text or "")
    out = _SG_NRIC_RE.sub("[REDACTED_ID]", out)
    out = _PHONE_RE.sub("[REDACTED_PHONE]", out)
    return out


def compute_cjk_ratio(text: str) -> float:
    """计算去除空白字符后的中日韩统一表意文字(CJK)占比。"""
    compact = _WS_RE.sub("", text or "")
    if not compact:
        return 0.0
    return len(_CJK_RE.findall(compact)) / len(compact)


def _normalize_digits(text: str) -> str:
    return normalize(text)


def extract_numeric_tokens(text: str) -> list[str]:
    """提取原文中的数字、金额数值、百分比、年份与条号数字(去重保序)。"""
    norm = _normalize_digits(text)
    seen: set[str] = set()
    tokens: list[str] = []
    for tok in _NUM_TOKEN_RE.findall(norm):
        t = tok.strip()
        if t and t not in seen:
            seen.add(t)
            tokens.append(t)
    return tokens


def split_clause_into_chunks(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """将长条款按款/句拆分为每块 <= max_chars 字符的片段,保持原有顺序。"""
    cleaned = (text or "").strip()
    if not cleaned:
        return [""]
    if len(cleaned) <= max_chars:
        return [cleaned]

    # 先按换行分段,再对超长段按句号/分号切分,仍超长则按空格或硬切
    atomic_segments: list[str] = []
    for raw_para in cleaned.splitlines():
        para = raw_para.strip()
        if not para:
            continue
        if len(para) <= max_chars:
            atomic_segments.append(para)
            continue
        for sent in _SENTENCE_SPLIT_RE.split(para):
            s = sent.strip()
            if not s:
                continue
            if len(s) <= max_chars:
                atomic_segments.append(s)
            else:
                # 按词边界切分超长单句
                words = s.split(" ")
                cur = ""
                for w in words:
                    cand = f"{cur} {w}".strip() if cur else w
                    if len(cand) <= max_chars:
                        cur = cand
                    else:
                        if cur:
                            atomic_segments.append(cur)
                        while len(w) > max_chars:
                            atomic_segments.append(w[:max_chars])
                            w = w[max_chars:]
                        cur = w
                if cur:
                    atomic_segments.append(cur)

    chunks: list[str] = []
    current = ""
    for seg in atomic_segments:
        candidate = f"{current}\n{seg}" if current else seg
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = seg
    if current:
        chunks.append(current)
    return chunks or [cleaned[:max_chars]]


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
        group.sort(
            key=lambda x: (0 if x.country_code == country_code.upper() else 1, x.priority_level)
        )
        effective.append(group[0])
    effective.sort(key=lambda x: (x.priority_level, -len(x.source_term)))
    return effective


def match_clause_glossary(
    original_text: str, effective_terms: list[GlossaryTerm]
) -> tuple[list[dict], list[dict]]:
    """在条款原文中识别命中的术语库词条,以及未入库但需悬浮解释的专业词(仅作统计与提示词传入)。"""
    hits: list[dict] = []
    lower_text = (original_text or "").lower()
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


def _number_appears_in_translation(
    num_token: str, translated_norm: str, translated_no_comma: str
) -> bool:
    """检查原文数字/金额/百分比/年份/条号是否在译文中出现。"""
    return not num_token.strip() or canonical(num_token) in tokens(translated_norm)


def verify_translation(
    original_text: str,
    translated_text: str,
    glossary_hits: list[dict],
    clause_number: str | None = None,
) -> tuple[bool, list[str], list[dict]]:
    """译后程序检查(四项强校验):
    ① CJK 占比 >= 30%(阿语/法语/英文原文都应翻成中文);
    ② 原文中的数字、金额、百分比、年份、条号在译文中全部出现;
    ③ 术语表要求的译法出现;
    ④ 译文不等于原文且非空。
    """
    issues: list[str] = []
    orig_clean = (original_text or "").strip()
    trans_clean = (translated_text or "").strip()

    # ④ 译文非空且不等于原文
    if not trans_clean:
        issues.append("译文为空")
    elif trans_clean == orig_clean:
        issues.append("译文与原文完全相同")

    # ① CJK 字符占比 >= 30%
    cjk_ratio = compute_cjk_ratio(trans_clean)
    if cjk_ratio < 0.30:
        issues.append(f"CJK 字符占比未达标: {cjk_ratio:.1%} < 30.0%")

    # ③ 术语表要求的译法全部出现
    updated_hits: list[dict] = []
    for h in glossary_hits:
        ok = bool(trans_clean) and (h["target_zh"] in trans_clean)
        item = dict(h, matched_in_translation=ok)
        updated_hits.append(item)
        if not ok:
            issues.append(
                f"术语未按规定译法输出: 原词 '{h['source_term']}' 必须译为 '{h['target_zh']}'"
            )

    # ② 原文中的数字、金额、百分比、年份、条号在译文中全部出现
    translated_norm = _normalize_digits(trans_clean)
    translated_no_comma = translated_norm.replace(",", "")
    orig_nums = extract_numeric_tokens(orig_clean)
    if clause_number:
        for cn_tok in extract_numeric_tokens(clause_number):
            if cn_tok in _normalize_digits(orig_clean) and cn_tok not in orig_nums:
                orig_nums.append(cn_tok)

    for num in orig_nums:
        if not _number_appears_in_translation(num, translated_norm, translated_no_comma):
            issues.append(f"译文缺失原文数字或比例: {num}")
    for amount in missing_amounts(orig_clean, trans_clean):
        issues.append(f"译文金额或币种不一致: {amount}")
    for year, month, day in sorted(dates(orig_clean) - dates(trans_clean)):
        issues.append(f"译文日期不一致: {year:04}-{month:02}-{day:02}")

    return len(issues) == 0, issues, updated_hits


def _build_translation_prompts(
    safe_text: str,
    language: str,
    glossary_hits: list[dict],
    unmatched_terms: list[dict],
    retry_issues: list[str] | None = None,
) -> tuple[str, str]:
    """构造 translate-v2 系统提示词与用户提示词。"""
    sys_lines = [
        "你是跨境汽车与数据合规领域的专业法律翻译引擎（提示词版本 translate-v2）。",
        "任务性质：法律翻译、逐句忠实、不增不减、保留条号与数字、术语表强制。",
        "请将给定的外文法律条款精准翻译为简体中文，并严格遵守以下强制准则：",
        (
            "1. 【逐句忠实、不增不减】：严格忠实于原文法律含义，"
            "逐句完整翻译为规范简体中文法律表述，不得擅自增删、概括、省略或添加解释性评注。"
        ),
        (
            "2. 【保留条号与数字】：原文中的所有条号、款项编号、阿拉伯数字、"
            "金额、百分比、日期与年份必须在译文中原样保留阿拉伯数字形式"
            "（如 1,000,000、10%、2020、3 等，严禁遗漏或仅改写为汉字数字）。"
        ),
        (
            "3. 【术语表强制】：凡命中下方【强制法律术语表】的原词，译文中必须严格、"
            "逐字使用规定的中文译法（规定译法），不得替换为同义词或近义词。"
        ),
        (
            "4. 【中文占比要求】：即使原文包含专有名词、机构缩写或公式标题，"
            "主体内容也必须翻译为简体中文（中文字符占比必须 >= 30%），严禁直接照抄外文原文。"
        ),
        (
            "5. 【纯译文输出】：只输出翻译后的简体中文法条正文，"
            "不要输出任何前言、后记、Markdown 代码块标记或说明。"
        ),
    ]
    if glossary_hits:
        sys_lines.append("\n【强制法律术语表（原词 -> 规定译法 -> 法定定义）】：")
        for h in glossary_hits:
            defn = (
                h.get("statutory_definition") or h.get("source_reference") or "法定标准定义"
            ).strip()
            sys_lines.append(
                f"- 原词: {h['source_term']} -> 规定译法: {h['target_zh']} -> 法定定义: {defn}"
            )
    if unmatched_terms:
        sys_lines.append("\n【专业词汇参考译法】：")
        for u in unmatched_terms:
            sys_lines.append(
                f"- 原词: {u['source_term']} -> 建议译法: {u['provisional_zh']} "
                f"({u['statutory_definition']})"
            )

    req_nums = extract_numeric_tokens(safe_text)
    user_parts = [f"源语言: {language}"]
    if req_nums:
        user_parts.append(f"必须在译文中原样保留的数字/金额/百分比/条号: {', '.join(req_nums)}")
    if glossary_hits:
        must_zh = ", ".join(f"'{h['target_zh']}'" for h in glossary_hits)
        user_parts.append(f"必须在译文中原样出现的强制术语译法: {must_zh}")
    if retry_issues:
        user_parts.append(
            "【重要修正指令】上一次翻译未通过译后程序检查，具体原因如下：\n"
            + "\n".join(f"  * {iss}" for iss in retry_issues)
            + "\n请严格修正上述问题，确保所有必现数字、强制术语完整出现且简体中文字符占比 >= 30%！"
        )
    user_parts.append(f"待翻译原文:\n{safe_text}")
    return "\n".join(sys_lines), "\n\n".join(user_parts)


async def _call_llm_translate(
    original_text: str,
    language: str,
    glossary_hits: list[dict],
    unmatched_terms: list[dict],
    *,
    retry_issues: list[str] | None = None,
) -> tuple[str, str, int, int, int] | tuple[str, str] | None:
    """调用 OpenAI 兼容接口进行受术语表约束的法条翻译。

    并发通过 asyncio.Semaphore(默认 4)限流,带指数退避重试。
    返回 (translated_text, model_name, tokens_in, tokens_out, duration_ms);
    当未配置密钥或重试后仍不可用时返回 None。
    """
    settings = get_settings()
    if not settings.llm_api_key:
        return None

    safe_text = redact_sensitive_text(original_text)
    sys_prompt, user_prompt = _build_translation_prompts(
        safe_text, language, glossary_hits, unmatched_terms, retry_issues=retry_issues
    )

    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    payload: dict[str, Any] = {
        "model": settings.llm_model,
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    if "deepseek" in (settings.llm_model or "").lower():
        payload["reasoning_effort"] = "low"

    semaphore = _get_llm_semaphore()
    max_attempts = 3
    t_start = time.perf_counter()

    async with semaphore:
        for attempt in range(max_attempts):
            try:
                async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds) as client:
                    resp = await client.post(
                        url,
                        headers={"Authorization": f"Bearer {settings.llm_api_key}"},
                        json=payload,
                    )
                    if resp.status_code in (429, 500, 502, 503, 504) and attempt + 1 < max_attempts:
                        await asyncio.sleep(0.6 * (2**attempt))
                        continue
                    resp.raise_for_status()
                    data = resp.json()
                    content = (data["choices"][0]["message"].get("content") or "").strip()
                    usage = data.get("usage") or {}
                    tokens_in = int(usage.get("prompt_tokens") or 0)
                    tokens_out = int(usage.get("completion_tokens") or 0)
                    duration_ms = max(1, int((time.perf_counter() - t_start) * 1000))
                    model_used = str(data.get("model") or settings.llm_model)
                    return content, model_used, tokens_in, tokens_out, duration_ms
            except Exception as exc:
                if attempt + 1 < max_attempts:
                    await asyncio.sleep(0.6 * (2**attempt))
                    continue
                log.warning("外部 LLM 翻译调用失败 (已重试 %d 次): %s", max_attempts, exc)
                return None
    return None


def _unpack_llm_result(
    res: tuple[Any, ...] | str | None, default_model: str
) -> tuple[str, str, int, int, int] | None:
    """兼容单元测试 monkeypatch 返回的 (text, model) 二元组或完整五元组。"""
    if res is None:
        return None
    if isinstance(res, str):
        return res, default_model, 0, 0, 0
    if len(res) == 2:
        return str(res[0]), str(res[1]), 0, 0, 0
    if len(res) >= 5:
        return str(res[0]), str(res[1]), int(res[2] or 0), int(res[3] or 0), int(res[4] or 0)
    return str(res[0]), default_model, 0, 0, 0


async def _invoke_llm_translate(
    chunk_text: str,
    language: str,
    chunk_hits: list[dict],
    chunk_unmatched: list[dict],
    retry_issues: list[str] | None = None,
) -> tuple[str, str, int, int, int] | None:
    """调用 _call_llm_translate,自动检测被 monkeypatch 的假函数是否支持 retry_issues 关键字参数。"""
    settings = get_settings()
    fn = _call_llm_translate
    supports_retry_kw = False
    try:
        sig = inspect.signature(fn)
        for p in sig.parameters.values():
            if p.name == "retry_issues" or p.kind == inspect.Parameter.VAR_KEYWORD:
                supports_retry_kw = True
                break
    except (ValueError, TypeError):
        supports_retry_kw = False

    if supports_retry_kw and retry_issues:
        raw = await fn(chunk_text, language, chunk_hits, chunk_unmatched, retry_issues=retry_issues)
    else:
        raw = await fn(chunk_text, language, chunk_hits, chunk_unmatched)
    return _unpack_llm_result(raw, settings.llm_model or "llm")


async def translate_clause(
    session: AsyncSession,
    clause: Clause,
    country_code: str,
    terms: list[GlossaryTerm] | None = None,
) -> ClauseTranslation:
    """对单条法条执行:术语提取 -> 分块 -> LLM 翻译 -> 四项检查(失败重译一次) -> 持久化。"""
    settings = get_settings()
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

    # Serialize retries for one clause; never reuse a previous row for new model output.
    await session.execute(select(Clause.id).where(Clause.id == clause.id).with_for_update())
    existing_rows = list(
        (
            await session.scalars(
                select(ClauseTranslation)
                .where(ClauseTranslation.clause_id == clause.id)
                .order_by(ClauseTranslation.updated_at.desc(), ClauseTranslation.id)
            )
        ).all()
    )
    current_rows = [
        row
        for row in existing_rows
        if row.is_current and row.review_status not in ("superseded", "stale", "rejected")
    ]
    approved = next((row for row in current_rows if row.review_status == "approved"), None)
    if approved is not None:
        return approved  # Human approval is bound to exactly this text, not to a retry.
    previous_good = next(
        (
            row
            for row in current_rows
            if row.engine == "llm"
            and row.quality_check_passed
            and verify_translation(
                clause.original_text, row.translated_text, row.glossary_hits or []
            )[0]
        ),
        None,
    )

    chunks = split_clause_into_chunks(clause.original_text, max_chars=MAX_CHUNK_CHARS)

    total_tokens_in = 0
    total_tokens_out = 0
    total_duration_ms = 0
    model_name = settings.llm_model or "deepseek-flash"
    llm_unavailable = False
    chunk_failures: list[str] = []

    if len(chunks) == 1:
        # 单块条款:翻译 -> 译后检查 -> 若未通过则携带问题原因重译一次
        first_res = await _invoke_llm_translate(chunks[0], clause.language, hits, unmatched)
        if first_res is None:
            llm_unavailable = True
            translated_text = ""
        else:
            translated_text, model_name, tin, tout, dms = first_res
            total_tokens_in += tin
            total_tokens_out += tout
            total_duration_ms += dms
            passed, issues, checked_hits = verify_translation(
                clause.original_text,
                translated_text,
                hits,
                clause_number=clause.clause_number,
            )
            if not passed:
                retry_res = await _invoke_llm_translate(
                    chunks[0],
                    clause.language,
                    hits,
                    unmatched,
                    retry_issues=issues,
                )
                if retry_res is not None:
                    translated_text, model_name, tin2, tout2, dms2 = retry_res
                    total_tokens_in += tin2
                    total_tokens_out += tout2
                    total_duration_ms += dms2
    else:
        # 多块长条款:每块独立提取命中术语并校验,单块失败重译一次,按顺序合并后再做整条总校验
        async def _translate_single_chunk(chunk_str: str) -> tuple[str | None, str, int, int, int]:
            c_hits, c_unmatched = match_clause_glossary(chunk_str, effective_terms)
            r1 = await _invoke_llm_translate(chunk_str, clause.language, c_hits, c_unmatched)
            if r1 is None:
                return None, model_name, 0, 0, 0
            c_zh, c_model, tin1, tout1, dms1 = r1
            c_ok, c_issues, _ = verify_translation(chunk_str, c_zh, c_hits)
            if not c_ok:
                r2 = await _invoke_llm_translate(
                    chunk_str,
                    clause.language,
                    c_hits,
                    c_unmatched,
                    retry_issues=c_issues,
                )
                if r2 is not None:
                    c_zh, c_model, tin2, tout2, dms2 = r2
                    retry_ok, retry_issues, _ = verify_translation(chunk_str, c_zh, c_hits)
                    if not retry_ok:
                        chunk_failures.extend(retry_issues)
                    return c_zh, c_model, tin1 + tin2, tout1 + tout2, dms1 + dms2
                chunk_failures.extend(c_issues)
            return c_zh, c_model, tin1, tout1, dms1

        chunk_results = await asyncio.gather(*(_translate_single_chunk(ch) for ch in chunks))
        merged_parts: list[str] = []
        for c_zh, c_model, tin, tout, dms in chunk_results:
            total_tokens_in += tin
            total_tokens_out += tout
            total_duration_ms += dms
            if c_zh is None:
                llm_unavailable = True
                continue
            merged_parts.append(c_zh)
            model_name = c_model
        translated_text = "\n".join(merged_parts) if not llm_unavailable else ""

    if llm_unavailable:
        checked_hits = [dict(h, matched_in_translation=False) for h in hits]
        passed = False
        issues = ["LLM 服务不可用或未配置密钥，未生成译文"]
        next_status = TranslationReviewStatus.NOT_TRANSLATED.value
        translated_text = ""
    else:
        passed, issues, checked_hits = verify_translation(
            clause.original_text,
            translated_text,
            hits,
            clause_number=clause.clause_number,
        )
        if chunk_failures:
            passed = False
            issues.extend("分块校验未通过: " + issue for issue in chunk_failures)
        next_status = (
            TranslationReviewStatus.MACHINE_TRANSLATED.value
            if passed
            else TranslationReviewStatus.TRANSLATION_FAILED.value
        )

    from app.knowledge.history import snapshot

    # A failed attempt is retained for diagnostics, while a good current translation stays usable.
    make_current = passed or previous_good is None
    if make_current:
        for row in existing_rows:
            if not row.is_current:
                continue
            snapshot(session, row, "before_translation_replacement")
            row.is_current = False
            if row.review_status not in ("approved", "rejected"):
                row.review_status = TranslationReviewStatus.SUPERSEDED.value
    attempt = ClauseTranslation(
        clause_id=clause.id,
        is_current=make_current,
        translated_text=translated_text,
        engine="llm",
        model_name=model_name,
        prompt_version=PROMPT_VERSION,
        tokens_in=total_tokens_in,
        tokens_out=total_tokens_out,
        duration_ms=total_duration_ms,
        glossary_hits=checked_hits,
        unmatched_terms=unmatched,
        quality_check_passed=passed,
        quality_issues=issues,
        review_status=next_status,
        updated_at=datetime.now(UTC),
    )
    session.add(attempt)
    await session.flush()
    snapshot(session, attempt, "translation_attempt")
    return attempt


async def translate_document_latest_version(
    session: AsyncSession,
    document_id,
    limit_clauses: int | None = None,
    only_missing: bool = True,
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
    clauses = list(
        (
            await session.execute(
                select(Clause).where(Clause.version_id == ver.id).order_by(Clause.ordinal)
            )
        )
        .scalars()
        .all()
    )
    out: list[ClauseTranslation] = []
    for c in clauses:
        current = (
            await session.scalars(
                select(ClauseTranslation).where(
                    ClauseTranslation.clause_id == c.id,
                    ClauseTranslation.is_current.is_(True),
                    ClauseTranslation.review_status.in_(
                        ("machine_translated", "pending_review", "approved")
                    ),
                )
            )
        ).all()
        if only_missing and any(
            row.review_status == "approved"
            or (
                row.quality_check_passed
                and verify_translation(
                    c.original_text, row.translated_text, row.glossary_hits or []
                )[0]
            )
            for row in current
        ):
            continue
        if limit_clauses is not None and len(out) >= limit_clauses:
            break
        out.append(await translate_clause(session, c, doc.country_code, terms))
        await session.commit()  # Resume skips completed clauses after a later failure.
    return out
