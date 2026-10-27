import copy
import json
from datetime import UTC, datetime, timedelta

from app.analysis.failure_feedback import failure_feedback
from app.analysis.models import AnalysisStepTrace, AnalysisTask, ReportVersion

from .conftest import bearer, login
from .test_s7_exports_events import fixture_report


def test_mixed_failure_explains_format_and_evidence_without_changing_status_or_leaking_details():
    now = datetime.now(UTC)
    traces = [
        {
            "phase": "re_analysis",
            "created_at": now,
            "output_snapshot": {
                "dimension_outputs": {
                    "D8": {"error": "PRIVATE_SCHEMA_ERROR"},
                    "D1": {"error": None},
                }
            },
            "tool_calls": [
                {
                    "arguments": {
                        "dimension": "D8",
                        "error_kind": "model_format_error",
                        "system_prompt": "PRIVATE_PROMPT",
                    }
                }
            ],
        }
    ]
    reports = [
        {
            "version_no": 1,
            "created_at": now,
            "status": "analysis_failed",
            "dimensions": [
                {
                    "dimension_code": "D1",
                    "status": "ANALYSIS_ERROR",
                    "citations": [{"quote": "source"}],
                },
                {"dimension_code": "D8", "status": "ANALYSIS_ERROR", "citations": []},
            ],
        }
    ]
    before = copy.deepcopy((traces, reports))
    result = failure_feedback("failed", traces, reports)
    assert result["retained_citations"] == 1 and result["report_version"] == 1
    assert result["issues"][0] == {"label": "分析输出未通过格式检查", "dimension_codes": ["D8"]}
    assert result["issues"][1]["dimension_codes"] == ["D1"]
    assert "原文与依据" in result["next_action"]
    assert "PRIVATE" not in json.dumps(result)
    assert (traces, reports) == before
    assert failure_feedback("running", traces, reports) is None
    assert failure_feedback("completed", traces, reports) is None


def test_new_attempt_does_not_inherit_old_report_or_old_model_failure():
    now = datetime.now(UTC)
    traces = [
        {
            "phase": "analysis",
            "created_at": now - timedelta(minutes=5),
            "output_snapshot": {"dimension_outputs": {"D1": {"error": "old failure"}}},
        },
        {"phase": "fact_check", "created_at": now},
        {
            "phase": "error",
            "created_at": now + timedelta(seconds=1),
            "input_snapshot": {"failed_phase": "retrieval", "private": "secret"},
        },
    ]
    reports = [{"version_no": 7, "created_at": now - timedelta(minutes=4), "dimensions": []}]
    result = failure_feedback("failed", traces, reports, {"retrieval": "查找适用法规"})
    assert result["summary"].startswith("查找适用法规环节中断")
    assert result["issues"] == [] and result["report_version"] is None
    assert "secret" not in json.dumps(result)


def test_model_timeout_and_empty_legacy_failure_are_safe():
    now = datetime.now(UTC)
    result = failure_feedback(
        "failed",
        [
            {
                "phase": "analysis",
                "created_at": now,
                "output_snapshot": {"dimension_outputs": {"D4": {"error": "private"}}},
                "tool_calls": [{"arguments": {"dimension": "D4", "error_kind": "model_timeout"}}],
            }
        ],
        [],
    )
    assert result["issues"] == [{"label": "分析响应超时", "dimension_codes": ["D4"]}]
    assert "重试" in result["next_action"]
    assert failure_feedback("failed", [], [])["issues"] == []


async def test_failure_summary_http_respects_tenants_and_does_not_mutate_report(
    client, seed, session_factory
):
    task, report = await fixture_report(session_factory, seed, state="failed")
    async with session_factory() as s:
        # The export fixture deliberately stores an incomplete sentinel finding;
        # use an actual empty finding list for this task-detail HTTP check.
        current = await s.get(AnalysisTask, task.id)
        current.findings_json = []
        row = await s.get(ReportVersion, report.id)
        row.status = "analysis_failed"
        dims = copy.deepcopy(row.dimensions_json)
        dims[0].update(status="ANALYSIS_ERROR", gap_kind="analysis_error")
        row.dimensions_json = dims
        s.add(
            AnalysisStepTrace(
                task_id=task.id,
                seq_no=1,
                agent_node_id="PRIVATE_NODE",
                agent_name="PRIVATE_AGENT",
                phase="analysis",
                status="failed",
                business_summary="PRIVATE",
                output_snapshot_json={"dimension_outputs": {"D1": {"error": "PRIVATE_ERROR"}}},
                tool_calls_json=[
                    {
                        "call_id": "synthetic",
                        "tool_name": "llm.chat",
                        "tool_label": "PRIVATE",
                        "arguments": {"dimension": "D1", "error_kind": "model_format_error"},
                        "result_summary": "PRIVATE",
                        "duration_ms": 1,
                        "verified": False,
                    }
                ],
            )
        )
        await s.commit()
        before = copy.deepcopy(row.dimensions_json)
    auth = bearer(await login(client, "user", "a-member@example.com"))
    response = await client.get(f"/api/analysis/tasks/{task.id}", headers=auth)
    assert response.status_code == 200
    feedback = response.json()["failure_summary"]
    assert feedback["issues"][0]["label"] == "分析输出未通过格式检查"
    assert "PRIVATE" not in response.text and "step_traces" not in response.json()
    foreign = bearer(await login(client, "user", "b-admin@example.com"))
    assert (await client.get(f"/api/analysis/tasks/{task.id}", headers=foreign)).status_code == 404
    async with session_factory() as s:
        assert (await s.get(ReportVersion, report.id)).dimensions_json == before
        assert (await s.get(AnalysisTask, task.id)).status == "failed"


async def test_running_progress_omits_historical_failure_but_keeps_admin_history(
    client, seed, session_factory
):
    task, _ = await fixture_report(session_factory, seed, state="running")
    async with session_factory() as s:
        (await s.get(AnalysisTask, task.id)).findings_json = []
        for seq, phase, status in [
            (1, "error", "failed"),
            (2, "fact_check", "passed"),
            (3, "analysis", "passed"),
        ]:
            s.add(
                AnalysisStepTrace(
                    task_id=task.id,
                    seq_no=seq,
                    phase=phase,
                    status=status,
                    agent_node_id="synthetic",
                    agent_name="synthetic",
                    business_summary="synthetic",
                )
            )
        await s.commit()
    auth = bearer(await login(client, "user", "a-member@example.com"))
    data = (await client.get(f"/api/analysis/tasks/{task.id}", headers=auth)).json()
    assert data["failure_summary"] is None
    assert data["progress"] == [
        {"label": "整理业务信息", "state": "complete"},
        {"label": "分析合规要求", "state": "complete"},
    ]
    admin = bearer(await login(client, "admin", "admin@example.com"))
    internal = (await client.get(f"/api/admin/agent-studio/tasks/{task.id}", headers=admin)).json()
    assert len(internal["step_traces"]) == 3 and internal["step_traces"][0]["phase"] == "error"
