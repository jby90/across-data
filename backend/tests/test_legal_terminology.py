"""Synthetic multilingual/version fixtures, isolated from all business records."""

import uuid

import pytest
from sqlalchemy import func, select

from app.ingestion.models import Clause, Document, DocumentVersion
from app.knowledge.legal_terms import (
    current_definitions,
    definition_spans,
    rebuild_library,
    register_critical_ambiguity,
    register_reference,
    sha,
    translation_terms,
)
from app.knowledge.models import GlossaryTerm, LegalTermDefinition, ReviewPoint
from app.knowledge.translator import (
    _build_translation_prompts,
    match_clause_glossary,
    verify_translation,
)
from app.sources.models import SourceSite

from .conftest import bearer, login


async def source(
    s,
    text,
    *,
    country="XY",
    language="en",
    kind="LAW",
    document=None,
    number=1,
    ocr=False,
    heading="Definitions",
):
    if document is None:
        site = SourceSite(
            country_code=country,
            publisher="Synthetic official fixture",
            language=language,
            document_type=kind,
            entry_url="https://authority.invalid/" + str(uuid.uuid4()),
            strategy="DIRECT",
            terms_status="approved",
            enabled=False,
        )
        s.add(site)
        await s.flush()
        document = Document(
            site_id=site.id,
            country_code=country,
            language=language,
            title="Synthetic law",
            document_type=kind,
            source_url=site.entry_url,
        )
        s.add(document)
        await s.flush()
    v = DocumentVersion(
        document_id=document.id,
        version_no=number,
        content_hash=sha(text),
        archive_path="fixture",
        size_bytes=len(text.encode()),
        mime_type="application/pdf",
        final_url=document.source_url,
        state="parsed",
        text_source="ocr" if ocr else "text_layer",
    )
    s.add(v)
    await s.flush()
    c = Clause(
        version_id=v.id,
        ordinal=1,
        clause_number="2",
        language=language,
        original_text=text,
        text_hash=sha(text),
        heading=heading,
        ocr_required=ocr,
    )
    s.add(c)
    await s.flush()
    return document, v, c


@pytest.mark.parametrize(
    "text,heading,term",
    [
        (
            "“controller” means a person deciding the purposes of personal data processing.",
            "",
            "controller",
        ),
        (
            "«responsable du traitement» désigne une personne déterminant les finalités du traitement.",
            "",
            "responsable du traitement",
        ),
        (
            "المتحكم: الشخص الذي يحدد أغراض معالجة البيانات الشخصية ووسائلها.",
            "التعريفات",
            "المتحكم",
        ),
    ],
)
def test_explicit_definition_offsets_are_exact(text, heading, term):
    rows = definition_spans(text, heading)
    assert len(rows) == 1 and rows[0]["source_term"] == term
    assert rows[0]["quote"] == text[rows[0]["start"] : rows[0]["end"]]


@pytest.mark.parametrize(
    "text,heading",
    [
        ("Controller: inspect the engine and vehicle braking system.", "Operations"),
        ("“controller” means a person deciding the purposes of personal da", "Definitions"),
        ("definitions: ...", "Definitions"),
        ("The authority means to issue rules about controllers.", "Powers"),
    ],
)
def test_no_guessed_or_incomplete_definitions(text, heading):
    assert definition_spans(text, heading) == []


def test_nested_amendment_quotes_do_not_become_part_of_terms_or_definitions():
    text = (
        "“(2) In paragraph (1), “ongoing relationship” means a continuing business relationship.; "
        "“applicable purpose” means a purpose in Schedule 10.;”; "
        "(b) delete “old expression” and insert “new expression”."
    )
    rows = definition_spans(text, "Interpretation")
    assert [r["source_term"] for r in rows] == ["ongoing relationship", "applicable purpose"]
    assert rows[1]["quote"] == "“applicable purpose” means a purpose in Schedule 10.;"
    assert all(r["quote"] == text[r["start"] : r["end"]] for r in rows)


def test_adjacent_definition_names_are_not_absorbed_by_previous_quote():
    text = (
        "“appeal” means a request under section 4; "
        "“Appeal Panel” means a panel constituted under section 5; "
        "“authorised person” means a person exercising the delegated power."
    )
    rows = definition_spans(text, "Definitions")
    assert [r["source_term"] for r in rows] == ["appeal", "Appeal Panel", "authorised person"]
    assert "Panel" not in rows[0]["quote"]
    assert "authorised" not in rows[1]["quote"]


def test_scoped_and_cross_referenced_definitions_end_previous_definition():
    text = (
        "“applicable country” means a country prescribed in the regulations; "
        "“applicable data”, in relation to an organisation, means personal data in its control; "
        "“data request” has the meaning given by section 9."
    )
    rows = definition_spans(text, "Definitions")
    assert [r["source_term"] for r in rows] == [
        "applicable country",
        "applicable data",
        "data request",
    ]
    assert "applicable data" not in rows[0]["quote"]
    assert "data request" not in rows[1]["quote"]


async def test_append_only_version_scope_and_hash_guard(session_factory):
    async with session_factory() as s:
        d, v, c = await source(
            s, "“controller” means a person deciding the purposes of personal data processing."
        )
        legacy = GlossaryTerm(
            concept_code="data_controller",
            country_code="XY",
            language="en",
            source_term="controller",
            target_zh="旧译法",
            priority_level=1,
            review_status="approved",
        )
        s.add(legacy)
        await s.flush()
        before = (c.original_text, legacy.review_status, legacy.target_zh)
        result = await rebuild_library(s)
        assert result["definitions_added"] == 1 and result["model_calls"] == 0
        assert (await rebuild_library(s))["definitions_added"] == 0
        assert len(await current_definitions(s, "XY", d.id)) == 1
        assert not await current_definitions(s, "YZ", d.id)
        assert (c.original_text, legacy.review_status, legacy.target_zh) == before
        _, v2, c2 = await source(s, "A new provision has no defined terms.", document=d, number=2)
        assert not await current_definitions(s, "XY", d.id)
        assert len(await current_definitions(s, "XY", d.id, v.id)) == 1
        # A changed source invalidates the index; no silent quote repair.
        c.original_text += " Changed."
        await s.flush()
        assert not await current_definitions(s, "XY", d.id, v.id)
        assert await s.scalar(select(func.count()).select_from(LegalTermDefinition)) == 1


async def test_priority_context_and_strict_rendering(session_factory):
    async with session_factory() as s:
        d, v, c = await source(
            s, "“controller” means a person deciding the purposes of personal data processing."
        )
        _, _, guide = await source(
            s, "“controller” means the organisation described in this guide.", kind="GUIDANCE"
        )
        await rebuild_library(s)
        terms = await translation_terms(s, c, "XY", [])
        controller = next(t for t in terms if t.concept_code == "data_controller")
        assert controller.priority_level == 1 and str(c.id) != controller.definition_id
        hits, _ = match_clause_glossary(c.original_text, terms)
        hit = next(h for h in hits if h["concept_code"] == "data_controller")
        assert hit["source_kind"] == "law_definition" and "Synthetic law" in hit["source_reference"]
        assert "来源释义" in _build_translation_prompts(c.original_text, "en", hits, [])[0]
        assert not verify_translation(
            "controller processes personal data", "数据控制者应处理个人数据，也叫控制器。", hits
        )[0]
        _, _, hardware = await source(
            s, "The controller checks the vehicle processor and brakes.", heading="Hardware"
        )
        hardware_terms = await translation_terms(s, hardware, "XY", [])
        # Country-wide guides must not convert hardware into a privacy actor.
        hw_hits, _ = match_clause_glossary(hardware.original_text, hardware_terms)
        assert not any(h["concept_code"] in {"data_controller", "data_processor"} for h in hw_hits)


async def test_ocr_gap_and_material_ambiguities_only(session_factory):
    async with session_factory() as s:
        _, _, c = await source(s, "“unknown actor” means a person handling information.", ocr=True)
        assert (await rebuild_library(s))["definitions_added"] == 0
        d, v, c2 = await source(
            s, "Vehicle telemetry can identify an individual in some circumstances."
        )
        point = await register_critical_ambiguity(
            s,
            clause_id=c2.id,
            source_term="telemetry",
            question="该场景的 telemetry 是否属于个人数据？",
            impact="会影响该业务的告知与传输义务。",
        )
        again = await register_critical_ambiguity(
            s,
            clause_id=c2.id,
            source_term="telemetry",
            question=point.question,
            impact=point.impact_on_conclusion,
        )
        assert again.id == point.id and point.status == "open"
        assert await s.scalar(select(func.count()).select_from(ReviewPoint)) == 1
        with pytest.raises(ValueError):
            await register_critical_ambiguity(
                s,
                clause_id=c2.id,
                source_term="invented term",
                question="虚构术语？",
                impact="不能进入确认清单。",
            )


async def test_reference_hierarchy_is_explicit_and_no_manual_approval(session_factory):
    async with session_factory() as s:
        d, v, c = await source(
            s, "“controller” means the body deciding purposes of personal data processing."
        )
        quote = c.original_text
        with pytest.raises(ValueError, match="对应"):
            await register_reference(
                s,
                clause_id=c.id,
                source_term="controller",
                target_zh="数据控制者",
                quote=quote,
                source_kind="official_parallel",
                country_code="XY",
            )
        ref = await register_reference(
            s,
            clause_id=c.id,
            source_term="controller",
            target_zh="数据控制者",
            quote=quote,
            source_kind="international_reference",
        )
        assert ref.country_code is None and ref.verification_status == "source_matched"
        assert (await current_definitions(s, "YZ"))[0].id == ref.id
        with pytest.raises(ValueError, match="逐字"):
            await register_reference(
                s,
                clause_id=c.id,
                source_term="controller",
                target_zh="数据控制者",
                quote="a fabricated definition",
                source_kind="international_reference",
            )
        assert v.review_status == "pending_review" and c.needs_review


async def test_term_routes_side_auth_and_automatic_readiness(client, seed, session_factory):
    async with session_factory() as s:
        await source(s, "“personal data” means information about an identifiable individual.")
        await s.commit()
    user = bearer(await login(client, "user", "a-member@example.com"))
    admin = bearer(await login(client, "admin", "admin@example.com"))
    url = "/api/admin/knowledge/terminology/rebuild"
    assert (await client.post(url, headers=user, json={})).status_code in (401, 403)
    assert (await client.post(url, headers=admin, json={})).json()["definitions_added"] == 1
    rows = (await client.get("/api/knowledge/terminology?country_code=XY", headers=user)).json()[
        "items"
    ]
    assert any(r["source_kind"] == "law_definition" and r["target_zh"] == "个人数据" for r in rows)
    assert (await client.get("/api/knowledge/terminology?country_code=XY")).status_code == 401


async def test_parallel_language_definition_is_scoped(session_factory):
    async with session_factory() as s:
        target, _, clause = await source(
            s,
            "Le responsable du traitement protège les données à caractère personnel.",
            language="fr",
        )
        _, _, reference = await source(
            s, "“controller” means the person deciding purposes of personal data processing."
        )
        term = await register_reference(
            s,
            clause_id=reference.id,
            source_term="controller",
            target_zh="数据控制者",
            quote=reference.original_text,
            source_kind="official_parallel",
            country_code="XY",
            applies_to_document_id=target.id,
        )
        selected = await translation_terms(s, clause, "XY", [])
        hits, _ = match_clause_glossary(clause.original_text, selected)
        hit = next(h for h in hits if h["concept_code"] == "data_controller")
        assert hit["definition_id"] == str(term.id) and hit["source_kind"] == "official_parallel"
        other, _, _ = await source(
            s,
            "Le responsable du traitement protège les données à caractère personnel.",
            language="fr",
        )
        assert term.id not in {t.id for t in await current_definitions(s, "XY", other.id)}


def test_definition_stops_before_article_and_chapter():
    raw = (
        "Art. 6 : on entend par :\n - responsable du traitement : "
        "Toute personne traitant des données personnelles.\nCHAPITRE II\nAutres dispositions"
    )
    rows = definition_spans(raw)
    assert len(rows) == 1 and rows[0]["source_term"] == "responsable du traitement"
    assert "CHAPITRE" not in rows[0]["quote"]
    raw = (
        "Au sens de la loi, on entend par données personnelles toutes les informations identifiantes.\n"
        "Article 5 : Une autre règle."
    )
    rows = definition_spans(raw)
    assert len(rows) == 1 and rows[0]["source_term"] == "données personnelles"
    assert rows[0]["quote"].endswith("identifiantes.")


async def test_local_definition_does_not_escape_clause_and_physical_controller_is_not_a_privacy_role(
    session_factory,
):
    async with session_factory() as s:
        d, v, definition = await source(
            s,
            "For the purposes of this section, “controller” means an embedded unit controlling vehicle brakes.",
        )
        await rebuild_library(s)
        body = "The controller processes personal data before operating the vehicle."
        other = Clause(
            version_id=v.id,
            ordinal=2,
            clause_number="3",
            original_text=body,
            text_hash=sha(body),
            language="en",
        )
        s.add(other)
        await s.flush()
        local = await translation_terms(s, definition, "XY", [])
        raw = next(t for t in local if t.source_term == "controller")
        assert raw.target_zh == "controller" and raw.preserve_original
        definitions = await current_definitions(s, "XY", d.id)
        assert definitions[0].source_scope == "clause"
        # The local definition cannot be reused in another provision.
        assert raw.id not in {t.id for t in await translation_terms(s, other, "XY", [])}


async def test_source_bound_rendering_registration_keeps_old_definition(session_factory):
    async with session_factory() as s:
        d, v, c = await source(
            s, "“personal data” means information about an identifiable individual."
        )
        await rebuild_library(s)
        old = (await current_definitions(s, "XY", d.id))[0]
        term = await register_reference(
            s,
            clause_id=c.id,
            source_term="personal data",
            target_zh="个人信息",
            quote=c.original_text,
            source_kind="law_definition",
            country_code="XY",
        )
        assert term.registration_origin == "manual"
        current = await current_definitions(s, "XY", d.id)
        assert len(current) == 1 and current[0].target_zh == "个人信息"
        assert await s.get(LegalTermDefinition, old.id) is old
        assert (
            old.target_zh == "个人数据" and v.review_status == "pending_review" and c.needs_review
        )
        selected = await translation_terms(s, c, "XY", [])
        assert (
            next(t for t in selected if t.concept_code == "personal_data").target_zh == "个人信息"
        )


def test_aliases_match_pdf_whitespace_and_arabic_clitics_without_source_rewriting():
    from app.knowledge.legal_terms import TranslationTerm

    def t(language, source_term, target):
        return TranslationTerm(
            uuid.uuid4(), "test", None, language, source_term, target, 4, None, None
        )

    raw = "Protect personal\ndata within 3 days."
    hits, _ = match_clause_glossary(raw, [t("en", "personal data", "个人数据")])
    assert hits[0]["source_term"] == "personal\ndata"
    assert raw == "Protect personal\ndata within 3 days."
    raw = "وللمتحكم واجبات لحماية البيانات الشخصية."
    hits, _ = match_clause_glossary(raw, [t("ar", "المتحكم", "数据控制者")])
    assert hits[0]["source_term"] == "وللمتحكم"
