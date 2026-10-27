"""Public scoped exploration must retain the same evidence/hold boundary as analysis."""

import json
import uuid

from sqlalchemy import select

from app.knowledge.models import KgEdge, KgNode, KnowledgeChangeProposal

from .conftest import bearer, login
from .test_legal_terminology import source


async def graph(s, doc, clause, *, count=1):
    nodes = [
        KgNode(
            country_code=doc.country_code,
            node_key=str(uuid.uuid4()),
            node_type=kind,
            name_zh=name,
            document_id=doc.id if kind == "INSTRUMENT" else None,
        )
        for kind, name in [
            ("INSTRUMENT", doc.title),
            ("OBLIGATION", "حماية البيانات"),
            ("DATA_CATEGORY", "données personnelles"),
        ]
    ]
    s.add_all(nodes)
    await s.flush()
    edges = []
    for i in range(count):
        # Include a disconnected obligation -> data relationship, not just document fan-out.
        edge = KgEdge(
            country_code=doc.country_code,
            source_node_id=nodes[i % 2].id,
            target_node_id=nodes[i % 2 + 1].id,
            relation_type="REQUIRES",
            evidence_clause_id=clause.id,
            evidence_quote=clause.original_text,
            extractor_model="synthetic-private-model",
            judge_reason="private diagnostic",
        )
        s.add(edge)
        edges.append(edge)
    await s.flush()
    return nodes, edges


async def test_scoped_paginated_relations_preserve_originals_and_approvals(
    client,
    seed,
    session_factory,
    monkeypatch,
):
    from app.knowledge import router

    async def forbidden(*args, **kwargs):
        raise AssertionError("public reading must not build a country graph")

    monkeypatch.setattr(router, "_build_graph_payload", forbidden)
    async with session_factory() as s:
        doc, v, c = await source(s, "يجب حماية البيانات الشخصية", country="QZ", language="ar")
        nodes, edges = await graph(s, doc, c, count=7)
        edges[0].review_status = "approved"
        other, _, oc = await source(s, "Protect personal data.", country="XY")
        await graph(s, other, oc)
        await s.commit()
        before = [(str(e.id), e.evidence_quote, e.review_status, e.extractor_model) for e in edges]
    auth = bearer(await login(client, "user", "a-member@example.com"))
    url = f"/api/knowledge/regulations/{doc.id}/relations?version_id={v.id}"
    assert (await client.get(url)).status_code in (401, 403)
    pages = [
        (await client.get(url + f"&offset={n}&limit=3", headers=auth)).json() for n in (0, 3, 6)
    ]
    assert [len(p["relations"]) for p in pages] == [3, 3, 1]
    assert all(p["total"] == 7 and p["version_id"] == str(v.id) for p in pages)
    assert {e["id"] for p in pages for e in p["relations"]} == {str(e.id) for e in edges}
    assert all(e["quote"] == c.original_text for p in pages for e in p["relations"])
    assert any(e["source"] == str(nodes[1].id) for p in pages for e in p["relations"])
    text = json.dumps(pages)
    for hidden in (
        "extractor_model",
        "private diagnostic",
        "content_hash",
        "attributes",
        "synthetic-private-model",
    ):
        assert hidden not in text
    assert (await client.get(url + "&limit=121", headers=auth)).status_code == 422
    assert (await client.get(url + "&offset=-1", headers=auth)).status_code == 422
    async with session_factory() as s:
        after = (await s.scalars(select(KgEdge).where(KgEdge.id.in_([e.id for e in edges])))).all()
        assert sorted(before) == sorted(
            (str(e.id), e.evidence_quote, e.review_status, e.extractor_model) for e in after
        )


async def test_invalid_semantics_old_rules_and_unavailable_nodes_are_not_published(
    client,
    seed,
    session_factory,
):
    async with session_factory() as s:
        doc, v, c = await source(s, "Le responsable protège les données.", language="fr")
        nodes, edges = await graph(s, doc, c, count=8)
        edges[1].evidence_quote = "fabricated quote"
        edges[2].judge_verdict = "no"
        edges[3].extractor_model = "kg-rule-extractor-v1"
        edges[4].review_status = "rejected"
        edges[5].is_structural = True
        edges[6].review_status = "superseded"
        hidden = KgNode(
            country_code="XY",
            node_key="rejected",
            node_type="OBLIGATION",
            name_zh="不可用节点",
            review_status="rejected",
        )
        s.add(hidden)
        await s.flush()
        edges[7].target_node_id = hidden.id
        other, ov, _ = await source(s, "Unrelated version.")
        await s.commit()
    auth = bearer(await login(client, "user", "a-member@example.com"))
    url = f"/api/knowledge/regulations/{doc.id}/relations"
    response = (await client.get(url, headers=auth)).json()
    assert [e["id"] for e in response["relations"]] == [str(edges[0].id)]
    assert response["relations"][0]["review_status"] == "pending_review"
    assert (await client.get(url + f"?version_id={ov.id}", headers=auth)).status_code == 404
    async with session_factory() as s:
        # Linking a held document must not expose its nodes through another document.
        held_node = await s.get(KgNode, nodes[1].id)
        held_node.document_id = other.id
        s.add(
            KnowledgeChangeProposal(
                target_type="documents",
                target_id=other.id,
                content_hash="a" * 64,
                payload={"safety_hold": True},
            )
        )
        await s.commit()
    assert (await client.get(url, headers=auth)).json()["total"] == 0


async def test_old_repealed_and_held_sources_stay_out_of_reader_graph(
    client, seed, session_factory
):
    async with session_factory() as s:
        doc, v1, c1 = await source(s, "First text.")
        await graph(s, doc, c1)
        _, v2, c2 = await source(s, "Second text.", document=doc, number=2)
        await graph(s, doc, c2)
        await s.commit()
    auth = bearer(await login(client, "user", "a-member@example.com"))
    url = f"/api/knowledge/regulations/{doc.id}/relations"
    assert (await client.get(url + f"?version_id={v1.id}", headers=auth)).json()["total"] == 0
    assert (await client.get(url + f"?version_id={v2.id}", headers=auth)).json()["total"] == 1
    async with session_factory() as s:
        v = await s.get(type(v2), v2.id)
        v.legal_status = "repealed"
        await s.commit()
    assert (await client.get(url, headers=auth)).json()["total"] == 0
    async with session_factory() as s:
        s.add(
            KnowledgeChangeProposal(
                target_type="documents",
                target_id=doc.id,
                content_hash="b" * 64,
                payload={"safety_hold": True},
            )
        )
        await s.commit()
    assert (await client.get(url, headers=auth)).status_code == 409
