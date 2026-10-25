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
            output = {"queries": [source, "data accuracy"]}
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
            {"queries": ["accuracy"]} if phase == "plan" else {"clause_ids": ids}, 10, 2, 1, "fake"
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
    assert audit["tokens"] == 24  # rejected call still costs tokens


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
