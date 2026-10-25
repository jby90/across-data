"""Unseen languages/documents and negative cases; no frozen-question success fixtures."""

import copy
import json

import pytest

from app.analysis import retrieval_planner
from app.analysis.llm import LlmResult
from app.analysis.retrieval import ClauseIndex
from app.analysis.workflow_runtime import baseline_nodes, execution_settings, validate_graph

from .test_s5_quality_followup import candidate


@pytest.mark.parametrize(
    "language,source,question",
    [
        ("fr", "Les informations doivent rester exactes et corrigées.", "数据质量要求"),
        ("ar", "يجب حماية المعلومات من الوصول غير المصرح به.", "安全保护"),
        ("en", "A person may object to commercial processing.", "营销反对权"),
        ("xx", "Source in a newly registered language.", "未知语言"),
    ],
)
async def test_planning_is_configured_and_source_bound_across_languages(
    monkeypatch,
    language,
    source,
    question,
):
    rows = [candidate("a", source), candidate("b", "Irrelevant retention example.")]
    rows[0].language = language
    before = copy.deepcopy(rows)
    requests = []

    async def fake(messages, **kwargs):
        request = json.loads(messages[1]["content"])
        requests.append((request, kwargs, messages[0]["content"]))
        if request["phase"] == "plan":
            assert request["languages"] == [language]
            output = {"queries": [source, "data accuracy"], "heading_queries": [source]}
        else:
            assert {c["clause_id"] for c in request["candidates"]} <= {"a", "b"}
            assert all("expected" not in k for k in request)
            output = {"clause_ids": ["a"]}
        return LlmResult(output, 10, 2, 1, kwargs["model"])

    monkeypatch.setattr(retrieval_planner, "call_llm_json", fake)
    config = {
        "system_prompt": "Configured JSON prompt",
        "model": "configured-model",
        "temperature": 0.25,
        "timeout": 5.0,
    }
    result, audit = await retrieval_planner.planned_search(
        ClauseIndex(rows),
        "quality",
        "质量",
        question,
        1,
        config,
    )
    assert result[0][0].clause_id == "a" and rows == before
    assert audit["tokens"] == 24 and audit["requests"] == 2
    assert all(
        p == config["system_prompt"]
        and kw["temperature"] == 0.25
        and kw["model"] == "configured-model"
        and kw["timeout"] == 5
        for _, kw, p in requests
    )
    assert len(audit["ledger"]) == 2 and audit["pool_ids"]


@pytest.mark.parametrize("ids", [["outside"], ["a", "a"], ["a", "b"], []])
async def test_invalid_reranking_does_not_invent_sources_or_hide_failure(monkeypatch, ids):
    rows = [candidate("a", "data accuracy shall be maintained"), candidate("b", "another rule")]

    async def fake(messages, **kwargs):
        phase = json.loads(messages[1]["content"])["phase"]
        return LlmResult(
            {"queries": ["accuracy"], "heading_queries": ["accuracy"]}
            if phase == "plan"
            else {"clause_ids": ids},
            10,
            2,
            1,
            "fake",
        )

    monkeypatch.setattr(retrieval_planner, "call_llm_json", fake)
    index = ClauseIndex(rows)
    config = {"system_prompt": "JSON", "model": "fake", "temperature": 0.0, "timeout": 2.0}
    result, audit = await retrieval_planner.planned_search(
        index,
        "accuracy",
        "quality",
        "accurate data",
        1,
        config,
    )
    assert result == index.rank("accuracy", "accurate data")[:1]
    assert audit["error"] and audit["fallback"] == "original_sparse_route"
    assert audit["tokens"] == (24 if not ids else 36)  # every rejected attempt still costs tokens
    assert audit["pool_ids"] and audit["queries"]  # failure diagnosis cannot invent a missing pool


def test_heading_catalog_is_source_only_bounded_and_independent_of_document_identity():
    rows = [
        candidate(str(i), "Body", heading=("Very long descriptive heading " * 2) + str(i))
        for i in range(500)
    ]
    rows.append(candidate("target", "Another body", heading="Interpretation"))
    headings = retrieval_planner.heading_catalog(rows, "Interpretation", "definitions", budget=200)
    assert "Interpretation" in headings and sum(map(len, headings)) <= 200
    assert set(headings) <= {c.heading for c in rows}


@pytest.mark.parametrize(
    "lead",
    [
        "“mobility record” means",
        '"registre mobile" désigne',
        "«السجل المتنقل» يعني",
        '"移动记录" 是指',
    ],
)
def test_quoted_definitions_on_continuations_get_source_bound_search_names(lead):
    row = candidate(
        "new-law", "Interpretation\n" + lead + " information in a record.", heading="Interpretation"
    )
    before = copy.deepcopy(row)
    assert lead in retrieval_planner.source_headings(row)
    assert lead in retrieval_planner.heading_catalog([row], lead, "definition")
    assert row == before  # no rewriting, renumbering, status changes or graph entity creation
    row.text = "The text mentions “a definition” but makes no definition here."
    assert retrieval_planner.source_headings(row) == ["Interpretation"]


@pytest.mark.parametrize(
    "source_date,translation",
    [
        ("1 February 2032", "2032年2月1日"),
        ("2 juillet 2030", "2030年7月2日"),
        ("٢ جانفي ٢٠٣١", "2031年1月2日"),
        ("2032-02-29", "2032年2月29日"),
    ],
)
def test_complete_dates_compare_exactly_without_rewriting_quotes(source_date, translation):
    from app.analysis.tools import verify_finding

    source = "A valid filing is required on " + source_date + "."
    finding = {"quote": source, "obligation": "申报日期为" + translation}
    assert verify_finding(finding, source) == (True, [])
    assert finding["quote"] == source
    assert not verify_finding({"quote": source, "obligation": "费用为1"}, source)[0]


@pytest.mark.parametrize(
    "source_date,wrong",
    [
        ("1 February 2032", "2032年2月2日"),
        ("1 February 2032", "2032年1月2日"),
        ("1 February 2032", "2031年2月1日"),
        ("1 February 2032", "2032年2月31日"),
        ("July 2030", "2030年7月2日"),
        ("03/04/2030", "2030年3月4日"),
    ],
)
def test_changed_incomplete_or_ambiguous_dates_are_not_inferred(source_date, wrong):
    from app.analysis.tools import verify_finding

    source = "A valid filing is required on " + source_date + ". Other amounts are 1, 2, 3, 31."
    assert not verify_finding({"quote": source, "obligation": "申报日期为" + wrong}, source)[0]


@pytest.mark.parametrize(
    "question",
    [
        "谁应保管审计日志？",
        "Who may revoke access?",
        "هل يلزم تقديم إشعار؟",
        "Qui vérifie la demande ?\nThe organisation provides maintenance services.",
    ],
)
def test_explicit_questions_do_not_inherit_unrelated_directory_labels(question):
    assert retrieval_planner.query_dimension("An unrelated report category", question) is None


@pytest.mark.parametrize(
    "facts",
    [
        "传输遥测数据并保留两年",
        "Store diagnostics at https://official.example/?category=data",
        "Data is encrypted before upload",
        "",
        None,
    ],
)
def test_business_facts_keep_their_dimension_and_url_query_strings_are_not_questions(facts):
    assert retrieval_planner.query_dimension("Storage obligations", facts) == "Storage obligations"


@pytest.mark.parametrize(
    "query,expected_dimension",
    [
        ("Who is responsible for this service?", None),
        ("The company records service telemetry", "A report directory topic"),
    ],
)
async def test_actual_plan_and_ranking_requests_use_question_context_not_directory_topics(
    monkeypatch,
    query,
    expected_dimension,
):
    observed = []

    async def fake(messages, **kwargs):
        data = json.loads(messages[1]["content"])
        observed.append(data)
        assert data["dimension"] == expected_dimension
        output = (
            {"queries": ["responsible individual"], "heading_queries": ["Responsibility"]}
            if data["phase"] == "plan"
            else {"clause_ids": ["a"]}
        )
        return LlmResult(output, 1, 1, 1, "fake")

    monkeypatch.setattr(retrieval_planner, "call_llm_json", fake)
    rows = [
        candidate(
            "a", "The service must designate a responsible individual.", heading="Responsibility"
        )
    ]
    selected, audit = await retrieval_planner.planned_search(
        ClauseIndex(rows),
        "directory terms",
        "A report directory topic",
        query,
        5,
        {"system_prompt": "JSON", "model": "fake", "temperature": 0.0, "timeout": 2.0},
    )
    assert selected[0][0].clause_id == "a" and not audit.get("error")
    assert [d["phase"] for d in observed] == ["plan", "rank"]


async def test_correlated_queries_cannot_starve_an_independent_heading_route(monkeypatch):
    rows = [
        candidate(str(i), "Common processing examples common common common.") for i in range(120)
    ]
    target = candidate(
        "novel",
        "Individuals can request administrative reconsideration.",
        heading="Appeal procedure",
    )
    rows.append(target)

    async def fake(messages, **kwargs):
        data = json.loads(messages[1]["content"])
        if data["phase"] == "plan":
            output = {
                "queries": ["common processing", "processing examples", "common examples"],
                "heading_queries": ["Appeal procedure"],
            }
        else:
            assert "novel" in {c["clause_id"] for c in data["candidates"]}
            assert len(data["candidates"]) <= 40
            output = {"clause_ids": ["novel"]}
        return LlmResult(output, 1, 1, 1, "fake")

    monkeypatch.setattr(retrieval_planner, "call_llm_json", fake)
    selected, _ = await retrieval_planner.planned_search(
        ClauseIndex(rows),
        "common",
        "appeal",
        "request reconsideration",
        5,
        {"system_prompt": "JSON", "model": "fake", "temperature": 0.0, "timeout": 2.0},
    )
    assert selected[0][0] is target


def test_retrieval_model_and_prompt_are_actual_version_parameters_not_code_bindings():
    from app.analysis.workflow_engine import DEFAULT_WORKFLOW_EDGES

    nodes = baseline_nodes()
    node = next(n for n in nodes if n["node_id"] == "node_mcp_retriever")
    node["parameters"]["semantic_planning"] = True
    node["model_name"] = "new-planner"
    node["system_prompt"] += "\nCustom JSON policy"
    validate_graph(nodes, DEFAULT_WORKFLOW_EDGES)
    settings = execution_settings(node)
    assert settings["semantic_planning"] and settings["model"] == "new-planner"
    node["code_entrypoint"] = "arbitrary:code"
    with pytest.raises(ValueError):
        validate_graph(nodes, DEFAULT_WORKFLOW_EDGES)


def test_ranking_preview_is_readonly_and_marks_separate_source_windows():
    row = candidate("a", "Unrelated opening.\n" * 100 + "Target security condition.\n" * 80)
    before = row.text
    windows = json.loads(retrieval_planner.preview(row, ["security"]))
    assert all(w["text"] == before[w["start"] : w["start"] + len(w["text"])] for w in windows)
    assert row.text == before and len(windows) <= 3


@pytest.mark.parametrize("mark", ["\u200f", "\u200e", "\u202b", "\u2067"])
def test_invisible_direction_marks_cannot_hide_qualifiers_or_list_chapeau(mark):
    from app.analysis.evidence_selection import select_legal_quote, sentence_catalog

    source = f"A licence is required.\n{mark}Except for private processing."
    evidence = select_legal_quote(source, [1])
    assert evidence["quote"] == source and mark in evidence["quote"]
    source = f"The operator must supply the following.\n{mark}- A security record is required."
    assert len(sentence_catalog(source)) == 2
    evidence = select_legal_quote(source, [2])
    assert evidence["quote"] == source and evidence["anchor_quote_start"] > 0


@pytest.mark.parametrize(
    "result",
    [
        {"findings": [], "gaps": "Need the definition, not a list of records"},
        {"findings": [], "error": "Invalid JSON from provider"},
        {},
    ],
)
def test_missing_dimension_is_a_readiness_failure_not_silently_passed(result):
    from app.analysis.pipeline import _verify_dimension

    row = candidate("a", "A general source is available, but selection has not succeeded.")
    problems = _verify_dimension("D9", result, [row])
    assert len(problems) == 1 and problems[0].startswith("D9:")
    assert _verify_dimension("D9", {"findings": []}, []) == []  # genuine empty source pool


async def test_empty_selection_returns_to_analysis_and_preserves_first_gap_trace(
    client,
    seed,
    session_factory,
    monkeypatch,
):
    from app.analysis import pipeline

    from .conftest import bearer, login
    from .test_analysis_p3 import FACTS, _fake_llm_factory, _seed_sg_corpus, _wait_done

    await _seed_sg_corpus(session_factory)
    normal = _fake_llm_factory(False)

    async def fake(messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        if (
            payload["phase"] == "select_evidence"
            and payload["dimension"]["code"] == "D2"
            and "verifier_feedback_must_fix" not in payload
        ):
            return LlmResult(
                {"findings": [], "gaps": "Need a definition, not a record list"}, 12, 4, 1, "fake"
            )
        return await normal(messages, **kwargs)

    monkeypatch.setattr(pipeline, "call_llm_json", fake)
    headers = bearer(await login(client, "user", "a-member@example.com"))
    created = await client.post(
        "/api/analysis/tasks",
        headers=headers,
        json={"title": "Readiness regression", "country_code": "SG", "facts": FACTS},
    )
    done = await _wait_done(client, headers, created.json()["id"])
    assert done["status"] in ("completed", "partial_sample")
    assert done["backtrack_count"] == 1
    first = next(t for t in done["step_traces"] if t["phase"] == "verification")
    missing = next(
        r for r in first["output_snapshot"]["readiness_items"] if r["dimension_code"] == "D2"
    )
    assert not missing["passed"] and "definition" in missing["gap"]
    retry = next(t for t in done["step_traces"] if t["phase"] == "re_analysis")
    assert retry["input_snapshot"]["re_analysed_dimensions"] == ["D2"]


async def test_judge_retries_only_rejected_schema_and_preserves_a_valid_negative(monkeypatch):
    import hashlib

    from app.analysis import evaluation

    calls = []
    invalid = {
        "judgments": [{"index": 0, "supported": "no", "reason": "Not supported"}],
        "type": "json_object",
    }
    valid = {"judgments": [{"index": 0, "supported": "partial", "reason": "Missing exception"}]}

    async def fake(messages, **kwargs):
        calls.append(messages)
        return LlmResult(invalid if len(calls) == 1 else valid, 5, 2, 1, "fake")

    monkeypatch.setattr(evaluation, "call_llm_json", fake)
    items = [{"index": 0, "conclusion": "A rule", "quote": "An exception also applies"}]
    verdicts, tokens, error, ledger = await evaluation.judge_findings(
        items,
        {"model": "fake", "timeout": 1.0},
    )
    assert not error and tokens == 14 and len(calls) == 2
    assert verdicts[0]["supported"] == "partial"  # valid negative is final, not retried
    assert ledger[0]["output"] == invalid and ledger[0]["error"]
    assert "schema_feedback" in json.loads(calls[1][1]["content"])
    assert calls[0][0] == calls[1][0]
    assert hashlib.sha256(evaluation.JUDGE_PROMPT.encode()).hexdigest() == (
        "01793fddab716693d4b5eb7e5f1272768f93edc5cc5a857435d9f0539a6bc6f3"
    )
