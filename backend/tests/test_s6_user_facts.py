"""S6 state-machine/HTTP tests with a fake LLM, never development/production data."""

import asyncio
import copy
import json
from collections import Counter

import pytest
from sqlalchemy import select

from app.analysis import pipeline
from app.analysis.evidence_selection import AnalysisResponse, UserFactNeed
from app.analysis.llm import LlmResult
from app.analysis.schemas import compute_active_node
from app.core.config import get_settings
from app.ingestion.archive import ArchiveStore
from app.ingestion.models import Clause, DocumentVersion

from .conftest import bearer, login
from .helpers import admin_task_detail, make_pdf
from .test_analysis_p3 import FACTS, _seed_sg_corpus


def fake_s6(calls, request_key="transfer_destination", extra_question=False):
    async def fake(messages, **kwargs):
        p = json.loads(messages[1]["content"])
        phase = p["phase"]
        if phase == "verify_source_support":
            data = {"supported": "yes", "reason": "Synthetic fixture support only"}
        elif phase == "write_from_evidence":
            if p.get("source_units"):
                data = {
                    "units": [
                        {"unit_id": u["unit_id"], "meaning": u["text"]} for u in p["source_units"]
                    ],
                    "gap": "",
                    "risk_level": "LOW",
                }
            else:
                data = {"obligation": p["evidence"]["quote"], "risk_level": "LOW", "gap": ""}
        else:
            code = p["dimension"]["code"]
            calls.append({"code": code, "facts": p["business_facts"], "model": kwargs.get("model")})
            if extra_question and code == "D4" and not p["business_facts"].get("recipient_role"):
                data = {
                    "findings": [],
                    "needs_user_fact": [
                        {
                            "fact_key": "recipient_role",
                            "question_zh": "接收机构承担什么实际业务职责？",
                            "why_needed": "需要明确接收机构的实际业务职责。",
                        }
                    ],
                }
            elif code == "D3" and "上海" not in p["business_facts"].get(request_key, ""):
                data = {
                    "findings": [],
                    "needs_user_fact": [
                        {
                            "fact_key": request_key,
                            "question_zh": "数据实际传输到哪个国家和接收机构？",
                            "why_needed": "接收地是业务事实，影响本维度的场景判断。",
                            "options": ["中国上海研发中心", "暂不确定，尚未决定"],
                        }
                    ],
                }
            else:
                data = {
                    "findings": [
                        {
                            "clause_id": p["candidate_clauses"][0]["clause_id"],
                            "sentence_ids": [1],
                            "fact_key": request_key if code == "D3" else "consent_and_notice",
                            "obligation": "待依据生成",
                            "risk_level": "LOW",
                        }
                    ]
                }
        return LlmResult(data, 20, 10, 1, "synthetic-s6")

    return fake


async def wait_state(client, headers, task_id, expected):
    for _ in range(200):
        r = await client.get(f"/api/analysis/tasks/{task_id}", headers=headers)
        assert r.status_code == 200, r.text
        d = r.json()
        if d["status"] in expected:
            assert "step_traces" not in d
            return await admin_task_detail(client, task_id)
        if d["status"] == "failed":
            raise AssertionError(await admin_task_detail(client, task_id))
        await asyncio.sleep(0.05)
    raise AssertionError("S6 state transition timed out")


async def start(
    client,
    seed,
    session_factory,
    monkeypatch,
    request_key="transfer_destination",
    extra_question=False,
):
    await _seed_sg_corpus(session_factory)
    async with session_factory() as session:
        version = await session.scalar(select(DocumentVersion).limit(1))
        text = (
            "An organisation must obtain a licence and permit before admission. "
            "An organisation must appoint a data protection officer. "
            "An organisation must protect personal data using reasonable security "
            "arrangements to prevent unauthorised access."
        )
        import hashlib

        session.add(
            Clause(
                version_id=version.id,
                ordinal=10,
                clause_number="8",
                language="en",
                original_text=text,
                text_hash=hashlib.sha256(text.encode()).hexdigest(),
            )
        )
        clauses = list(
            (await session.scalars(select(Clause).where(Clause.version_id == version.id))).all()
        )
        for clause in clauses:
            clause.text_hash = hashlib.sha256(clause.original_text.encode()).hexdigest()
        blob = make_pdf([[c.original_text for c in clauses]])
        version.content_hash, version.archive_path = ArchiveStore(get_settings().archive_dir).put(
            blob
        )
        version.size_bytes = len(blob)
        await session.commit()
    calls = []
    monkeypatch.setattr(pipeline, "call_llm_json", fake_s6(calls, request_key, extra_question))
    headers = bearer(await login(client, "user", "a-member@example.com"))
    r = await client.post(
        "/api/analysis/tasks", headers=headers, json={"title": "S6 synthetic test", "facts": FACTS}
    )
    assert r.status_code == 201, r.text
    paused = await wait_state(client, headers, r.json()["id"], {"awaiting_user_facts"})
    return headers, calls, paused


async def answer(client, headers, d, text):
    q = d["pending_user_facts"][0]
    return await client.post(
        f"/api/analysis/tasks/{d['id']}/messages",
        headers=headers,
        json={"content": text, "question_id": q["id"]},
    )


async def test_pause_answer_only_affected_dimension_reruns_with_original_version(
    client, seed, session_factory, monkeypatch
):
    headers, calls, paused = await start(client, seed, session_factory, monkeypatch)
    assert paused["fact_followup_rounds"] == 1 and not paused["report_versions"]
    assert paused["current_active_node_id"] == "node_fact_collector"
    assert Counter(c["code"] for c in calls) == Counter({f"D{i}": 1 for i in range(1, 9)})
    first_tokens = paused["total_tokens"]
    old_results = copy.deepcopy(paused["step_traces"][2]["output_snapshot"]["dimension_outputs"])
    # An administrator activates another version while this conversation is paused.
    admin = bearer(await login(client, "admin", "admin@example.com"))
    base = "/api/admin/agent-studio/workflow"
    dsl = (await client.get(base + "/export", headers=admin)).json()
    nodes = dsl["workflow"]["graph"]["nodes"]
    next(n for n in nodes if n["node_id"] == "node_dimension_analyst")["model_name"] = (
        "other-version-model"
    )
    r = await client.post(base + "/import", headers=admin, json={"dsl_payload": dsl})
    assert r.status_code == 201, r.text
    await client.post(base + f"/versions/{r.json()['id']}/activate", headers=admin)
    answered = await answer(client, headers, paused, "数据传输到中国上海研发中心")
    assert answered.status_code == 200, answered.text
    done = await wait_state(client, headers, paused["id"], {"completed"})
    assert done["workflow_version_id"] == paused["workflow_version_id"]
    assert calls[-1]["model"] != "other-version-model"
    assert Counter(c["code"] for c in calls) == Counter(
        {**{f"D{i}": 1 for i in range(1, 9)}, "D3": 2}
    )
    phases = [s["phase"] for s in done["step_traces"]]
    assert phases.index("user_fact_question") < phases.index("user_fact_answer")
    latest = [s for s in done["step_traces"] if s["phase"] == "analysis"][-1]
    assert latest["input_snapshot"]["dimensions_requested"] == ["D3"]
    assert len(latest["input_snapshot"]["retained_dimensions"]) == 7
    for code in old_results:
        if code != "D3":
            assert latest["output_snapshot"]["dimension_outputs"][code] == old_results[code]
    assert done["total_tokens"] == sum(t["tokens_used"] for t in done["step_traces"])
    assert done["total_tokens"] > first_tokens
    duplicate = await answer(client, headers, paused, "重复的旧回答")
    assert duplicate.status_code == 409


async def test_two_ambiguous_answers_keep_fact_gap_without_legal_backtrack(
    client, seed, session_factory, monkeypatch
):
    headers, calls, first = await start(client, seed, session_factory, monkeypatch)
    r = await answer(client, headers, first, "暂不确定，尚未决定")
    assert r.status_code == 200
    second = await wait_state(client, headers, first["id"], {"awaiting_user_facts"})
    assert second["fact_followup_rounds"] == 2
    assert second["pending_user_facts"][0]["id"] != first["pending_user_facts"][0]["id"]
    assert (await answer(client, headers, first, "过期回答")).status_code == 409
    assert (await answer(client, headers, second, "目前确实不知道接收地")).status_code == 200
    done = await wait_state(client, headers, first["id"], {"partial_sample"})
    dim = next(d for d in done["report_versions"][0]["dimensions"] if d["dimension_code"] == "D3")
    assert (
        dim["gap_kind"] == "user_fact_gap"
        if "gap_kind" in dim
        else "业务事实" in dim["missing_gap_note"]
    )
    assert done["fact_followup_rounds"] == 2 and done["backtrack_count"] == 0
    assert Counter(c["code"] for c in calls)["D3"] == 3
    assert any(t["phase"] == "user_fact_exhausted" for t in done["step_traces"])


async def test_pending_question_survives_reload_tenant_isolation_and_no_manual_bypass(
    client, seed, session_factory, monkeypatch
):
    headers, _, paused = await start(client, seed, session_factory, monkeypatch)
    other = bearer(await login(client, "user", "b-admin@example.com"))
    assert (await answer(client, other, paused, "别的企业回答")).status_code == 404
    assert (
        await client.post(f"/api/analysis/tasks/{paused['id']}/run", headers=headers)
    ).status_code == 409
    reloaded = (await client.get(f"/api/analysis/tasks/{paused['id']}", headers=headers)).json()
    assert reloaded["pending_user_facts"] == paused["pending_user_facts"]
    q = reloaded["pending_user_facts"][0]
    response = await client.post(
        f"/api/analysis/tasks/{paused['id']}/facts",
        headers=headers,
        json={
            "facts": {q["fact_key"]: "中国上海研发中心"},
            "question_ids": {q["fact_key"]: q["id"]},
        },
    )
    assert response.status_code == 200, response.text
    await wait_state(client, headers, paused["id"], {"completed"})
    duplicate = await client.post(
        f"/api/analysis/tasks/{paused['id']}/facts",
        headers=headers,
        json={"facts": {q["fact_key"]: "重复回答"}, "question_ids": {q["fact_key"]: q["id"]}},
    )
    assert duplicate.status_code == 409


def test_missing_law_is_gap_not_a_business_question():
    assert not AnalysisResponse.model_validate(
        {"findings": [], "gaps": "没有可核查的法源"}
    ).needs_user_fact
    with pytest.raises(ValueError):
        UserFactNeed(fact_key="source_url", question_zh="请提供法条原文", why_needed="需要法源依据")
    assert compute_active_node("awaiting_user_facts", [])[0] == "node_fact_collector"


async def test_shared_fact_answer_invalidates_all_dimensions_that_used_it(
    client, seed, session_factory, monkeypatch
):
    headers, calls, paused = await start(
        client, seed, session_factory, monkeypatch, request_key="consent_and_notice"
    )
    assert (
        await answer(client, headers, paused, "中国上海研发中心，当前同意安排待确认")
    ).status_code == 200
    await wait_state(client, headers, paused["id"], {"completed"})
    assert Counter(c["code"] for c in calls) == Counter({f"D{i}": 2 for i in range(1, 9)})


async def test_multiple_questions_queue_answers_before_any_dimension_resumes(
    client, seed, session_factory, monkeypatch
):
    headers, calls, paused = await start(
        client, seed, session_factory, monkeypatch, extra_question=True
    )
    assert len(paused["pending_user_facts"]) == 2
    assert (await answer(client, headers, paused, "中国上海研发中心")).status_code == 200
    intermediate = (await client.get(f"/api/analysis/tasks/{paused['id']}", headers=headers)).json()
    assert intermediate["status"] == "awaiting_user_facts"
    assert intermediate["pending_user_facts"][0]["fact_key"] == "recipient_role"
    assert len(calls) == 8  # No eager model calls while another answer is outstanding.
    assert intermediate["messages"][-1]["slot"] == "recipient_role"
    assert (
        await answer(client, headers, intermediate, "仅承担合成测试研发职责")
    ).status_code == 200
    done = await wait_state(client, headers, paused["id"], {"completed"})
    trace = [t for t in done["step_traces"] if t["phase"] == "analysis"][-1]
    assert trace["input_snapshot"]["dimensions_requested"] == ["D3", "D4"]
    assert len(trace["input_snapshot"]["retained_dimensions"]) == 6


async def test_source_text_change_invalidates_affected_checkpoint_without_reusing_stale_quotes(
    client, seed, session_factory, monkeypatch
):
    import hashlib

    from app.analysis.models import AnalysisTask

    headers, calls, paused = await start(client, seed, session_factory, monkeypatch)
    async with session_factory() as session:
        task = await session.get(AnalysisTask, paused["id"])
        bindings = task.analysis_checkpoint_json["candidate_bindings"]
        changed_id = bindings["D8"][0]["clause_id"]
        affected = {
            code
            for code, rows in bindings.items()
            if any(b["clause_id"] == changed_id for b in rows)
        } | {"D3"}
        clause = await session.get(Clause, changed_id)
        clause.original_text += "\nAdditional fixture condition about security."
        clause.text_hash = hashlib.sha256(clause.original_text.encode()).hexdigest()
        await session.commit()
    assert (await answer(client, headers, paused, "中国上海研发中心")).status_code == 200
    done = await wait_state(client, headers, paused["id"], {"completed"})
    expected = Counter({f"D{i}": 1 for i in range(1, 9)})
    expected.update(affected)
    assert Counter(c["code"] for c in calls) == expected
    trace = [t for t in done["step_traces"] if t["phase"] == "analysis"][-1]
    assert set(trace["input_snapshot"]["dimensions_requested"]) == affected


async def test_corrupt_retained_archive_blocks_resume_instead_of_reusing_results(
    client, seed, session_factory, monkeypatch
):
    from pathlib import Path

    headers, calls, paused = await start(client, seed, session_factory, monkeypatch)
    async with session_factory() as session:
        version = await session.scalar(select(DocumentVersion).limit(1))
        path = Path(get_settings().archive_dir) / version.archive_path
        original = path.read_bytes()
    try:
        path.write_bytes(b"damaged fixture archive")
        before = len(calls)
        assert (await answer(client, headers, paused, "中国上海研发中心")).status_code == 200
        for _ in range(100):
            result = (
                await client.get(f"/api/analysis/tasks/{paused['id']}", headers=headers)
            ).json()
            if result["status"] == "failed":
                break
            await asyncio.sleep(0.05)
        assert result["status"] == "failed" and not result["report_versions"]
        assert len(calls) == before
    finally:
        # This content-addressed synthetic file can be reused by later test fixtures.
        path.write_bytes(original)


@pytest.mark.parametrize("retained_code", ["D1", "D6"])
@pytest.mark.parametrize("prior_failure", ["refused_finding", "no_findings", "model_error"])
async def test_resume_can_repair_retained_dimensions_not_in_initial_rerun(
    client, seed, session_factory, monkeypatch, retained_code, prior_failure
):
    """A checkpoint may contain refused proposals while another dimension asks a question."""
    from app.analysis.models import AnalysisTask

    headers, calls, paused = await start(client, seed, session_factory, monkeypatch)
    async with session_factory() as session:
        task = await session.get(AnalysisTask, paused["id"])
        checkpoint = copy.deepcopy(task.analysis_checkpoint_json)
        proposal = checkpoint["results"][retained_code]
        if prior_failure == "refused_finding":
            proposal["findings"][0]["_generation_gap"] = "Synthetic prior incomplete source"
            proposal["findings"][0]["_ok"] = False
        else:
            proposal["findings"] = []
            proposal["gaps"] = "Synthetic previous result lacks verified evidence"
            if prior_failure == "model_error":
                proposal["error"] = "Synthetic prior model timeout"
        task.analysis_checkpoint_json = checkpoint
        await session.commit()
    assert (await answer(client, headers, paused, "数据传输到中国上海研发中心")).status_code == 200
    done = await wait_state(client, headers, paused["id"], {"completed"})
    analyses = [t for t in done["step_traces"] if t["phase"] == "analysis"]
    assert analyses[-1]["input_snapshot"]["dimensions_requested"] == ["D3"]
    assert retained_code in analyses[-1]["input_snapshot"]["retained_dimensions"]
    repairs = [t for t in done["step_traces"] if t["phase"] == "re_analysis"]
    assert repairs[-1]["input_snapshot"]["re_analysed_dimensions"] == [retained_code]
    assert Counter(c["code"] for c in calls) == Counter(
        {**{f"D{i}": 1 for i in range(1, 9)}, "D3": 2, retained_code: 2}
    )
    assert not any(t["phase"] == "error" for t in done["step_traces"])
    assert len(done["report_versions"][0]["dimensions"]) == 8
    assert done["workflow_version_id"] == paused["workflow_version_id"]
    # The failed first proposal remains in the trace, never rewritten as a success.
    verification = [t for t in done["step_traces"] if t["phase"] == "verification"][-1]
    assert verification["status"] == "backtracked"
    assert any(p.startswith(retained_code) for p in verification["output_snapshot"]["problems"])
