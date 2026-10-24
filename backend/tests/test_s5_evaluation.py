import json
import uuid

import pytest
from sqlalchemy import select

from app.analysis import pipeline
from app.analysis.evaluation import aggregate, suite
from app.analysis.llm import LlmResult
from app.analysis.models import AnalysisTask
from app.analysis.workflow_runtime import baseline_nodes, execution_settings
from app.knowledge.models import KnowledgeAuditRecord

from .conftest import bearer, login
from .test_analysis_p3 import FACTS, _seed_sg_corpus


def test_frozen_suite_has_real_locators_five_countries_eight_dimensions():
    cases = suite()["cases"]
    assert len(cases) >= 30
    assert {c["country_code"] for c in cases} == {"SG", "EG", "TN", "MA", "LB"}
    assert {c["dimension_code"] for c in cases} == {f"D{i}" for i in range(1, 9)}
    for case in cases:
        assert case["human_review"] is None
        assert case["facts"] and case["expected_conclusion_points"]
        for expected in case["expected_clauses"]:
            assert expected["clause_number"] and expected["document_title"]
            assert len(expected["source_text_sha256"]) == 64
            assert expected["source_url"].startswith("https://")


def test_aggregate_never_counts_missing_quotes_or_unjudged_as_success():
    empty = aggregate([])
    assert empty["quote_fidelity"] is empty["fact_consistency"] is None
    cases = [
        {
            "retrieval_expected": 2,
            "retrieval_hits": 1,
            "findings_total": 3,
            "faithful_findings": 2,
            "judged_findings": 1,
            "supported_findings": 0,
            "tokens": 42,
            "analysis_error": None,
            "judge_error": "timeout",
        }
    ]
    result = aggregate(cases)
    assert result["recall_at_5"] == 0.5
    assert result["quote_fidelity"] == 2 / 3
    assert result["fact_consistency"] == 0
    assert result["judge_coverage"] == 1 / 3
    assert result["human_review_count"] == 0 and result["technical_error_cases"] == 1


async def test_json_schema_retry_gets_concrete_feedback_and_default_temperature_zero(
    session_factory,
    seed,
    monkeypatch,
):
    from app.analysis.tools import ToolBox

    await _seed_sg_corpus(session_factory)
    calls = []

    async def fake(messages, **kwargs):
        calls.append((json.loads(messages[1]["content"]), kwargs))
        if len(calls) == 1:
            return LlmResult({"findings": "invalid"}, 10, 5, 1, "test")
        return LlmResult({"findings": [], "gaps": "没有可适用结论"}, 10, 5, 1, "test")

    monkeypatch.setattr(pipeline, "call_llm_json", fake)
    config = execution_settings(
        next(n for n in baseline_nodes() if n["node_id"] == "node_dimension_analyst")
    )
    async with session_factory() as session:
        cands = await ToolBox(session, "SG").search_effective_clauses("D3")
        result, call = await pipeline._analyse_dimension(
            AnalysisTask(
                country_code="SG", facts_json=FACTS, vehicle_model="test", scenario_type="test"
            ),
            "D3",
            "跨境",
            cands,
            None,
            config,
        )
    assert not result.get("error") and result["tokens"] == 30
    assert call.arguments["schema_attempts"] == 2
    assert all(c[1]["temperature"] == 0 for c in calls)
    assert "findings" in str(calls[1][0]["verifier_feedback_must_fix"])
    assert calls[1][0]["candidate_clauses"][0]["sentences"][0]["text"]


async def test_evaluation_api_auth_no_llm_on_get_and_append_only_server_results(
    client,
    seed,
    session_factory,
):
    admin = bearer(await login(client, "admin", "admin@example.com"))
    user = bearer(await login(client, "user", "a-member@example.com"))
    base = "/api/admin/agent-studio/evaluations"
    assert (await client.get(base + "/latest", headers=user)).status_code == 401
    before = (await client.get(base + "/latest", headers=admin)).json()
    assert before["completed_cases"] == 0 and before["recall_at_5"] is None
    batch = uuid.uuid4()
    response = await client.post(f"{base}/{batch}/cases/S5-01", headers=admin)
    assert response.status_code == 200
    case = response.json()
    assert case["findings_total"] == 0 and case["human_review"] is None
    assert case["source_drift"]  # temporary test DB has none of the archived real sources
    latest = (await client.get(base + "/latest", headers=admin)).json()
    assert latest["completed_cases"] == 1 and latest["quote_fidelity"] is None
    async with session_factory() as session:
        rows = (
            await session.scalars(
                select(KnowledgeAuditRecord).where(
                    KnowledgeAuditRecord.action == "s5_evaluation_case"
                )
            )
        ).all()
        assert len(rows) == 1 and rows[0].payload["retrieval_hits"] == 0
    assert (await client.post(f"{base}/{batch}/cases/fake", headers=admin)).status_code == 404


@pytest.mark.parametrize("rounds", [3, -1])
def test_backtrack_round_limit_is_not_importable_above_two(rounds):
    from app.analysis.workflow_runtime import NodeExecutionSettings

    with pytest.raises(ValueError):
        NodeExecutionSettings(max_backtrack_rounds=rounds)
