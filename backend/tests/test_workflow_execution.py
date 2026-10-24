import asyncio
import json

import httpx

from app.analysis import llm, pipeline
from app.core.config import get_settings

from .conftest import bearer, login
from .test_analysis_p3 import FACTS, _seed_sg_corpus, _wait_done

BASE = "/api/admin/agent-studio/workflow"


async def test_config_drives_actual_http_requests_pins_inflight_run_and_preserves_rerun_history(
    client,
    seed,
    session_factory,
    monkeypatch,
):
    await _seed_sg_corpus(session_factory)
    admin = bearer(await login(client, "admin", "admin@example.com"))
    member = bearer(await login(client, "user", "a-member@example.com"))
    original = (await client.get(BASE, headers=admin)).json()
    dsl = (await client.get(BASE + "/export", headers=admin)).json()
    nodes = {n["node_id"]: n for n in dsl["workflow"]["graph"]["nodes"]}
    nodes["node_mcp_retriever"]["parameters"]["top_k"] = 1
    analyst = nodes["node_dimension_analyst"]
    analyst["parameters"].update(temperature=0.65, timeout=4.0)
    analyst["model_name"] = "synthetic-s4-model"
    analyst["system_prompt"] += "\n这是独立测试配置，保留全部原文核验要求。"
    nodes["node_update_linkage"]["parameters"]["enabled"] = False
    imported = (
        await client.post(BASE + "/import", headers=admin, json={"dsl_payload": dsl})
    ).json()
    assert not imported["is_active"]

    entered, release = asyncio.Event(), asyncio.Event()
    requests = []
    timeouts = []
    original_client = httpx.AsyncClient
    monkeypatch.setattr(get_settings(), "llm_api_key", "synthetic-test-key")

    async def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if body["model"] != "synthetic-s4-model" and not release.is_set():
            entered.set()
            await release.wait()
        return httpx.Response(
            200,
            request=request,
            json={
                "model": body["model"],
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "summary": "合成测试无结论",
                                    "findings": [],
                                    "gaps": "未提供结论",
                                }
                            )
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    def fake_client(**kwargs):
        timeouts.append(kwargs.get("timeout"))
        return original_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(llm.httpx, "AsyncClient", fake_client)
    payload = {"title": "S4 same input", "country_code": "SG", "facts": FACTS}
    first = await client.post("/api/analysis/tasks", headers=member, json=payload)
    assert first.status_code == 201, first.text
    await asyncio.wait_for(entered.wait(), timeout=5)
    activated = await client.post(BASE + f"/versions/{imported['id']}/activate", headers=admin)
    assert activated.status_code == 200
    release.set()
    task_a = await _wait_done(client, member, first.json()["id"])
    assert task_a["workflow_version_id"] == original["id"]
    assert all(t["workflow_version_id"] == original["id"] for t in task_a["step_traces"])
    assert all(r["model"] != "synthetic-s4-model" for r in requests)
    old_calls = len(requests)

    second = await client.post("/api/analysis/tasks", headers=member, json=payload)
    task_b = await _wait_done(client, member, second.json()["id"])
    assert task_b["workflow_version_id"] == imported["id"] != task_a["workflow_version_id"]
    actual = requests[old_calls:]
    assert actual and all(r["temperature"] == 0.65 for r in actual)
    assert all(r["model"] == analyst["model_name"] for r in actual)
    assert all(r["messages"][0]["content"] == analyst["system_prompt"] for r in actual)
    assert all(
        len(json.loads(r["messages"][1]["content"])["candidate_clauses"]) <= 1 for r in actual
    )
    assert all(t == 4.0 for t in timeouts[old_calls:])
    retrieval = next(t for t in task_b["step_traces"] if t["phase"] == "retrieval")
    assert retrieval["input_snapshot"]["workflow_execution"]["node_settings"]["top_k"] == 1
    assert all(
        c["arguments"]["top_k"] == 1
        for c in retrieval["tool_calls"]
        if c["tool_name"] == "search_effective_clauses"
    )
    assert (
        next(t for t in task_b["step_traces"] if t["phase"] == "update_linkage")["status"]
        == "skipped"
    )

    old_traces = {t["id"]: t for t in task_b["step_traces"]}
    await client.post(BASE + f"/versions/{original['id']}/activate", headers=admin)
    rerun = await client.post(f"/api/analysis/tasks/{task_b['id']}/run", headers=member)
    assert rerun.status_code == 200
    rerun_done = await _wait_done(client, member, task_b["id"])
    assert rerun_done["workflow_version_id"] == original["id"]
    by_id = {t["id"]: t for t in rerun_done["step_traces"]}
    assert all(by_id[tid] == trace for tid, trace in old_traces.items())
    assert len(rerun_done["step_traces"]) > len(old_traces)
    assert len(rerun_done["report_versions"]) == 2


async def test_configured_retry_budget_never_disables_citation_gate(
    client,
    seed,
    session_factory,
    monkeypatch,
):
    await _seed_sg_corpus(session_factory)
    admin = bearer(await login(client, "admin", "admin@example.com"))
    member = bearer(await login(client, "user", "a-member@example.com"))
    dsl = (await client.get(BASE + "/export", headers=admin)).json()
    dsl["workflow"]["graph"]["nodes"][4]["parameters"]["max_backtrack_rounds"] = 2
    imported = (
        await client.post(BASE + "/import", headers=admin, json={"dsl_payload": dsl})
    ).json()
    await client.post(BASE + f"/versions/{imported['id']}/activate", headers=admin)

    async def bad_model(messages, **kwargs):
        candidate = json.loads(messages[1]["content"])["candidate_clauses"][0]
        return llm.LlmResult(
            data={
                "findings": [
                    {
                        "clause_id": candidate["clause_id"],
                        "sentence_ids": [999],
                        "fact_key": "exterior_video_and_gnss",
                        "obligation": "错误结论",
                        "risk_level": "HIGH",
                    }
                ]
            },
            prompt_tokens=1,
            completion_tokens=1,
            duration_ms=1,
            model="fake",
        )

    monkeypatch.setattr(pipeline, "call_llm_json", bad_model)
    result = await client.post(
        "/api/analysis/tasks",
        headers=member,
        json={
            "title": "S4 retry guard",
            "country_code": "SG",
            "facts": FACTS,
        },
    )
    task = await _wait_done(client, member, result.json()["id"])
    assert task["backtrack_count"] == 2
    assert len([t for t in task["step_traces"] if t["phase"] == "re_analysis"]) == 2
    assert task["findings"] == [] and task["status"] == "failed"


async def test_analysis_timeout_is_applied_and_not_turned_into_legal_findings(monkeypatch):
    async def slow(*args, **kwargs):
        await asyncio.sleep(5)

    from app.analysis.models import AnalysisTask

    monkeypatch.setattr(pipeline, "call_llm_json", slow)
    task = AnalysisTask(
        country_code="BR", vehicle_model="Synthetic", scenario_type="test", facts_json={}
    )
    data, call = await pipeline._analyse_dimension(
        task,
        "D1",
        "Test",
        [],
        None,
        {
            "model": "fake",
            "system_prompt": "test",
            "temperature": 0.2,
            "timeout": 1.0,
        },
    )
    assert data["error"] and data["findings"] == []
    assert not call.verified and call.duration_ms >= 900
