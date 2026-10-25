"""Actual ingestion/SQL change linkage in isolated databases; no real-law approvals."""

import copy
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.analysis.models import AnalysisTask, ReportVersion
from app.ingestion.models import Clause, Document, DocumentVersion
from app.sources.models import SourceSite
from app.updates.models import (
    RegulatoryDiscovery,
    SourceFetchObservation,
    UpdateImpact,
    VersionChange,
)
from app.updates.service import (
    compare_clauses,
    current_source_versions,
    link_report,
    monitor_once,
    reconcile_impacts,
    report_updates,
)

from .conftest import bearer, login
from .helpers import make_pdf
from .test_pipeline import Site, act_pdf, add_site, run


async def setup_source(sf, tmp_path, *, country="XY"):
    state = Site()
    site_id = await add_site(sf, strategy="DIRECT", entry="https://example.org/laws/act.pdf")
    async with sf() as s:
        site = await s.get(SourceSite, site_id)
        site.country_code = country
        await s.commit()
    await run(sf, tmp_path, state, site_id)
    async with sf() as s:
        doc = (await s.scalars(select(Document))).one()
        v1 = (await s.scalars(select(DocumentVersion))).one()
        c1 = (await s.scalars(select(Clause).order_by(Clause.ordinal))).first()
        assert v1.parsed_at is not None
    return state, site_id, doc, v1, c1


def dimensions(doc, version, clause, *, count=8, status="COMPLETE"):
    return [
        {
            "dimension_code": f"D{i}",
            "dimension_title": f"Synthetic dimension {i}",
            "risk_level": "LOW",
            "content_markdown": "Synthetic report body; pending review",
            "status": status,
            "citations": [
                {
                    "document_id": str(doc.id),
                    "version_id": str(version.id),
                    "version_no": version.version_no,
                    "clause_id": str(clause.id),
                    "citation_tag": "[synthetic]",
                    "document_title": doc.title,
                    "clause_number": clause.clause_number or "",
                    "quote_original": clause.original_text,
                    "quote_zh": "",
                    "review_status": "pending_review",
                }
            ],
        }
        for i in range(1, count + 1)
    ]


async def add_report(sf, seed, doc, ver, clause):
    async with sf() as s:
        task = AnalysisTask(
            org_id=seed["org_a"].id,
            created_by=seed["a_member"].id,
            title="Synthetic update test",
            country_code=doc.country_code,
            vehicle_model="test",
            scenario_type="test",
            status="completed",
            sufficiency_score=100,
        )
        s.add(task)
        await s.flush()
        report = ReportVersion(
            task_id=task.id,
            version_no=1,
            change_note="original",
            executive_summary="Original report must remain unchanged",
            dimensions_json=dimensions(doc, ver, clause),
        )
        s.add(report)
        await s.commit()
        return task, report


async def change_source(sf, tmp_path, state, site_id, text=" Extra requirement."):
    state.pdf, state.etag = act_pdf(text), '"' + str(uuid.uuid4()) + '"'
    await run(sf, tmp_path, state, site_id)


async def test_capture_change_links_eight_dimensions_deduplicates_and_preserves(
    session_factory,
    tmp_path,
    seed,
    client,
):
    sf = session_factory
    state, site_id, doc, v1, c1 = await setup_source(sf, tmp_path)
    task, report = await add_report(sf, seed, doc, v1, c1)
    snapshot = copy.deepcopy(report.dimensions_json)
    original_bytes = state.pdf
    await change_source(sf, tmp_path, state, site_id)
    result = await monitor_once(sf)
    assert result == {"changes": 1, "impacts": 8, "notifications": 2, "busy": False}
    assert (await monitor_once(sf))["notifications"] == 0
    async with sf() as s:
        change = (await s.scalars(select(VersionChange))).one()
        assert change.change_kind == "text_change_pending_review"
        assert change.content_hashes["old"] == hashlib.sha256(original_bytes).hexdigest()
        impacts = (await s.scalars(select(UpdateImpact))).all()
        assert len(impacts) == 8
        assert all(i.due_at == change.detected_at + timedelta(hours=72) for i in impacts)
        assert (await s.get(ReportVersion, report.id)).dimensions_json == snapshot
        assert (await s.get(Clause, c1.id)).original_text == c1.original_text
        assert (await s.get(DocumentVersion, v1.id)).review_status == v1.review_status
        assert len(await report_updates(s, report.id)) == 8
        assert (await s.scalar(select(func.count()).select_from(SourceFetchObservation))) == 2
        assert (await s.get(DocumentVersion, v1.id)).content_hash == v1.content_hash
    a = bearer(await login(client, "user", "a-member@example.com"))
    b = bearer(await login(client, "user", "b-admin@example.com"))
    admin = bearer(await login(client, "admin", "admin@example.com"))
    notices = (await client.get("/api/notifications", headers=a)).json()
    assert notices["total"] == notices["unread"] == 1
    n = notices["items"][0]
    assert len(n["dimension_codes"]) == 8 and n["human_review"] == "pending_review"
    assert (await client.get("/api/notifications", headers=b)).json()["total"] == 0
    for action in ("read", "reevaluate"):
        assert (
            await client.post(f"/api/notifications/{n['id']}/{action}", headers=b)
        ).status_code == 404
    assert (await client.post(f"/api/notifications/{n['id']}/read", headers=a)).status_code == 200
    await monitor_once(sf)
    assert (await client.get("/api/notifications", headers=a)).json()["unread"] == 0
    metrics = await client.get("/api/admin/updates/sla", headers=admin)
    assert metrics.status_code == 200, metrics.text
    assert metrics.json()["pending_updates"] == 1
    assert metrics.json()["intake"]["completed"] == 1
    # User-side JWTs are rejected by the existing admin-side authentication boundary.
    assert (await client.get("/api/admin/updates/sla", headers=a)).status_code == 401
    discoveries = (
        await client.get("/api/admin/updates/discoveries?country=XY", headers=admin)
    ).json()
    assert discoveries["total"] == 1
    assert discoveries["items"][0]["first_parsed_at"] is not None
    assert not discoveries["items"][0]["history_unknown"]


async def test_source_reversion_is_observed_without_overwriting_archive(
    session_factory, tmp_path, seed
):
    sf = session_factory
    state, site_id, doc, v1, c1 = await setup_source(sf, tmp_path, country="TN")
    original = state.pdf
    task, report = await add_report(sf, seed, doc, v1, c1)
    await change_source(sf, tmp_path, state, site_id)
    await monitor_once(sf)
    async with sf() as s:
        v2 = await s.scalar(select(DocumentVersion).where(DocumentVersion.version_no == 2))
        assert (await current_source_versions(s))[doc.id] == v2.id
    state.pdf, state.etag = original, '"back-to-original"'
    await run(sf, tmp_path, state, site_id)
    assert (await monitor_once(sf))["changes"] == 1
    async with sf() as s:
        assert (await current_source_versions(s))[doc.id] == v1.id
        assert await s.scalar(select(func.count()).select_from(DocumentVersion)) == 2
        assert await s.scalar(select(func.count()).select_from(SourceFetchObservation)) == 3
        changes = (await s.scalars(select(VersionChange).order_by(VersionChange.detected_at))).all()
        assert changes[1].old_version_id == v2.id and changes[1].new_version_id == v1.id
        # Reading an alert does not resolve it; a newer complete report does.
        assert {i.state for i in (await s.scalars(select(UpdateImpact))).all()} == {"open"}
        new = ReportVersion(
            task_id=task.id,
            version_no=2,
            change_note="reassessment",
            executive_summary="New report using actually observed archive",
            dimensions_json=dimensions(doc, v1, c1),
            created_at=datetime.now(UTC),
        )
        s.add(new)
        await s.flush()
        await link_report(s, task, new)
        await reconcile_impacts(s, task_ids=[task.id])
        await s.commit()
    async with sf() as s:
        assert {i.state for i in (await s.scalars(select(UpdateImpact))).all()} == {"resolved"}
        assert (await s.get(ReportVersion, report.id)).change_note == "original"


async def test_late_report_link_recovery_and_incomplete_reassessment_does_not_resolve(
    session_factory,
    tmp_path,
    seed,
):
    sf = session_factory
    state, site_id, doc, v1, c1 = await setup_source(sf, tmp_path)
    await change_source(sf, tmp_path, state, site_id)
    assert (await monitor_once(sf))["impacts"] == 0
    task, report = await add_report(sf, seed, doc, v1, c1)
    assert (await monitor_once(sf))["impacts"] == 8
    async with sf() as s:
        v2 = await s.scalar(select(DocumentVersion).where(DocumentVersion.version_no == 2))
        clause = await s.scalar(select(Clause).where(Clause.version_id == v2.id))
        s.add(
            ReportVersion(
                task_id=task.id,
                version_no=2,
                change_note="insufficient source",
                executive_summary="Gap retained",
                dimensions_json=dimensions(doc, v2, clause, status="GAP"),
            )
        )
        await s.flush()
        await reconcile_impacts(s)
        await s.commit()
    async with sf() as s:
        assert {i.state for i in (await s.scalars(select(UpdateImpact))).all()} == {"open"}


@pytest.mark.parametrize("texts", [("نص أصلي 12", "نص جديد 15"), ("données 12", "données 15")])
def test_alignment_is_language_independent_and_never_fills_missing_text(texts):
    def clause(text, number="2"):
        return SimpleNamespace(
            id=uuid.uuid4(),
            original_text=text,
            text_hash=hashlib.sha256(text.encode()).hexdigest(),
            section_path="Part A",
            clause_number=number,
        )

    a, b = clause(texts[0]), clause(texts[1])
    kind, differences = compare_clauses([a], [b])
    assert kind == "text_change_pending_review" and differences[0]["kind"] == "changed"
    assert a.original_text == texts[0] and b.original_text == texts[1]
    assert compare_clauses([a], [a])[0] == "bytes_only"
    b.text_hash = "wrong"
    assert compare_clauses([a], [b])[0] == "source_integrity_pending"
    b = clause(texts[1], "")
    a.clause_number = ""
    assert compare_clauses([a], [b])[1][0]["kind"] == "alignment_pending"


async def test_sla_preserves_unknown_history_and_failed_discovery(
    session_factory, tmp_path, seed, client
):
    sf = session_factory
    state, site_id, doc, v1, c1 = await setup_source(sf, tmp_path)
    async with sf() as s:
        version = await s.get(DocumentVersion, v1.id)
        version.parsed_at = None  # Legacy fixture, not backfilled with a guessed time.
        then = datetime.now(UTC) - timedelta(days=31)
        s.add(
            RegulatoryDiscovery(
                site_id=site_id,
                country_code="ZZ",
                source_url="https://example.org/blocked.pdf",
                title="Blocked source",
                discovered_at=then,
                last_seen_at=then,
            )
        )
        await s.commit()
    admin = bearer(await login(client, "admin", "admin@example.com"))
    response = await client.get("/api/admin/updates/sla", headers=admin)
    assert response.status_code == 200, response.text
    assert response.json()["intake"]["history_unknown"] == 1
    assert response.json()["intake"]["pending"] == 1
    assert response.json()["intake"]["overdue_30d"] == 1
    assert set(response.json()["countries"]) == {"XY", "ZZ"}


async def test_bytes_only_and_unparsed_changes_are_not_fabricated_legal_amendments(
    session_factory,
    tmp_path,
    seed,
):
    sf = session_factory
    state, site_id, doc, v1, c1 = await setup_source(sf, tmp_path)
    await add_report(sf, seed, doc, v1, c1)
    # A harmless PDF trailing comment changes bytes but not extracted clauses.
    state.pdf += b"\n% New PDF producer metadata only\n"
    state.etag = '"metadata-only"'
    await run(sf, tmp_path, state, site_id)
    result = await monitor_once(sf)
    assert result["changes"] == 1 and result["impacts"] == result["notifications"] == 0
    async with sf() as s:
        assert (await s.scalar(select(VersionChange))).change_kind == "bytes_only"
    state.pdf, state.etag = make_pdf([[]]), '"image-only"'
    await run(sf, tmp_path, state, site_id)
    await monitor_once(sf)
    async with sf() as s:
        newest = await s.scalar(select(DocumentVersion).order_by(DocumentVersion.version_no.desc()))
        assert newest.state == "needs_ocr"
        assert newest.parsed_at is None
        change = await s.scalar(
            select(VersionChange).where(VersionChange.new_version_id == newest.id)
        )
        assert change.change_kind == "parsing_pending"
        assert change.changes_json[0]["kind"] == "parse_not_available"
        assert not await s.scalar(
            select(func.count()).select_from(Clause).where(Clause.version_id == newest.id)
        )
        # A newly observed source with no text does not fall back to stale text.
        from app.analysis.tools import ToolBox

        assert await ToolBox(s, doc.country_code)._load_candidates() == []


async def test_notification_reevaluation_keeps_old_version_and_uses_current_source(
    session_factory,
    tmp_path,
    seed,
    client,
    monkeypatch,
):
    from app.analysis import pipeline
    from app.core.config import get_settings

    from .test_analysis_p3 import FACTS, _wait_done
    from .test_s6_user_facts import fake_s6

    sf = session_factory
    state, site_id, doc, v1, c1 = await setup_source(sf, tmp_path, country="SG")
    task, report = await add_report(sf, seed, doc, v1, c1)
    # A long actual parsed test clause is needed for the unchanged retrieval gates.
    state.pdf = make_pdf(
        [
            [
                "Article 1 Data protection",
                "An organisation must protect personal data using reasonable security",
                "arrangements and appoint a data protection officer before processing.",
                "An organisation must not transfer personal data without comparable",
                "protection, a licence and permit, consent and appropriate retention.",
            ]
        ]
    )
    state.etag = '"new-body"'
    await run(sf, tmp_path, state, site_id)
    async with sf() as s:
        t = await s.get(AnalysisTask, task.id)
        t.facts_json = dict(FACTS, transfer_destination="中国上海研发中心")
        # Explicit synthetic source relevance configuration, not a production rule.
        d = await s.get(Document, doc.id)
        d.relevance = "relevant"
        await s.commit()
    await monitor_once(sf)
    auth = bearer(await login(client, "user", "a-member@example.com"))
    notification = (await client.get("/api/notifications", headers=auth)).json()["items"][0]
    calls = []
    monkeypatch.setattr(pipeline, "call_llm_json", fake_s6(calls))
    # The isolated actual source archive is the one used by verification.
    monkeypatch.setattr(get_settings(), "archive_dir", tmp_path / "archive")
    response = await client.post(
        f"/api/notifications/{notification['id']}/reevaluate", headers=auth
    )
    assert response.status_code == 200, response.text
    done = await _wait_done(client, auth, str(task.id))
    assert done["status"] in {"completed", "partial_sample"}, done
    async with sf() as s:
        reports = (await s.scalars(select(ReportVersion).order_by(ReportVersion.version_no))).all()
        assert len(reports) == 2 and reports[0].dimensions_json == report.dimensions_json
        assert reports[1].version_no == 2
        v2 = await s.scalar(select(DocumentVersion).where(DocumentVersion.version_no == 2))
        cites = [c for d in reports[1].dimensions_json for c in d["citations"]]
        assert cites and {c["version_id"] for c in cites} == {str(v2.id)}
        impacts = (await s.scalars(select(UpdateImpact))).all()
        states = {i.dimension_code: i.state for i in impacts}
        for dim in reports[1].dimensions_json:
            assert states[dim["dimension_code"]] == (
                "resolved" if dim["status"] == "COMPLETE" else "open"
            )
        assert (await s.get(DocumentVersion, v1.id)).review_status == v1.review_status


async def test_changed_source_hides_old_semantic_edges_without_rewriting_approval(
    session_factory,
    tmp_path,
    seed,
):
    from app.knowledge.graph_evidence import active_edge_filters
    from app.knowledge.history import payload_hash
    from app.knowledge.models import KgEdge, KgNode, KnowledgeAuditRecord

    sf = session_factory
    state, site_id, doc, v1, c1 = await setup_source(sf, tmp_path)
    async with sf() as s:
        d = await s.get(Document, doc.id)
        d.relevance = "relevant"
        a = KgNode(
            country_code=doc.country_code,
            node_type="INSTRUMENT",
            node_key="law",
            name_zh="合成法",
            document_id=doc.id,
        )
        b = KgNode(
            country_code=doc.country_code,
            node_type="DATA_CATEGORY",
            node_key="data",
            name_zh="合成数据",
        )
        s.add_all([a, b])
        await s.flush()
        edge = KgEdge(
            country_code=doc.country_code,
            source_node_id=a.id,
            target_node_id=b.id,
            relation_type="GOVERNS",
            evidence_clause_id=c1.id,
            evidence_quote=c1.original_text,
            extractor_model="synthetic-semantic-test",
            judge_verdict="yes",
            review_status="approved",
            reviewed_by=seed["reviewer"].id,
            reviewed_at=datetime.now(UTC),
        )
        s.add(edge)
        await s.commit()
        assert await s.scalar(select(KgEdge.id).where(*active_edge_filters())) == edge.id
        approval = (edge.review_status, edge.reviewed_by, edge.reviewed_at, edge.evidence_quote)
    await change_source(sf, tmp_path, state, site_id)
    await monitor_once(sf)
    async with sf() as s:
        assert await s.scalar(select(KgEdge.id).where(*active_edge_filters())) is None
        saved = await s.get(KgEdge, edge.id)
        # Existing ingestion marks changed-version edges stale, preserving their
        # full previous approval in immutable audit. It must never carry that
        # approval over to a changed source or erase its reviewer/evidence.
        assert saved.review_status == "stale"
        assert (saved.reviewed_by, saved.reviewed_at, saved.evidence_quote) == approval[1:]
        audit = await s.scalar(
            select(KnowledgeAuditRecord).where(
                KnowledgeAuditRecord.target_id == edge.id,
                KnowledgeAuditRecord.action == "before_version_stale",
            )
        )
        assert audit is not None and audit.content_hash == payload_hash(audit.payload)
        assert audit.payload["review_status"] == "approved"
        assert audit.payload["reviewed_by"] == str(approval[1])
        assert audit.payload["reviewed_at"] == approval[2].isoformat()
        assert audit.payload["evidence_quote"] == approval[3]
        change = await s.scalar(select(VersionChange))
        assert change.graph_edge_ids == [str(edge.id)]
