"""Unseen structures/languages; no frozen suite IDs or expected answers."""

import copy
import json
from types import SimpleNamespace

import pytest

from app.analysis import retrieval_cache, retrieval_planner
from app.analysis.evidence_selection import (
    CoveredClaim,
    assemble_covered_claim,
    coverage_units,
    select_legal_quote,
    sentence_catalog,
)
from app.analysis.llm import LlmResult
from app.analysis.retrieval import ClauseIndex
from app.analysis.source_context import fragment_links, include_context_roots

from .test_s5_quality_followup import candidate


@pytest.mark.parametrize("heading", ["Definitions", "Définitions", "تعاريف", "未知格式"])
def test_only_archive_proven_continuations_link_to_root(heading):
    a = candidate("root", "Opening source and first definition.", heading=heading)
    b = candidate("child", "Second full definition from continuation.", heading=heading)
    a.label, b.label = "17", "17.8"
    b.version_id = a.version_id
    a.section_path = b.section_path = "ANNEX B"
    draft = SimpleNamespace(clause_number="17", section_path="ANNEX B", text=a.text + "\n" + b.text)
    before = copy.deepcopy([a, b])
    assert fragment_links([a, b], [draft]) == {"child": "root"}
    # Real decimal numbering alone, another version/section or changed prefix proves nothing.
    assert fragment_links([a, b], []) == {}
    assert fragment_links([a, b], [draft, copy.deepcopy(draft)]) == {}
    assert [a, b] == before
    b.version_id = "other-version"
    assert fragment_links([a, b], [draft]) == {}
    b.version_id = a.version_id
    draft.text = "Different beginning " + b.text
    assert fragment_links([a, b], [draft]) == {}


def test_context_parent_uses_real_slot_and_never_substitutes_evidence():
    a, b, c = [candidate(x, x * 100) for x in ("a", "b", "c")]
    b.context_parent_id = "a"
    assert [r.clause_id for r, _ in include_context_roots([(b, 1), (c, 0.5)], [a, b, c], 2)] == [
        "b",
        "a",
    ]
    assert b.text == "b" * 100 and b.clause_id == "b"


async def test_cache_no_cross_scope_source_config_reuse_or_hidden_cost(monkeypatch):
    retrieval_cache._entries.clear()
    rows = [candidate("a", "Always protect confidential information.")]
    index = ClauseIndex(rows)
    config = dict(system_prompt="JSON", model="test", temperature=0.0, timeout=5.0)
    calls = []

    async def fake(messages, **kwargs):
        phase = json.loads(messages[1]["content"])["phase"]
        calls.append(phase)
        return LlmResult(
            {"queries": ["protect"], "heading_queries": ["protect"]}
            if phase == "plan"
            else {"clause_ids": ["a"]},
            20,
            10,
            1,
            "test",
        )

    monkeypatch.setattr(retrieval_planner, "call_llm_json", fake)

    async def run(scope="org-A", cfg=None):
        return await retrieval_planner.planned_search(
            index, "protect", "安全", "保护?", 1, cfg or config, cache_scope=scope
        )

    first, cold = await run()
    second, warm = await run()
    assert first == second and cold["tokens"] == 60
    assert warm["cache_hit"] and warm["tokens"] == warm["requests"] == 0
    assert warm["origin_tokens"] == 60 and warm["ledger"] == []
    assert len(calls) == 2
    await run("org-B")
    await run(cfg={**config, "temperature": 0.2})
    rows[0].review_status = "rejected"
    await run()
    assert len(calls) == 8
    key = cold["cache_key"]
    saved = retrieval_cache.get(key)
    saved[1]["tokens"] = 999
    assert retrieval_cache.get(key)[1]["tokens"] == 60
    monkeypatch.setattr(retrieval_cache, "TTL_SECONDS", -1)
    assert retrieval_cache.get(key) is None


@pytest.mark.parametrize(
    "source",
    [
        "Article 19: The following conditions apply.\n1 - Consent is required.\n2 - Research is exempt.",
        "Art. 44 : Le responsable agit. Sauf pour la recherche scientifique.",
        "المادة ١٩: يجب حماية البيانات.\n١ - تمنع الإتاحة.\n٢ - باستثناء البحث العلمي.",
        "第十九条 本条适用以下情形。\n1 - 需要同意。\n2 - 科研例外。",
    ],
)
def test_every_source_unit_is_retained_and_rendered_in_order(source):
    units = coverage_units(source)
    assert len(units) > 1 and "".join(u["text"] for u in units) == source
    good = {"units": [{"unit_id": u["unit_id"], "meaning": u["text"]} for u in units], "gap": ""}
    assert assemble_covered_claim(CoveredClaim.model_validate(good), units)
    for bad in (
        good["units"][:-1],
        list(reversed(good["units"])),
        good["units"] + [good["units"][0]],
    ):
        with pytest.raises(ValueError, match="遗漏、重复、乱序"):
            assemble_covered_claim(CoveredClaim(units=bad), units)


def test_unit_numeric_coverage_cannot_drop_either_range_end_or_borrow_from_other_units():
    units = coverage_units("The fine is 13 to 79 credits. The separate filing period is 8 days.")
    with pytest.raises(ValueError, match="遗漏数值"):
        assemble_covered_claim(
            CoveredClaim(
                units=[
                    {"unit_id": 1, "meaning": "罚金不低于13 credits"},
                    {"unit_id": 2, "meaning": "另有79 credits以及8 days"},
                ]
            ),
            units,
        )


@pytest.mark.parametrize(
    "marker", ["Article 83:", "Art. 83 :", "الفصل ٨٣ .", "المادة ٨٣:", "第八十三条"]
)
def test_no_cross_provision_quote_or_previous_tail_attribution(marker):
    source = (
        "Earlier independent provision ends here.\n"
        + marker
        + " The applicant must notify the authority."
    )
    catalog = sentence_catalog(source)
    with pytest.raises(ValueError, match="跨越独立条文"):
        select_legal_quote(source, list(range(1, len(catalog) + 1)))
    selected = select_legal_quote(source, [len(catalog)])
    assert "Earlier independent" not in selected["quote"]


@pytest.mark.parametrize(
    "heading,marker",
    [
        ("Notice requirements", "Article 42:"),
        ("Exigences de déclaration", "Art. 42 :"),
        ("إجراءات الإخطار", "المادة ٤٢:"),
        ("告知要求", "第四十二条"),
    ],
)
def test_exact_source_title_and_own_number_are_not_two_provisions(heading, marker):
    source = heading + "\n" + marker + " Operators must provide notice before processing."
    ids = list(range(1, len(sentence_catalog(source)) + 1))
    assert select_legal_quote(source, ids, heading=heading)["quote"] == source
    with pytest.raises(ValueError, match="跨越独立条文"):
        select_legal_quote(source, ids, heading="Another title")
    extended = source + "\nArticle 43: A separate requirement applies."
    with pytest.raises(ValueError, match="跨越独立条文"):
        select_legal_quote(
            extended, list(range(1, len(sentence_catalog(extended)) + 1)), heading=heading
        )


async def test_multiunit_generation_receives_source_once_and_preserves_coverage_audit(monkeypatch):
    from app.analysis import pipeline
    from app.analysis.models import AnalysisTask

    source = "Operators must provide notice. Except for purely private use."
    row = candidate("x", source)
    seen = []

    async def fake(messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        seen.append(payload)
        assert payload["evidence"] == {"format": "source_units"}
        assert "quote" not in payload["evidence"]
        assert "".join(u["text"] for u in payload["source_units"]) == source
        return LlmResult(
            {
                "units": [
                    {"unit_id": 1, "meaning": "经营者必须提供告知。"},
                    {"unit_id": 2, "meaning": "纯私人用途除外。"},
                ],
                "gap": "",
                "risk_level": "MEDIUM",
            },
            12,
            3,
            1,
            "test",
        )

    monkeypatch.setattr(pipeline, "call_llm_json", fake)
    result, audit = await pipeline._ground_selection(
        AnalysisTask(facts_json={"x": "处理数据"}),
        "D8",
        "安全",
        {"clause_id": "x", "fact_key": "x", "sentence_ids": [1, 2]},
        [row],
        dict(system_prompt="JSON", model="test", temperature=0.0, timeout=2.0),
    )
    assert result["quote"] == source and "除外" in result["obligation"]
    assert result["source_coverage"]["human_review"] is None
    assert audit["tokens"] == 15 and len(audit["proposals"]) == 1 and len(seen) == 1


async def test_graph_vocabulary_adds_unseen_source_without_becoming_evidence(monkeypatch):
    rows = [candidate(str(i), "Consent and information data." * 5) for i in range(120)]
    target = candidate(
        "new-role", "The company shall appoint an individual to supervise compliance."
    )
    target.search_hints = "合规监督人员指定职责"
    rows.append(target)

    async def fake(messages, **kwargs):
        p = json.loads(messages[1]["content"])
        if p["phase"] == "plan":
            out = {"queries": ["consent information"], "heading_queries": ["consent"]}
        else:
            assert "new-role" in {x["clause_id"] for x in p["candidates"]}
            chosen = next(x for x in p["candidates"] if x["clause_id"] == "new-role")
            assert chosen["source"] == target.text and "search_hints" not in chosen
            assert target.search_hints not in messages[1]["content"]
            out = {"clause_ids": ["new-role"]}
        return LlmResult(out, 10, 2, 1, "test")

    monkeypatch.setattr(retrieval_planner, "call_llm_json", fake)
    found, audit = await retrieval_planner.planned_search(
        ClauseIndex(rows),
        "consent",
        "组织要求",
        "合规监督人员如何指定？",
        1,
        dict(system_prompt="JSON", model="test", temperature=0.0, timeout=5.0),
    )
    assert found[0][0].clause_id == "new-role" and audit["requests"] == 2


@pytest.mark.parametrize(
    "variant",
    [
        "pending",
        "approved",
        "rejected",
        "superseded",
        "node_rejected",
        "wrong_quote",
        "unjudged",
        "rule_engine",
    ],
)
async def test_graph_hint_eligibility_never_reuses_bad_or_retired_edges(session_factory, variant):
    from app.analysis.tools import ToolBox

    from .review_s1_s2_contracts import make_document, make_edge

    async with session_factory() as session:
        doc, ver, clauses = await make_document(session)
        doc.title = "A newly registered source"
        edge, _, target = await make_edge(session, clauses[0])
        target.name_zh = "专用保密责任"
        if variant in ("approved", "rejected", "superseded"):
            edge.review_status = variant
        elif variant == "node_rejected":
            target.review_status = "rejected"
        elif variant == "wrong_quote":
            edge.evidence_quote = "not in the source"
        elif variant == "unjudged":
            edge.judge_verdict = "no"
        elif variant == "rule_engine":
            edge.extractor_model = "kg-rule-extractor-v1"
        await session.flush()
        before = (
            edge.review_status,
            target.review_status,
            clauses[0].original_text,
            ver.review_status,
        )
        rows = await ToolBox(session, doc.country_code)._load_candidates()
        assert bool(rows[0].search_hints) == (variant in ("pending", "approved"))
        assert before == (
            edge.review_status,
            target.review_status,
            clauses[0].original_text,
            ver.review_status,
        )


async def test_underfilled_ranking_uses_existing_local_candidates_and_keeps_uncertainty(
    monkeypatch,
):
    rows = [
        candidate(str(i), "A duty to protect records applies to the operator.") for i in range(6)
    ]

    async def fake(messages, **kwargs):
        p = json.loads(messages[1]["content"])
        data = (
            {"queries": ["protect records"], "heading_queries": ["duty"]}
            if p["phase"] == "plan"
            else {"clause_ids": [p["candidates"][-1]["clause_id"]]}
        )
        return LlmResult(data, 2, 1, 1, "test")

    monkeypatch.setattr(retrieval_planner, "call_llm_json", fake)
    found, audit = await retrieval_planner.planned_search(
        ClauseIndex(rows),
        "duty",
        "职责",
        "protect records?",
        5,
        dict(system_prompt="JSON", model="test", temperature=0.0, timeout=3.0),
    )
    assert len(found) == 5 and len({c.clause_id for c, _ in found}) == 5
    assert audit["model_ranked_count"] == 1 and audit["local_fill_count"] == 4
    assert all(
        c.review_status == "pending_review" and c.legal_status == "unknown" for c, _ in found
    )
    assert audit["requests"] == 2 and audit["tokens"] == 6
