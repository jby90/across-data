import json

from app.analysis import pipeline
from app.knowledge.business_view import topic_cards
from app.knowledge.models import KgEdge, KgNode

from .conftest import bearer, login
from .test_analysis_p3 import FACTS, _fake_llm_factory, _seed_sg_corpus, _wait_done
from .test_legal_terminology import source


async def test_user_task_omits_diagnostics_but_admin_retains_trace(
    client, seed, session_factory, monkeypatch
):
    await _seed_sg_corpus(session_factory)
    monkeypatch.setattr(pipeline, "call_llm_json", _fake_llm_factory(bad_first=False))
    user = bearer(await login(client, "user", "a-member@example.com"))
    admin = bearer(await login(client, "admin", "admin@example.com"))
    r = await client.post(
        "/api/analysis/tasks",
        headers=user,
        json={"title": "业务视图隔离", "country_code": "SG", "facts": FACTS},
    )
    assert r.status_code == 201
    done = await _wait_done(client, user, r.json()["id"])
    assert done["step_traces"] and done["workflow_version_id"]
    public = (await client.get(f"/api/analysis/tasks/{done['id']}", headers=user)).json()
    for key in (
        "step_traces",
        "workflow_version_id",
        "workflow_version_no",
        "mcp_calls_count",
        "total_tokens",
        "backtrack_count",
        "current_active_node_title",
    ):
        assert key not in public
    assert public["progress"] and public["report_versions"] and public["findings"]
    text = json.dumps(public, ensure_ascii=False)
    assert (
        "system_prompt" not in text
        and "code_entrypoint" not in text
        and "input_snapshot" not in text
    )
    assert (
        await client.get(f"/api/admin/agent-studio/tasks/{done['id']}", headers=user)
    ).status_code in (401, 403)
    internal = (
        await client.get(f"/api/admin/agent-studio/tasks/{done['id']}", headers=admin)
    ).json()
    assert any(
        t["input_snapshot"]["workflow_execution"]["node_settings"].get("system_prompt")
        for t in internal["step_traces"]
    )
    assert '"source_support"' not in text and '"source_coverage"' not in text


async def test_topic_cards_only_from_current_eligible_evidence_and_user_has_no_graph(
    client, seed, session_factory
):
    async with session_factory() as s:
        doc, version, clause = await source(
            s, "A controller must protect personal data during transfer."
        )
        a = KgNode(
            country_code="XY",
            node_key="law",
            node_type="INSTRUMENT",
            name_zh="合成法规",
            document_id=doc.id,
        )
        b = KgNode(
            country_code="XY", node_key="duty", node_type="OBLIGATION", name_zh="保护个人数据"
        )
        s.add_all([a, b])
        await s.flush()
        for quote, verdict in [
            (clause.original_text, "yes"),
            ("not in the source", "yes"),
            (clause.original_text, "no"),
        ]:
            s.add(
                KgEdge(
                    country_code="XY",
                    source_node_id=a.id,
                    target_node_id=b.id,
                    relation_type="REQUIRES",
                    evidence_clause_id=clause.id,
                    evidence_quote=quote,
                    extractor_model="synthetic-test",
                    judge_verdict=verdict,
                )
            )
        await s.commit()
        cards = await topic_cards(s, "XY")
        assert len(cards) == 1 and cards[0]["quote"] == clause.original_text
        assert cards[0]["review_status"] == "pending_review"
        assert "extractor_model" not in cards[0] and "source_node_id" not in cards[0]
    headers = bearer(await login(client, "user", "a-member@example.com"))
    public = (await client.get(f"/api/knowledge/regulations/{doc.id}", headers=headers)).json()
    assert "mini_graph" not in public and "sha256_hex" not in public["regulation"]
    assert "content_hash" not in public["versions"][0]
    assert (
        await client.get("/api/knowledge/graph?country_code=XY", headers=headers)
    ).status_code == 410


async def test_library_reaches_clauses_after_80_with_search_and_exact_source_navigation(
    client, seed, session_factory, monkeypatch
):
    from app.ingestion.models import Clause
    from app.knowledge import router
    from app.knowledge.legal_terms import sha

    async def no_diagnostics(*args, **kwargs):
        raise AssertionError("A reading page must not build the administrative graph")

    monkeypatch.setattr(router, "_build_graph_payload", no_diagnostics)
    async with session_factory() as s:
        doc, version, _ = await source(s, "A synthetic source has many independent provisions.")
        for number in range(2, 96):
            body = f"Provision {number} has a distinct source statement."
            row = Clause(
                version_id=version.id,
                ordinal=number,
                clause_number=str(number),
                original_text=body,
                text_hash=sha(body),
                language="en",
            )
            s.add(row)
            if number == 93:
                focus = row
        await s.commit()
        focus_id = str(focus.id)
    auth = bearer(await login(client, "user", "a-member@example.com"))
    url = f"/api/knowledge/regulations/{doc.id}"
    first = (await client.get(url, headers=auth)).json()
    assert first["clauses_total"] == 95 and len(first["clauses"]) == 12
    later = (await client.get(url + "?clause_offset=84", headers=auth)).json()
    assert later["clauses"][0]["ordinal"] == 85 and len(later["clauses"]) == 11
    focused = (await client.get(url + f"?focus_clause_id={focus_id}", headers=auth)).json()
    assert focused["clause_offset"] == 84 and focus_id in {
        c["clause_id"] for c in focused["clauses"]
    }
    searched = (await client.get(url + "?keyword=Provision%2093", headers=auth)).json()
    assert searched["clauses_total"] == 1 and searched["clauses"][0]["clause_id"] == focus_id
    assert (await client.get(url + "?clause_limit=101", headers=auth)).status_code == 422
