"""Unseen sources/identities, not frozen evaluation questions or document IDs."""

import copy
from types import SimpleNamespace

import pytest

from app.analysis.source_alternatives import include_source_alternatives
from app.ingestion.parsing import parse_html
from app.ingestion.source_observations import observation_text, validate_observation

from .test_s5_quality_followup import candidate


@pytest.mark.parametrize("prefix", ["Article", "Section", "المادة", "第"])
def test_native_html_keeps_all_numbered_body_and_excludes_unrelated_navigation(prefix):
    headings = [f"{prefix}{n}条" if prefix == "第" else f"{prefix} {n}" for n in range(7, 10)]
    blocks = "".join(
        f"<h2>{h}</h2><p>source condition {n} and exception.</p>" for n, h in enumerate(headings)
    )
    html = f"<nav>menu</nav><article>other story<div>{blocks}</div>related story</article>"
    result = parse_html(html.encode())[0]
    assert "related story" not in result and "other story" not in result
    for n, h in enumerate(headings):
        assert h in result and f"source condition {n} and exception." in result


def test_plain_html_without_structure_still_preserves_source():
    assert parse_html(b"<p>Unknown source. All words remain.</p>") == [
        "Unknown source. All words remain."
    ]


def observation():
    version = SimpleNamespace(id="v", document_id="d", content_hash="a" * 64)
    doc = SimpleNamespace(id="d", country_code="XY", source_url="https://authority.example/source")
    data = dict(
        version_id="v",
        content_hash="a" * 64,
        country_code="XY",
        instrument_key="act-new",
        instrument_role="base",
        source_url=doc.source_url,
        publisher="Authority",
        source_nature="official_publication",
        source_excerpt="Original instrument identity.",
    )
    return data, version, doc


@pytest.mark.parametrize(
    "field,value",
    [
        ("content_hash", "b" * 64),
        ("version_id", "other"),
        ("country_code", "YZ"),
        ("source_url", "https://other.example/source"),
        ("human_reviewed", True),
        ("usable_as_legal_basis", True),
        ("source_excerpt", "Not in source at all today."),
    ],
)
def test_source_observation_does_not_authorize_identity_drift_or_approval(field, value):
    data, v, d = observation()
    data[field] = value
    with pytest.raises(ValueError):
        validate_observation(data, v, d, "Original instrument identity.")


def test_source_observation_allows_whitespace_only_without_legal_approval():
    data, v, d = observation()
    assert validate_observation(data, v, d, "Original instrument\nidentity.") == (
        "XY:act-new",
        "base",
    )


def row(cid, doc, label="19", identity="XY:act-new", role="base", section="PART A"):
    r = candidate(cid, "A full provision with an exception remains original.")
    r.document_id = doc
    r.label = label
    r.instrument_identity = identity
    r.instrument_role = role
    r.section_path = section
    return r


def test_alternates_occupy_real_slots_and_keep_distinct_source_identity():
    a, b, c = [row(i, i) for i in ("a", "b", "c")]
    c.instrument_identity = "XY:other"
    before = copy.deepcopy([a, b, c])
    out = include_source_alternatives([(a, 2), (c, 1)], [a, b, c], 2)
    assert [r.clause_id for r, _ in out] == ["a", "b"]
    assert [a, b, c] == before and a.document_id != b.document_id
    assert include_source_alternatives([(a, 2)], [a, b, c], 1) == [(a, 2)]


@pytest.mark.parametrize("change", ["country", "role", "section", "unproven", "annex", "label"])
def test_comparison_never_pairs_unproven_or_ambiguous_provisions(change):
    a, b = row("a", "doc-a"), row("b", "doc-b")
    rows = [a, b]
    if change == "country":
        b.instrument_identity = "YZ:act-new"
    if change == "role":
        b.instrument_role = "amendment"
    if change == "section":
        b.section_path = "SCHEDULE"
    if change == "unproven":
        b.instrument_identity = ""
    if change == "annex":
        rows.append(row("repeat", "doc-b", section="SCHEDULE"))
    if change == "label":
        b.label = "20"
    assert include_source_alternatives([(a, 2)], rows, 5) == [(a, 2)]


def test_alternative_number_normalization_does_not_touch_source_text():
    a, b = row("a", "doc-a", label="١٩"), row("b", "doc-b")
    assert len(include_source_alternatives([(a, 2)], [a, b], 5)) == 2
    assert a.label == "١٩"


def test_identity_uses_archived_title_while_clauses_exclude_page_chrome():
    blob = (
        "<h1>Official instrument identity outside legal body.</h1><article>"
        + "".join(
            f"<h2>Article {i}</h2><p>Unchanged source paragraph {i}.</p>" for i in range(3, 6)
        )
        + "</article>"
    ).encode()
    assert "Official instrument identity" in observation_text(blob, "text/html")
    assert "Official instrument identity" not in parse_html(blob)[0]


def test_observation_version_document_binding_is_mandatory():
    data, version, document = observation()
    version.document_id = "another-document"
    with pytest.raises(ValueError):
        validate_observation(data, version, document, "Original instrument identity.")


@pytest.mark.parametrize(
    "first,second",
    [
        ("PART IV / Division I", "Part 4 / Division 1"),
        ("CHAPTER XII", "Chapter 12"),
        ("PART IX / Division II", "Part 9 / Division 2"),
    ],
)
def test_structural_roman_and_decimal_ordinals_can_be_compared(first, second):
    a, b = row("one", "doc-1", section=first), row("two", "doc-2", section=second)
    assert len(include_source_alternatives([(a, 1)], [a, b], 5)) == 2
    assert a.section_path == first


def test_unknown_or_invalid_structural_name_is_not_guessed():
    from app.analysis.source_alternatives import section_key

    assert section_key("PART IIX") != section_key("Part 8")
    assert section_key("CIVIL LAW") == "civil law"


def test_republished_source_notice_survives_api_and_shared_export_reference():
    from app.analysis.report_exporter import _clause_ref
    from app.analysis.schemas import ReportCitationOut
    from app.analysis.source_alternatives import source_notice

    cite = ReportCitationOut(
        citation_tag="[D2-C1]",
        document_title="Original official title",
        clause_number="19",
        quote_original="Original legal clause remains unchanged.",
        quote_zh="待核对译文",
        review_status="pending_review",
        version_id="v",
        source_nature="statutory_body_republication",
        source_notice=source_notice("statutory_body_republication"),
    )
    assert "非原始公报" in _clause_ref(cite.model_dump())
    assert (
        cite.document_title == "Original official title" and cite.review_status == "pending_review"
    )


@pytest.mark.parametrize("digits", ["123456789", "١٢٣٤٥٦٧٨٩"])
def test_full_legal_list_is_one_anchor_but_all_coverage_items_remain(digits):
    from app.analysis.evidence_selection import coverage_units, select_legal_quote, sentence_catalog

    text = "The operator must disclose these matters:\n" + "\n".join(
        f"{d} – Complete obligation {i}." for i, d in enumerate(digits, 1)
    )
    catalog = sentence_catalog(text)
    assert len(catalog) == 1 and catalog[0]["text"] == text
    quote = select_legal_quote(text, [1])
    assert quote["quote"] == text and quote["selected_sentence_ids"] == [1]
    units = coverage_units(text)
    assert len(units) == 10 and "".join(u["text"] for u in units) == text


def test_list_selection_does_not_absorb_an_independent_provision():
    from app.analysis.evidence_selection import sentence_catalog

    text = "First rule:\n1 – Duty one.\n2 – Duty two.\nArticle 22.\nNew rule:\n1 – Duty three.\n2 – Duty four."
    catalog = sentence_catalog(text)
    assert len(catalog) >= 2
    assert all(not ("Duty one" in s["text"] and "Duty three" in s["text"]) for s in catalog)


def test_spaced_header_is_boundary_detection_only_and_six_anchor_limit_stays():
    from app.analysis.evidence_selection import (
        FindingSelection,
        SourceBoundaryError,
        select_legal_quote,
    )

    text = (
        "سياق كامل من المادة السابقة لا يجوز نقله.\nالم ادة ٧٨:\nهذا هو النص الكامل للمادة التالية."
    )
    with pytest.raises(SourceBoundaryError):
        select_legal_quote(text, [1, 2])
    assert "الم ادة" in text
    with pytest.raises(ValueError):
        FindingSelection.model_validate(
            dict(
                clause_id="x",
                sentence_ids=list(range(1, 8)),
                fact_key="q",
                obligation="unchanged",
                risk_level="MEDIUM",
            )
        )
