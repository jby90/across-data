import copy

import pytest
from sqlalchemy import select

from app.analysis.models import AgentWorkflowConfig
from app.analysis.workflow_engine import (
    DEFAULT_WORKFLOW_EDGES,
    WORKFLOW_KEY,
    ensure_workflow_config,
)
from app.analysis.workflow_runtime import baseline_nodes, execution_settings, validate_graph

from .conftest import bearer, login

BASE = "/api/admin/agent-studio/workflow"


@pytest.mark.parametrize(
    "mutation",
    [
        "code",
        "tools",
        "topology",
        "duplicate",
        "gate",
        "unknown",
        "bool_number",
        "too_many",
        "disable_required",
        "non_llm_prompt",
        "invalid_edge",
    ],
)
def test_reject_unsafe_or_unimplemented_changes(mutation):
    nodes, edges = baseline_nodes(), copy.deepcopy(DEFAULT_WORKFLOW_EDGES)
    by_id = {n["node_id"]: n for n in nodes}
    analyst = by_id["node_dimension_analyst"]
    if mutation == "code":
        analyst["code_entrypoint"] = "os:system"
    elif mutation == "tools":
        analyst["mcp_tools"] = [{"tool_name": "execute_code"}]
    elif mutation == "topology":
        edges[0]["target_node_id"] = "node_report_compiler"
    elif mutation == "duplicate":
        nodes[-1] = copy.deepcopy(nodes[0])
    elif mutation == "gate":
        by_id["node_citation_verifier"]["parameters"]["require_exact_substring"] = False
    elif mutation == "unknown":
        analyst["parameters"]["api_key"] = "synthetic-not-a-secret"
    elif mutation == "bool_number":
        analyst["parameters"]["temperature"] = True
    elif mutation == "too_many":
        by_id["node_mcp_retriever"]["parameters"]["top_k"] = 31
    elif mutation == "disable_required":
        by_id["node_citation_verifier"]["parameters"]["enabled"] = False
    elif mutation == "non_llm_prompt":
        by_id["node_report_compiler"]["system_prompt"] = "pretend this changes execution"
    elif mutation == "invalid_edge":
        edges[0] = None
    with pytest.raises(ValueError):
        validate_graph(nodes, edges)


def test_whitelist_settings_and_input_are_preserved():
    nodes = baseline_nodes()
    nodes[2]["parameters"]["top_k"] = 9
    nodes[3]["parameters"]["temperature"] = 0.65
    nodes[3]["system_prompt"] += "\n合成测试附加提示"
    nodes[-1]["parameters"]["enabled"] = False
    before = copy.deepcopy(nodes)
    clean = validate_graph(nodes, DEFAULT_WORKFLOW_EDGES)
    assert execution_settings(clean[2])["top_k"] == 9
    assert execution_settings(clean[3])["temperature"] == 0.65
    assert execution_settings(clean[-1])["enabled"] is False
    assert nodes == before


async def test_import_inactive_activate_by_id_rollback_and_permissions(client, seed):
    admin = bearer(await login(client, "admin", "admin@example.com"))
    reviewer = bearer(await login(client, "admin", "reviewer@example.com"))
    original = (await client.get(BASE, headers=admin)).json()
    dsl = (await client.get(BASE + "/export", headers=admin)).json()
    dsl["workflow"]["graph"]["nodes"][2]["parameters"]["top_k"] = 9
    imported = await client.post(BASE + "/import", headers=admin, json={"dsl_payload": dsl})
    assert imported.status_code == 201, imported.text
    new = imported.json()
    assert new["is_active"] is False
    assert (await client.get(BASE, headers=admin)).json()["id"] == original["id"]
    denied = await client.post(BASE + f"/versions/{new['id']}/activate", headers=reviewer)
    assert denied.status_code == 403
    activated = await client.post(BASE + f"/versions/{new['id']}/activate", headers=admin)
    assert activated.status_code == 200, activated.text
    assert activated.json()["nodes"][2]["runtime_settings"]["top_k"] == 9
    rolled_back = await client.post(BASE + f"/versions/{original['id']}/activate", headers=admin)
    assert rolled_back.status_code == 200
    assert rolled_back.json()["id"] == original["id"]
    automatic = await client.post(
        BASE + "/import",
        headers=admin,
        json={
            "dsl_payload": dsl,
            "activate_immediately": True,
        },
    )
    assert automatic.status_code == 400
    dsl["workflow"]["graph"]["nodes"][3]["code_entrypoint"] = "os:system"
    bad = await client.post(BASE + "/import", headers=admin, json={"dsl_payload": dsl})
    assert bad.status_code == 400


async def test_legacy_history_not_automatically_activated_or_rewritten(session_factory):
    async with session_factory() as session:
        legacy = AgentWorkflowConfig(
            workflow_key=WORKFLOW_KEY,
            version_no=16,
            is_active=True,
            name="Legacy history",
            description="Historical display configuration",
            nodes_json=[],
            edges_json=[],
        )
        session.add(legacy)
        await session.commit()
        selected = await ensure_workflow_config(session)
        assert selected.id == legacy.id and selected.is_active
        rows = list((await session.scalars(select(AgentWorkflowConfig))).all())
        assert len(rows) == 2
        baseline = next(r for r in rows if r.id != legacy.id)
        assert baseline.version_no == 17 and not baseline.is_active
        assert legacy.nodes_json == [] and legacy.runtime_schema_version is None
