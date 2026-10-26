"""Source-bound terminology. Definitions are reusable; legal scope is never borrowed.

Source matching is a machine check, independent of historical human approvals.
Only explicit definition constructions are admitted automatically. Unrecognised
layouts remain available as source documents, never guessed into definitions.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert

from app.ingestion.models import Clause, Document, DocumentVersion
from app.ingestion.safety import document_available, document_is_held, held_citation_ids
from app.knowledge.models import ClauseTranslation, GlossaryTerm, LegalTermDefinition, ReviewPoint

# Lexical identities/Chinese renderings, not assertions of equivalent local law.
# Authorities and definitions are separately bound to documents and versions.
CATALOG = [
    (
        "personal_data",
        "个人数据",
        {
            "en": ["personal data", "personal information"],
            "fr": ["données à caractère personnel", "données personnelles"],
            "ar": ["البيانات الشخصية", "المعطيات الشخصية", "المعطيات ذات الطابع الشخصي"],
        },
    ),
    (
        "data_controller",
        "数据控制者",
        {
            "en": ["controller", "data controller"],
            "fr": ["responsable du traitement", "responsable de traitement"],
            "ar": ["المتحكم", "المسؤول عن المعالجة", "مسؤول المعالجة"],
        },
    ),
    (
        "data_processor",
        "数据处理者",
        {
            "en": ["processor", "data processor"],
            "fr": ["sous-traitant"],
            "ar": ["المعالج", "معالج البيانات"],
        },
    ),
    (
        "data_subject",
        "数据主体",
        {
            "en": ["data subject"],
            "fr": ["personne concernée"],
            "ar": ["الشخص المعني", "صاحب البيانات"],
        },
    ),
    (
        "sensitive_data",
        "敏感数据",
        {
            "en": ["sensitive data", "sensitive personal data"],
            "fr": ["données sensibles"],
            "ar": ["البيانات الشخصية الحساسة", "المعطيات الحساسة"],
        },
    ),
    (
        "processing",
        "处理",
        {"en": ["processing"], "fr": ["traitement"], "ar": ["المعالجة", "معالجة"]},
    ),
    ("consent", "同意", {"en": ["consent"], "fr": ["consentement"], "ar": ["الموافقة"]}),
    ("data_intermediary", "数据中介机构", {"en": ["data intermediary"]}),
    (
        "data_protection_officer",
        "数据保护官",
        {
            "en": ["data protection officer"],
            "fr": ["délégué à la protection des données"],
            "ar": ["مسؤول حماية البيانات"],
        },
    ),
    (
        "cross_border_transfer",
        "跨境传输",
        {
            "en": ["cross-border transfer"],
            "fr": ["transfert transfrontalier"],
            "ar": ["النقل عبر الحدود"],
        },
    ),
]
PRIORITY = {
    "law_definition": 1,
    "regulator_guidance": 2,
    "official_parallel": 3,
    "international_reference": 4,
}
DEFINITION_RULE_VERSION = "explicit-definition-v5"
ORIGIN_PRIORITY = {"manual": 0, "machine_translation": 1, "machine_reference": 1, "auto": 2}
SOURCE_LABEL = {
    "law_definition": "法律定义",
    "regulator_guidance": "监管说明",
    "official_parallel": "官方语种对照",
    "international_reference": "国际参考",
}
QUOTED = re.compile(
    r'[“‘"«](?P<term>[^“‘”’"«»\n]{2,100})[”’"»]'
    r'(?:\s*,?\s*(?:in relation to|in respect of|for the purposes of)\s+[^;“”"\n]{1,200}?)?\s*,?\s*'
    r"(?:means?|includes?|refers to|has the (?:same )?meaning|désigne|"
    r"s[’\']entend(?:\s+par)?|signifie|"
    r"يقصد\s+ب[هها]*|تعني)\s+",
    re.I,
)
COLON = re.compile(r"(?m)^\s*(?:[\d٠-٩]+\s*[-.)ـ]\s*)?(?P<term>[^\n:：]{2,90})\s*[:：]\s*")
DEFINITION_HEADING = re.compile(
    r"\bdefinitions?\b|\binterprétation\b|\bdéfinitions?\b|on entend par|تعريف|يقصد|المصطلحات", re.I
)

QUOTED_COLON = re.compile(r'[“‘"«]\s*(?P<term>[^“‘”’"«»\n]{2,100}?)\s*[”’"»]\s*[:：]\s*')
FRENCH_DIRECT = re.compile(
    r"on entend par\s+(?P<term>[^\n:：]{2,100}?)\s+(?=toutes?\b|tous\b|les?\b|la\b|une?\b)", re.I
)
STRUCTURAL_BOUNDARY = re.compile(
    r"(?m)^[ \t]*(?:(?:Article\.?|Art\.)\s*\d+\s*[:.\-]|CHAPITRE\b|PARTIE\b|TITRE\b|"
    r"Section\s+[IVX0-9]+\b|الفصل\s+[0-9٠-٩]+)",
    re.I,
)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def has_privacy_context(text):
    return any(
        alias.casefold() in text.casefold()
        for code, _, languages in CATALOG
        if code in {"personal_data", "data_subject", "sensitive_data"}
        for aliases in languages.values()
        for alias in aliases
    )


def identity(term, language, context=None):
    normal = " ".join(term.casefold().split())
    for code, target, aliases in CATALOG:
        if normal in [x.casefold() for x in aliases.get(language.split("-")[0], [])]:
            if (
                code in {"data_controller", "data_processor", "processing"}
                and context is not None
                and not has_privacy_context(context)
            ):
                break
            return code, target
    return "term_" + sha(language + ":" + normal)[:24], term


def definition_spans(text, heading=""):
    """Return exact offsets, never repaired/truncated words or inferred definitions."""
    matches = list(QUOTED.finditer(text)) + list(FRENCH_DIRECT.finditer(text))
    if DEFINITION_HEADING.search(heading + "\n" + text[:300]):
        matches += list(QUOTED_COLON.finditer(text))
        if not matches:
            matches = list(COLON.finditer(text))
    matches.sort(key=lambda m: m.start())
    out = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        # A direct prose definition ends with its defining sentence, not later duties.
        if m.re is FRENCH_DIRECT:
            sentence_end = re.search(r"\.(?=\s+[A-ZÀ-Ý]|$)", text[m.end() : end])
            if sentence_end:
                end = m.end() + sentence_end.end()
        boundary = STRUCTURAL_BOUNDARY.search(text, m.end(), end)
        if boundary:
            end = boundary.start()
        # An amending instruction can wrap an inserted definition in another
        # quotation. Keep its terminating punctuation, not the next instruction.
        amendment_end = re.search(
            r'(?<=[.;؛。])[”"]\s*(?:[”"]\s*)?;\s*(?=\([a-z]\)|(?:delete|insert|omit|replace)\b)',
            text[m.end() : end],
            re.I,
        )
        if amendment_end:
            end = m.end() + amendment_end.start()
        while end > m.end() and text[end - 1].isspace():
            end -= 1
        term = re.sub(
            r"^(?:\.[0-9٠-٩]+\s*|[0-9٠-٩]+\s*[.)\-]\s*|[–—\-•]\s*)", "", m.group("term").strip()
        )
        term = term.strip(" “«»”")
        if re.match(r"(?:Art\.?|Article)\s*\d", term, re.I) or "\n" in term:
            continue
        if any(term.count(a) != term.count(b) for a, b in (("(", ")"), ("[", "]"))):
            continue
        body = text[m.end() : end].strip()
        if not 12 <= len(body) <= 6000 or len(term.split()) > 12:
            continue
        # A dangling colon/list header or a mid-word fragment isn't a complete definition.
        if not re.search(r"[.;؛。…]$", body) or body.endswith(("...", "…")):
            continue
        out.append(
            {"source_term": term, "quote": text[m.start() : end], "start": m.start(), "end": end}
        )
    return out


def _zh_definition(text, target):
    if not text or target.isascii():
        return None
    pattern = re.compile(re.escape(target) + r"[”’\"）)]*\s*(?:是指|系指|指|：|:)\s*")
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        return None
    m = matches[0]
    if not m:
        return None
    end = text.find("。", m.end())
    if end < 0 or end - m.start() > 1400:
        return None
    return text[m.start() : end + 1]  # exact excerpt of the retained translation


def _source_kind(doc):
    kind = (doc.instrument_kind or doc.document_type or "").upper()
    if any(word in kind for word in ("GUID", "FAQ", "Q&A")):
        return "regulator_guidance"
    if kind in {"BASE", "COMMENCEMENT"}:
        kind = (doc.document_type or "").upper()
    if kind in {
        "LAW",
        "ACT",
        "REGULATION",
        "DECREE",
        "ORDER",
        "STATUTE",
        "RULES",
        "PRIMARY",
        "SUBSIDIARY",
        "AMENDMENT",
    }:
        return "law_definition"
    return None


def definition_scope(clause, term):
    """Respect explicit local-definition chapeaux; never guess a missing chapter path."""
    before = clause.original_text[: term.quote_start or 0]
    context = (clause.heading or "") + "\n" + before[:300] + before[-220:]
    local = r"(?:this|the present)\s+(?:subsection|section|paragraph|rule|regulation)\b"
    local += r"|(?:présent|cet)\s+article\b|هذا\s+(?:الفصل|المادة)"
    chapter = r"this\s+(?:part|chapter)\b|présent\s+chapitre\b|هذا\s+(?:الباب|القسم)"
    if re.search(local, context, re.I):
        return "clause", ""
    if re.search(chapter, context, re.I):
        path = re.search(
            r"(?:Part|Chapter|Chapitre|الباب|القسم)\s+[\w]+", clause.section_path or "", re.I
        )
        return ("chapter", path.group()) if path else ("clause", "")
    return "document", ""


async def rebuild_library(session, country_code=None, document_id=None):
    """Idempotent append-only index of explicit definitions in archived parsed sources."""
    stmt = (
        select(Clause, DocumentVersion, Document)
        .join(DocumentVersion, Clause.version_id == DocumentVersion.id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(
            DocumentVersion.state == "parsed",
            document_available(),
            Document.relevance.in_(("relevant", "related")),
        )
    )
    if country_code:
        stmt = stmt.where(Document.country_code == country_code.upper())
    if document_id:
        stmt = stmt.where(Document.id == document_id)
    rows = (await session.execute(stmt.order_by(Document.id, Clause.ordinal))).all()
    held = set(await held_citation_ids(session, [c.id for c, _, _ in rows]))
    added = skipped = 0
    for clause, version, doc in rows:
        if clause.text_hash != sha(clause.original_text):
            skipped += 1
            continue
        source_kind = _source_kind(doc)
        if source_kind is None:
            skipped += 1
            continue
        if clause.id in held or version.legal_status == "repealed":
            skipped += 1
            continue
        if (
            version.text_source == "ocr" or clause.ocr_required
        ) and version.review_status != "approved":
            skipped += 1
            continue
        spans = definition_spans(clause.original_text, clause.heading or clause.section_path or "")
        if not spans:
            continue
        translations = list(
            (
                await session.scalars(
                    select(ClauseTranslation)
                    .where(
                        ClauseTranslation.clause_id == clause.id,
                        ClauseTranslation.is_current.is_(True),
                        ClauseTranslation.quality_check_passed.is_(True),
                        ClauseTranslation.review_status.in_(
                            ("approved", "machine_translated", "pending_review")
                        ),
                    )
                    .order_by(ClauseTranslation.updated_at.desc())
                )
            ).all()
        )
        for item in spans:
            code, target = identity(item["source_term"], clause.language, item["quote"])
            fingerprint = sha(
                f"{DEFINITION_RULE_VERSION}:{clause.id}:{sha(clause.original_text)}:"
                f"{item['start']}:{item['end']}"
            )
            if await session.scalar(
                select(LegalTermDefinition.id).where(LegalTermDefinition.fingerprint == fingerprint)
            ):
                continue
            zh = next(
                (
                    value
                    for tr in translations
                    if (value := _zh_definition(tr.translated_text, target))
                ),
                None,
            )
            statement = (
                insert(LegalTermDefinition)
                .values(
                    id=uuid.uuid4(),
                    concept_code=code,
                    country_code=doc.country_code,
                    language=clause.language,
                    source_term=item["source_term"],
                    target_zh=target,
                    definition_original=item["quote"],
                    definition_zh=zh,
                    source_kind=source_kind,
                    source_url=doc.source_url,
                    source_reference=(
                        f"{doc.title} · {clause.clause_number or clause.ordinal}"
                        f" · v{version.version_no}"
                    ),
                    document_id=doc.id,
                    version_id=version.id,
                    clause_id=clause.id,
                    quote_start=item["start"],
                    quote_end=item["end"],
                    source_text_sha256=sha(clause.original_text),
                    fingerprint=fingerprint,
                    verification_status="source_matched",
                )
                .on_conflict_do_nothing(index_elements=["fingerprint"])
                .returning(LegalTermDefinition.id)
            )
            added += int(await session.scalar(statement) is not None)
    await session.flush()
    return {
        "definitions_added": added,
        "source_clauses_examined": len(rows),
        "source_clauses_excluded": skipped,
        "model_calls": 0,
        "existing_approvals_changed": 0,
    }


async def current_definitions(session, country_code, document_id=None, version_id=None):
    # A definition remains valid only against exactly the retained source text.
    stmt = (
        select(LegalTermDefinition, Clause, DocumentVersion, Document)
        .join(Clause, Clause.id == LegalTermDefinition.clause_id)
        .join(DocumentVersion, DocumentVersion.id == Clause.version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(
            LegalTermDefinition.source_kind.in_(tuple(PRIORITY)),
            LegalTermDefinition.verification_status == "source_matched",
            or_(
                LegalTermDefinition.country_code == country_code.upper(),
                LegalTermDefinition.country_code.is_(None),
            ),
            document_available(),
            DocumentVersion.state == "parsed",
            DocumentVersion.review_status != "rejected",
            DocumentVersion.legal_status != "repealed",
        )
    )
    if document_id:
        stmt = stmt.where(
            or_(
                LegalTermDefinition.document_id == document_id,
                LegalTermDefinition.applies_to_document_id == document_id,
                (LegalTermDefinition.applies_to_document_id.is_(None))
                & LegalTermDefinition.source_kind.in_(
                    ("regulator_guidance", "international_reference")
                ),
            )
        )
    rows = (await session.execute(stmt)).all()
    held = set(await held_citation_ids(session, [clause.id for _, clause, _, _ in rows]))
    versions = dict(
        (
            await session.execute(
                select(DocumentVersion.document_id, func.max(DocumentVersion.version_no))
                .where(DocumentVersion.state == "parsed")
                .group_by(DocumentVersion.document_id)
            )
        ).all()
    )
    from app.updates.source_state import latest_observations_query

    observed = (
        dict(
            (
                await session.execute(
                    latest_observations_query(list({t.document_id for t, _, _, _ in rows}))
                )
            ).all()
        )
        if rows
        else {}
    )
    # A newly registered rendering supersedes only the same exact source definition.
    # Historical rows remain untouched, and this is never a legal approval.
    rows.sort(
        key=lambda row: (
            ORIGIN_PRIORITY.get(row[0].registration_origin, 3),
            -row[0].created_at.timestamp(),
            str(row[0].id),
        )
    )
    result, seen, parsed_spans = [], {}, {}
    for term, clause, version, _ in rows:
        historical_match = version_id is not None and version.id == version_id
        is_current = (
            version.id == observed[term.document_id]
            if term.document_id in observed
            else version.version_no == versions.get(term.document_id)
        )
        if clause.id in held or (not historical_match and not is_current):
            continue
        if version_id and term.document_id == document_id and not historical_match:
            continue
        if (
            version.text_source == "ocr" or clause.ocr_required
        ) and version.review_status != "approved":
            continue
        if term.version_id != clause.version_id or term.document_id != version.document_id:
            continue
        if clause.text_hash != sha(clause.original_text):
            continue
        if sha(clause.original_text) != term.source_text_sha256:
            continue
        if term.quote_start is None or term.quote_end is None:
            continue
        if clause.original_text[term.quote_start : term.quote_end] != term.definition_original:
            continue
        identity_key = (clause.id, term.quote_start, term.quote_end)
        if term.registration_origin not in {"manual", "machine_reference"}:
            if clause.id not in parsed_spans:
                parsed_spans[clause.id] = definition_spans(
                    clause.original_text, clause.heading or clause.section_path or ""
                )
            if not any(
                (x["start"], x["end"], x["source_term"])
                == (term.quote_start, term.quote_end, term.source_term)
                for x in parsed_spans[clause.id]
            ):
                continue
        dedup = (*identity_key, term.source_kind, term.applies_to_document_id)
        if dedup in seen:
            primary = seen[dedup]
            if (
                not primary.definition_zh
                and primary.target_zh == term.target_zh
                and term.definition_zh
            ):
                primary.definition_zh_display = term.definition_zh
            continue
        seen[dedup] = term
        term.source_scope, term.scope_path = definition_scope(clause, term)
        result.append(term)
    return result


@dataclass
class TranslationTerm:
    id: uuid.UUID
    concept_code: str
    country_code: str | None
    language: str
    source_term: str
    target_zh: str
    priority_level: int
    statutory_definition: str | None
    source_reference: str | None
    review_status: str = "pending"
    source_url: str = ""
    source_kind: str = "terminology_only"
    aliases: list[str] = field(default_factory=list)
    preserve_original: bool = False
    definition_id: str | None = None


async def translation_terms(session, clause, country_code, legacy_terms=None):
    document_id = await session.scalar(
        select(DocumentVersion.document_id).where(DocumentVersion.id == clause.version_id)
    )
    definitions = await current_definitions(session, country_code, document_id, clause.version_id)
    by_concept = {}
    for term in definitions:
        if term.source_scope == "clause" and term.clause_id != getattr(clause, "id", None):
            continue
        if term.source_scope == "chapter" and term.scope_path not in (
            getattr(clause, "section_path", "") or ""
        ):
            continue
        local_aliases = next(
            (
                aliases.get(clause.language.split("-")[0], [])
                for code, _, aliases in CATALOG
                if code == term.concept_code
            ),
            [],
        )
        same_language = term.language.split("-")[0] == clause.language.split("-")[0]
        if not same_language and not local_aliases:
            continue
        if (
            term.concept_code in {"data_controller", "data_processor", "processing"}
            and not has_privacy_context(clause.original_text)
            and term.document_id != document_id
        ):
            continue
        by_concept.setdefault(term.concept_code, []).append(term)
    selected = []
    for code, values in by_concept.items():
        values.sort(
            key=lambda t: (
                PRIORITY[t.source_kind],
                ORIGIN_PRIORITY.get(t.registration_origin, 3),
                -t.created_at.timestamp(),
                str(t.id),
            )
        )
        top = [t for t in values if PRIORITY[t.source_kind] == PRIORITY[values[0].source_kind]]
        distinct = {" ".join(t.definition_original.split()) for t in top}
        chosen = top[0]
        aliases = next(
            (a.get(clause.language.split("-")[0], []) for c, _, a in CATALOG if c == code), []
        )
        selected.append(
            TranslationTerm(
                id=chosen.id,
                concept_code=code,
                country_code=chosen.country_code,
                language=clause.language,
                source_term=chosen.source_term
                if chosen.language == clause.language
                else aliases[0],
                target_zh=chosen.target_zh,
                priority_level=PRIORITY[chosen.source_kind],
                statutory_definition=(
                    chosen.definition_original
                    if len(distinct) == 1
                    else "同名术语存在不同定义范围，保留原词；\n" + "\n".join(sorted(distinct))
                ),
                source_reference=chosen.source_reference,
                source_url=chosen.source_url,
                source_kind=chosen.source_kind,
                aliases=aliases,
                preserve_original=(
                    len(distinct) > 1
                    or chosen.target_zh == chosen.source_term
                    or code.startswith("term_")
                ),
                definition_id=str(chosen.id),
            )
        )
    # Legacy approved/seeded renderings remain available but do not become legal definitions.
    # The lexical catalog corrects common everyday mistranslations independently of legal scope.
    known = {t.concept_code for t in selected}
    privacy_context = any(
        alias.casefold() in clause.original_text.casefold()
        for code, _, languages in CATALOG
        if code in {"personal_data", "data_subject", "sensitive_data"}
        for aliases in languages.values()
        for alias in aliases
    )
    for code, target, aliases in CATALOG:
        if code in known:
            continue
        source_aliases = aliases.get(clause.language.split("-")[0], [])
        # An explicit same-word definition (e.g. a vehicle controller) outranks
        # a generic privacy rendering even if its concept is not in the catalog.
        if any(
            t.source_term.casefold() in {a.casefold() for a in source_aliases} for t in selected
        ):
            continue
        if source_aliases and (
            privacy_context or code not in {"data_controller", "data_processor", "processing"}
        ):
            selected.append(
                TranslationTerm(
                    id=uuid.uuid5(uuid.NAMESPACE_URL, "cyshuhe:lexicon:" + code),
                    concept_code=code,
                    country_code=None,
                    language=clause.language,
                    source_term=source_aliases[0],
                    target_zh=target,
                    priority_level=4,
                    statutory_definition=None,
                    source_reference="通用译名规范；具体定义按所引法规",
                    aliases=source_aliases,
                    source_kind="terminology_only",
                )
            )
    if legacy_terms is None:
        legacy_terms = list(
            (
                await session.scalars(
                    select(GlossaryTerm).where(
                        or_(
                            GlossaryTerm.country_code == country_code,
                            GlossaryTerm.country_code.is_(None),
                        )
                    )
                )
            ).all()
        )
    # Unknown legacy concepts keep their original translation path; no approval is changed.
    reserved = {code for code, _, _ in CATALOG} | {x.concept_code for x in selected}
    selected += [t for t in legacy_terms if t.concept_code not in reserved]
    return selected


async def register_reference(
    session,
    *,
    clause_id,
    source_term,
    target_zh,
    quote,
    source_kind,
    country_code=None,
    applies_to_document_id=None,
    actor_id=None,
):
    """Explicitly register an archived official reference; never infer parallel-law equivalence."""
    if source_kind not in {
        "law_definition",
        "regulator_guidance",
        "official_parallel",
        "international_reference",
    }:
        raise ValueError("请选择法律定义、监管说明、官方语种对照或国际参考")
    clause = await session.get(Clause, clause_id)
    if (
        clause is None
        or quote not in clause.original_text
        or source_term.casefold() not in quote.casefold()
    ):
        raise ValueError("定义及术语必须逐字对应已归档原文")
    version = await session.get(DocumentVersion, clause.version_id)
    doc = await session.get(Document, version.document_id)
    if await held_citation_ids(session, [clause.id]) or version.state != "parsed":
        raise ValueError("来源暂不可用于术语解释")
    if (
        version.text_source == "ocr" or clause.ocr_required
    ) and version.review_status != "approved":
        raise ValueError("OCR 定义需要先核对原文")
    # Official status comes from an admitted official source site, not an arbitrary URL.
    from app.sources.models import SourceSite

    site = await session.get(SourceSite, doc.site_id)
    if site is None or site.terms_status != "approved":
        raise ValueError("官方渠道需要先完成来源确认")
    if source_kind == "official_parallel" and not applies_to_document_id:
        raise ValueError("官方语种对照须明确对应的法规文件")
    if source_kind == "law_definition" and not any(
        item["quote"] == quote and item["source_term"].casefold() == source_term.casefold()
        for item in definition_spans(
            clause.original_text, clause.heading or clause.section_path or ""
        )
    ):
        raise ValueError("法律定义须对应完整的显式定义条款，不可仅凭关键词登记")
    if source_kind == "law_definition" and _source_kind(doc) != "law_definition":
        raise ValueError("该来源资料不是法律文件，不可登记为法律自身定义")
    if source_kind != "international_reference" and not country_code:
        raise ValueError("本国来源须明确适用国家")
    if applies_to_document_id:
        target = await session.get(Document, applies_to_document_id)
        if target is None or (country_code and target.country_code != country_code.upper()):
            raise ValueError("对应文件与适用国家不一致")
    if source_kind != "international_reference" and doc.country_code != country_code.upper():
        raise ValueError("本国来源与适用国家不一致")
    code, _ = identity(source_term, clause.language, quote)
    start = clause.original_text.index(quote)
    fingerprint = sha(
        f"{clause.id}:{sha(clause.original_text)}:{start}:{quote}:{source_kind}:{country_code}:{applies_to_document_id}:{target_zh}"
    )
    await session.execute(
        insert(LegalTermDefinition)
        .values(
            id=uuid.uuid4(),
            concept_code=code,
            country_code=country_code.upper() if country_code else None,
            language=clause.language,
            source_term=source_term,
            target_zh=target_zh,
            definition_original=quote,
            source_kind=source_kind,
            source_url=doc.source_url,
            source_reference=(
                f"{doc.title} · {clause.clause_number or clause.ordinal} · v{version.version_no}"
            ),
            document_id=doc.id,
            version_id=version.id,
            clause_id=clause.id,
            applies_to_document_id=applies_to_document_id,
            quote_start=start,
            quote_end=start + len(quote),
            source_text_sha256=sha(clause.original_text),
            fingerprint=fingerprint,
            verification_status="source_matched",
            registration_origin="manual",
            created_by=actor_id,
        )
        .on_conflict_do_nothing(index_elements=["fingerprint"])
    )
    return await session.scalar(
        select(LegalTermDefinition).where(LegalTermDefinition.fingerprint == fingerprint)
    )


def term_payload(term):
    return {
        "id": str(term.id),
        "concept_code": term.concept_code,
        "country_code": term.country_code,
        "language": term.language,
        "source_term": term.source_term,
        "target_zh": term.target_zh,
        "definition_original": term.definition_original,
        "definition_zh": getattr(term, "definition_zh_display", None) or term.definition_zh,
        "source_url": term.source_url,
        "source_reference": term.source_reference,
        "source_kind": term.source_kind,
        "source_label": SOURCE_LABEL[term.source_kind],
        "document_id": str(term.document_id) if term.document_id else None,
        "version_id": str(term.version_id) if term.version_id else None,
        "clause_id": str(term.clause_id) if term.clause_id else None,
        "source_scope": getattr(term, "source_scope", "document"),
        "scope_path": getattr(term, "scope_path", ""),
        "verification_status": term.verification_status,
        "usage": "译名与阅读释义",
        "rendering_label": "暂译" if term.concept_code.startswith("term_") else "规范译名",
    }


async def register_critical_ambiguity(session, *, clause_id, source_term, question, impact):
    clause = await session.get(Clause, clause_id)
    if clause is None or source_term.casefold() not in clause.original_text.casefold():
        raise ValueError("术语必须出现在所关联的原文条款中")
    version = await session.get(DocumentVersion, clause.version_id)
    doc = await session.get(Document, version.document_id)
    if await document_is_held(session, doc.id) or await held_citation_ids(session, [clause.id]):
        raise ValueError("原文来源需要先由资料管理人员处理")
    definitions = await current_definitions(session, doc.country_code, doc.id)
    code, _ = identity(source_term, clause.language)
    matching = [
        t
        for t in definitions
        if t.concept_code == code or t.source_term.casefold() == source_term.casefold()
    ]
    if matching:
        priority = min(PRIORITY[t.source_kind] for t in matching)
        meanings = {
            " ".join(t.definition_original.split())
            for t in matching
            if PRIORITY[t.source_kind] == priority
        }
    else:
        meanings = set()
    if len(meanings) == 1:
        raise ValueError("已有来源定义，请先按具体法规与适用范围查询")
    fingerprint = sha(f"{doc.country_code}:{doc.id}:{source_term.casefold()}:{question}")[:40]
    from sqlalchemy import text

    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": int(fingerprint[:15], 16)}
    )
    point_code = "TERM-" + fingerprint
    point = await session.scalar(select(ReviewPoint).where(ReviewPoint.point_code == point_code))
    if point is None:
        point = ReviewPoint(
            country_code=doc.country_code,
            point_code=point_code,
            title=source_term + " 的适用含义",
            question=question,
            impact_on_conclusion=impact,
            provisional_interpretation="保留原词，等待适用含义确认",
            clause_id=clause.id,
            status="open",
        )
        session.add(point)
        await session.flush()
    return point
