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
