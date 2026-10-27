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
    "marker",
    [
        "Article 83:",
        "Article. 83 :",
        "Section. 83:",
        "Art. 83 :",
        "الفصل ٨٣ .",
        "المادة ٨٣:",
        "第八十三条",
    ],
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


@pytest.mark.parametrize("prefix", ["Article.", "Section.", "Regulation.", "Rule."])
def test_numbered_heading_abbreviation_never_becomes_an_orphan_unit(prefix):
    source = prefix + " 73 : The provider shall notify the customer before disclosure."
    units = coverage_units(source)
    assert len(units) == 1 and units[0]["text"] == source


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


@pytest.mark.parametrize("changed", [True, False])
async def test_source_support_repair_preserves_bad_draft_and_never_fishes_same_claim(
    monkeypatch, changed
):
    from app.analysis import evaluation, pipeline
    from app.analysis.models import AnalysisTask

    source = "The holder shall notify its representative before disclosure."
    row = candidate("unseen", source)
    writes, reviews = [], []

    async def fake(messages, **kwargs):
        p = json.loads(messages[1]["content"])
        if p["phase"] == "verify_source_support":
            reviews.append(p)
            assert set(p) == {"phase", "quote", "conclusion", "contract", "output_schema"}
            assert p["quote"] == source and "business_fact" not in p
            output = (
                {"supported": "yes", "reason": "其代表与原文its一致"}
                if "其代表" in p["conclusion"]
                else {"supported": "partial", "reason": "its不能泛化为任何代表"}
            )
        else:
            writes.append(p)
            output = {
                "obligation": "持有人在披露前须通知"
                + ("其代表" if changed and len(writes) > 1 else "任何代表"),
                "gap": "",
                "risk_level": "MEDIUM",
            }
        return LlmResult(output, 10, 2, 1, "test")

    monkeypatch.setattr(pipeline, "call_llm_json", fake)
    result, audit = await pipeline._ground_selection(
        AnalysisTask(facts_json={"q": "披露资料"}),
        "D4",
        "告知",
        {"clause_id": "unseen", "fact_key": "q", "sentence_ids": [1]},
        [row],
        dict(
            system_prompt="JSON",
            model="test",
            temperature=0.0,
            timeout=5.0,
            verify_source_support=True,
        ),
    )
    assert len(writes) == 2 and len(reviews) == (2 if changed else 1)
    assert audit["tokens"] == 12 * (4 if changed else 3)
    assert audit["requests"] == (4 if changed else 3)
    attempted = evaluation.attempted_findings({"findings": [result]}, None)
    assert len(attempted) == 2
    pipeline._verify_dimension("D4", {"findings": attempted}, [row])
    assert attempted[0]["_program_ok"] and not attempted[0]["_ok"]
    assert attempted[1]["_ok"] == changed
    assert all(f["_program_ok"] for f in attempted)
    assert result["source_support"]["human_review"] is None


async def test_source_support_outage_fails_closed_and_keeps_known_cost(monkeypatch):
    from app.analysis import pipeline
    from app.analysis.llm import LlmUnavailableError
    from app.analysis.models import AnalysisTask

    row = candidate("safe", "The operator must protect confidential records from disclosure.")

    async def fake(messages, **kwargs):
        if json.loads(messages[1]["content"])["phase"] == "verify_source_support":
            raise LlmUnavailableError("unavailable", tokens=3, failures=[{"kind": "HTTP"}])
        return LlmResult(
            {"obligation": "经营者须保护保密记录免遭披露", "gap": "", "risk_level": "MEDIUM"},
            4,
            2,
            1,
            "test",
        )

    monkeypatch.setattr(pipeline, "call_llm_json", fake)
    result, audit = await pipeline._ground_selection(
        AnalysisTask(facts_json={"q": "业务"}),
        "D8",
        "保护",
        {"clause_id": "safe", "fact_key": "q", "sentence_ids": [1]},
        [row],
        dict(
            system_prompt="JSON",
            model="test",
            temperature=0.0,
            timeout=5.0,
            verify_source_support=True,
        ),
    )
    assert result["_generation_error"] and audit["tokens"] == 9 and audit["requests"] == 2


async def test_cross_article_selection_is_reselected_without_paying_to_rewrite_invalid_quote(
    monkeypatch,
):
    from app.analysis import pipeline
    from app.analysis.models import AnalysisTask

    source = (
        "Article 16: Operators must give notice.\nArticle 17: Individuals may withdraw consent."
    )
    row = candidate("mixed", source)
    selections = []

    async def fake(messages, **kwargs):
        p = json.loads(messages[1]["content"])
        if p["phase"] == "select_evidence":
            selections.append(p)
            out = {
                "findings": [
                    {
                        "clause_id": "mixed",
                        "fact_key": "q",
                        "sentence_ids": [1, 2] if len(selections) == 1 else [1],
                        "obligation": "待依据生成",
                        "risk_level": "MEDIUM",
                    }
                ]
            }
        else:
            out = {"obligation": "Article 16：经营者必须告知", "gap": "", "risk_level": "MEDIUM"}
        return LlmResult(out, 2, 1, 1, "test")

    monkeypatch.setattr(pipeline, "call_llm_json", fake)
    result, call = await pipeline._analyse_dimension(
        AnalysisTask(facts_json={"q": "处理资料"}, country_code="XY"),
        "D4",
        "告知",
        [row],
        None,
        dict(system_prompt="JSON", model="test", temperature=0.0, timeout=5.0),
    )
    assert len(selections) == 2 and result["tokens"] == 9
    previous = call.arguments["selection_retries"][0]["findings"][0]
    assert previous["_selection_error"] and not previous.get("_generation_gap")
    assert pipeline._verify_dimension("D4", result, [row]) == []


def test_source_support_gate_cannot_be_disabled_by_import():
    from app.analysis.workflow_engine import DEFAULT_WORKFLOW_EDGES
    from app.analysis.workflow_runtime import baseline_nodes, validate_graph

    nodes = baseline_nodes()
    next(n for n in nodes if n["node_id"] == "node_dimension_analyst")["parameters"][
        "verify_source_support"
    ] = False
    with pytest.raises(ValueError):
        validate_graph(nodes, copy.deepcopy(DEFAULT_WORKFLOW_EDGES))


@pytest.mark.parametrize("fail", [False, True])
async def test_concurrent_identical_retrieval_is_billed_once_and_failure_is_not_cached(
    monkeypatch, fail
):
    import asyncio

    from app.analysis.llm import LlmUnavailableError

    retrieval_cache._entries.clear()
    rows = [candidate("same", "The operator must protect confidential records.")]
    calls = []

    async def fake(messages, **kwargs):
        phase = json.loads(messages[1]["content"])["phase"]
        calls.append(phase)
        await asyncio.sleep(0.02)
        if fail:
            raise LlmUnavailableError("outage", tokens=6, failures=[{"kind": "HTTP"}])
        return LlmResult(
            {"queries": ["protect"], "heading_queries": ["protect"]}
            if phase == "plan"
            else {"clause_ids": ["same"]},
            4,
            2,
            1,
            "test",
        )

    monkeypatch.setattr(retrieval_planner, "call_llm_json", fake)

    async def one():
        return await retrieval_planner.planned_search(
            ClauseIndex(copy.deepcopy(rows)),
            "protect",
            "保护",
            "保护?",
            1,
            dict(system_prompt="JSON", model="test", temperature=0.0, timeout=5.0),
            cache_scope="same-org",
        )

    results = await asyncio.gather(*[one() for _ in range(10)])
    assert len(calls) == (1 if fail else 2)
    assert sum(a["tokens"] for _, a in results) == (6 if fail else 12)
    assert sum(a["requests"] for _, a in results) == (1 if fail else 2)
    assert sum(a.get("coalesced", False) for _, a in results) == 9
    if fail:
        assert all(a["error"] for _, a in results) and not retrieval_cache._entries
        await one()
        assert len(calls) == 2


@pytest.mark.parametrize("duplicate", [False, True, "gap"])
async def test_retry_keeps_passed_evidence_and_only_spends_on_failed_items(monkeypatch, duplicate):
    from app.analysis import evaluation, pipeline
    from app.analysis.models import AnalysisTask

    rows = [
        candidate("stable", "The operator must protect confidential records from disclosure."),
        candidate(
            "broken", "The subject and condition cannot be identified in this incomplete fragment."
        ),
        candidate("replacement", "The operator must notify the customer before disclosure."),
    ]
    selections = []
    writes = []

    async def fake(messages, **kwargs):
        p = json.loads(messages[1]["content"])
        if p["phase"] == "select_evidence":
            selections.append(p)
            ids = (
                ["stable", "broken"]
                if len(selections) == 1
                else ["stable" if duplicate else "replacement"]
            )
            if len(selections) > 1:
                assert p["output_schema"]["properties"]["findings"]["maxItems"] == 1
                assert p["repair_budget"]["max_new_findings"] == 1
                assert p["repair_budget"]["allowed_fact_keys"] == ["q"]
                assert p["retained_evidence"][0]["clause_id"] == "stable"
            output = {
                "findings": [
                    {
                        "clause_id": i,
                        "sentence_ids": [1],
                        "fact_key": "q",
                        "obligation": "待依据生成",
                        "risk_level": "MEDIUM",
                    }
                    for i in ids
                ]
            }
            if duplicate == "gap" and len(selections) > 1:
                output = {"findings": [], "gaps": "原文关键条件仍缺失"}
        else:
            text = p["evidence"]["quote"]
            writes.append(text)
            output = (
                {"obligation": "", "gap": "缺少主语条件", "risk_level": "MEDIUM"}
                if "incomplete fragment" in text
                else {
                    "obligation": "经营者必须保护保密记录免遭披露"
                    if "confidential" in text
                    else "经营者须在披露前告知客户",
                    "gap": "",
                    "risk_level": "MEDIUM",
                }
            )
        return LlmResult(output, 2, 1, 1, "test")

    monkeypatch.setattr(pipeline, "call_llm_json", fake)
    result, call = await pipeline._analyse_dimension(
        AnalysisTask(country_code="XY", facts_json={"q": "披露资料"}),
        "D4",
        "告知",
        rows,
        None,
        dict(system_prompt="JSON", model="test", temperature=0.0, timeout=5.0),
    )
    assert writes.count(rows[0].text) == 1
    assert result["findings"][0]["clause_id"] == "stable"
    assert len(result["findings"]) == (1 if duplicate else 2)
    assert bool(result.get("error")) == (duplicate is True)
    if duplicate == "gap":
        assert result["repair_unresolved"] and not call.verified
        assert pipeline._verify_dimension("D4", result, rows)
    attempts = evaluation.attempted_findings(result, call.as_dict())
    assert sum(f["clause_id"] == "stable" for f in attempts) == 1
    assert any(f.get("_generation_gap") for f in attempts)
    if not duplicate:
        assert result["evidence_selections"][0]["retained"]
        assert pipeline._verify_dimension("D4", result, rows) == []


async def test_repair_budget_is_explicit_without_retained_items_and_overflow_still_rejected(
    monkeypatch,
):
    from app.analysis import pipeline
    from app.analysis.models import AnalysisTask

    rows = [
        candidate("a", "The operator must provide notice."),
        candidate("b", "The operator must protect records."),
    ]
    calls = []

    async def oversized(messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        calls.append(payload)
        assert payload["phase"] == "select_evidence"  # Never ground an over-budget selection.
        assert payload["repair_budget"]["max_new_findings"] == 1
        assert payload["repair_budget"]["allowed_fact_keys"] == ["q"]
        assert payload["output_schema"]["properties"]["findings"]["maxItems"] == 1
        assert set(payload["business_facts"]) == {"q"}
        return LlmResult(
            {
                "findings": [
                    {
                        "clause_id": c.clause_id,
                        "sentence_ids": [1],
                        "fact_key": "q",
                        "obligation": "待依据生成",
                        "risk_level": "LOW",
                    }
                    for c in rows
                ]
            },
            2,
            1,
            1,
            "synthetic-overflow",
        )

    monkeypatch.setattr(pipeline, "call_llm_json", oversized)
    result, call = await pipeline._analyse_dimension(
        AnalysisTask(country_code="QZ", facts_json={"q": "synthetic fact", "other": "unused"}),
        "D8",
        "整改建议",
        rows,
        ["修复已有失败项"],
        dict(system_prompt="JSON", model="test", temperature=0.0, timeout=5.0),
        _repair_limit=1,
        _repair_keys={"q"},
    )
    assert len(calls) == 2 and result["error_kind"] == "model_format_error"
    assert result["findings"] == [] and not call.verified
    assert result["tokens"] == 6
    assert "实际返回 2 项" in result["error"]


@pytest.mark.parametrize("drift", ["text", "anchor"])
async def test_retained_source_hash_drift_is_blocked_before_any_model_call(monkeypatch, drift):
    from app.analysis import pipeline
    from app.analysis.models import AnalysisTask

    row = candidate(
        "a",
        "The operator must provide notice before disclosure. A separate permission is required for onward transfer.",
    )
    retained = {
        "clause_id": "a",
        "fact_key": "q",
        "obligation": "经营者须在披露前告知",
        **select_legal_quote(row.text, [1]),
    }
    if drift == "text":
        row.text = "The operator must not provide this information before disclosure."
    else:
        retained["selected_sentence_ids"] = [2]

    async def forbidden(*a, **k):
        raise AssertionError("must not call model")

    monkeypatch.setattr(pipeline, "call_llm_json", forbidden)
    with pytest.raises(ValueError, match="保留证据已失效"):
        await pipeline._analyse_dimension(
            AnalysisTask(facts_json={"q": "披露"}),
            "D4",
            "告知",
            [row],
            None,
            dict(system_prompt="JSON", model="test", temperature=0.0, timeout=5.0),
            _retained=[retained],
        )


async def test_source_review_format_retry_never_retries_a_valid_partial_verdict(monkeypatch):
    from app.analysis import pipeline

    observed = []

    async def fake(messages, **kwargs):
        p = json.loads(messages[1]["content"])
        observed.append(p)
        assert p["quote"] == "The applicant must obtain consent before transfer."
        assert p["conclusion"] == "申请人必须转移数据"
        return LlmResult(
            {
                "supported": "yes/partial/no" if len(observed) == 1 else "partial",
                "reason": "遗漏同意条件",
            },
            4,
            2,
            1,
            "test",
        )

    monkeypatch.setattr(pipeline, "call_llm_json", fake)
    grade, tokens, requests, failures, ledger = await pipeline._review_source_support(
        "The applicant must obtain consent before transfer.",
        "申请人必须转移数据",
        dict(system_prompt="JSON", model="test", temperature=0.0, timeout=5.0),
    )
    assert grade.supported == "partial" and len(observed) == 2
    assert "schema_feedback" in observed[1] and ledger[0]["error"]
    assert tokens == 12 and requests == 2 and failures[0]["kind"] == "source_support_schema_error"


def test_repair_catalog_keeps_original_ids_and_hides_only_completed_fact_spans():
    from app.analysis import pipeline
    from app.analysis.models import AnalysisTask

    row = candidate("a", "The operator must notify customers.\nArticle 17. Transfer needs consent.")
    retained = [{"clause_id": "a", "fact_key": "notice", **select_legal_quote(row.text, [1])}]
    task = AnalysisTask(country_code="XY", facts_json={"notice": "告知", "transfer": "传输"})
    p = json.loads(
        pipeline._build_user_message(task, "D4", "条件", [row], None, retained, 1, {"notice"})
    )
    item = p["candidate_clauses"][0]
    assert [s["sentence_id"] for s in item["retained_context"]] == [1]
    assert all(s["sentence_id"] != 1 for s in item["sentences"])
    assert item["sentences"][0]["scope_id"] > item["retained_context"][0]["scope_id"]
    for s in item["sentences"] + item["retained_context"]:
        assert row.text[s["start"] : s["end"]] == s["text"]
    assert p["business_facts"] == {"notice": "告知"}
    # The same source is still selectable for a different, unfinished fact.
    other = pipeline._candidate_payload(row, retained, {"transfer"})
    assert other["sentences"][0]["sentence_id"] == 1
    assert not other["retained_context"]
