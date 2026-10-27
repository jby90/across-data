"""Read-only whole-run checks: old rows, approvals, source pages, graph and reports."""

import argparse
import asyncio
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import asyncpg
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app import models_registry  # noqa: F401
from app.analysis.models import ReportVersion
from app.analysis.tools import ToolBox
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.ingestion.archive import ArchiveStore
from app.ingestion.layout_safety import layout_held_clause_ids
from app.ingestion.models import Clause, Document, DocumentVersion
from app.ingestion.safety import report_held_citation_ids
from app.knowledge.extractor import validate_relation_type_constraints
from app.knowledge.graph_evidence import active_edge_filters
from app.knowledge.history import payload_hash
from app.knowledge.models import KgEdge, KgNode, KnowledgeAuditRecord
from app.knowledge.parsed_graph import pending_parsed_sources, validate_cached_result
from app.knowledge.source_repairs import prepare_page


def same_snapshot(old, snapshot):
    # PostgreSQL JSON omits trailing fractional-second zeros; Python isoformat
    # retains six digits. Compare actual timestamp values, all other fields exact.
    if old.keys() != snapshot.keys():
        return False
    for key, value in old.items():
        other = snapshot[key]
        if key.endswith("_at") and value is not None and other is not None:
            if datetime.fromisoformat(value) != datetime.fromisoformat(other):
                return False
        elif value != other:
            return False
    return True


async def verify(baseline, graph_run, output):
    settings = get_settings()
    db = settings.database_url.rsplit("/", 1)[-1]
    if not db.startswith("remediation_") or output.exists():
        raise ValueError("isolated copy and fresh verification output required")
    before = json.loads(baseline.read_text(encoding="utf-8"))
    run = json.loads(graph_run.read_text(encoding="utf-8"))["summary"]
    assert run["all_countries_completed"] and run["no_sampling"]
    assert set(run["latest_by_country"]) == {"SG", "EG", "TN", "MA", "LB"}
    conn = await asyncpg.connect(
        settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
    )
    protected, additions, retirements = {}, {}, 0
    try:
        snapshots = {}
        for row in await conn.fetch(
            "SELECT target_id,payload,content_hash FROM knowledge_audit_records "
            "WHERE action LIKE 'before_%'"
        ):
            payload = json.loads(row["payload"])
            if payload_hash(payload) == row["content_hash"]:
                snapshots.setdefault(str(row["target_id"]), []).append(payload)
        tables = {
            r["tablename"]
            for r in await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname='public'")
        }
        for table, old_rows in before.items():
            assert table in tables and re.fullmatch(r"[a-z_]+", table)
            current = {
                str(row["id"]): row
                for item in await conn.fetch(f"SELECT row_to_json(t)::text value FROM {table} t")
                if (row := json.loads(item["value"]))
            }
            for rid, frozen in old_rows.items():
                assert rid in current, f"historical row missing in {table}"
                row = current[rid]
                digest = hashlib.sha256(
                    json.dumps(row, sort_keys=True, ensure_ascii=False).encode()
                ).hexdigest()
                if digest == frozen["hash"]:
                    continue
                old = frozen["row"]
                assert (
                    table == "kg_edges"
                    and old["review_status"] == "pending_review"
                    and row["review_status"] == "superseded"
                ), table
                assert all(
                    value == row[key] for key, value in old.items() if key != "review_status"
                )
                assert any(same_snapshot(old, snap) for snap in snapshots.get(rid, [])), (
                    "pending retirement lacks immutable full snapshot"
                )
                retirements += 1
            protected[table] = len(old_rows)
            additions[table] = len(current) - len(old_rows)
            if table not in ("kg_edges", "knowledge_audit_records", "knowledge_change_proposals"):
                assert additions[table] == 0, f"unexpected rows in {table}"
    finally:
        await conn.close()
    async with get_sessionmaker()() as session:
        sources = list(
            (
                await session.execute(
                    select(Clause, DocumentVersion, Document)
                    .join(DocumentVersion, DocumentVersion.id == Clause.version_id)
                    .join(Document, Document.id == DocumentVersion.document_id)
                )
            ).all()
        )
        source_by_id = {c.id: (c, v, d) for c, v, d in sources}
        held = set(await layout_held_clause_ids(session, list(source_by_id)))
        held_by_country = Counter(source_by_id[cid][2].country_code for cid in held)
        nodes = {n.id: n for n in (await session.scalars(select(KgNode))).all()}
        edges = list((await session.scalars(select(KgEdge).where(*active_edge_filters()))).all())
        for edge in edges:
            c, version, doc = source_by_id[edge.evidence_clause_id]
            assert (
                c.id not in held and edge.evidence_quote and edge.evidence_quote in c.original_text
            )
            assert ArchiveStore.sha256(c.original_text.encode()) == c.text_hash
            if not edge.is_structural:
                assert edge.judge_verdict == "yes"
                assert validate_relation_type_constraints(
                    edge.relation_type,
                    nodes[edge.source_node_id].node_type,
                    nodes[edge.target_node_id].node_type,
                    edge.evidence_quote,
                    nodes[edge.source_node_id].name_zh,
                    nodes[edge.target_node_id].name_zh,
                    doc.title,
                )[0]
            if str(edge.id) not in before["kg_edges"]:
                assert edge.review_status == "pending_review"
        candidates, report_impacts = [], []
        for cc in ("SG", "EG", "TN", "MA", "LB"):
            loaded = await ToolBox(session, cc)._load_candidates()
            assert not {c.clause_id for c in loaded}.intersection(str(cid) for cid in held)
            for c in loaded:
                assert "source_revision_id" not in c.__dict__, (
                    "pending repair must not replace original evidence"
                )
        for report in (await session.scalars(select(ReportVersion))).all():
            held_ids = await report_held_citation_ids(session, report)
            cited = [cite for d in report.dimensions_json for cite in d.get("citations", [])]
            report_impacts.append(
                dict(
                    id=str(report.id),
                    version=report.version_no,
                    citations=len(cited),
                    held_citation_ids=[str(i) for i in held_ids],
                    unchanged=True,
                )
            )
        for source in await pending_parsed_sources(session):
            p = source.payload
            blob = ArchiveStore(settings.archive_dir).read(
                p["pdf_content_hash"][:2] + "/" + p["pdf_content_hash"]
            )
            prepared = prepare_page(blob, p["page_number"])
            assert prepared == p["prepared"]
            graph = await session.scalar(
                select(KnowledgeAuditRecord)
                .where(
                    KnowledgeAuditRecord.target_id == source.target_id,
                    KnowledgeAuditRecord.action == "kg_parsed_source_result",
                    KnowledgeAuditRecord.payload["source_revision_id"].as_string()
                    == str(source.id),
                )
                .order_by(KnowledgeAuditRecord.created_at.desc())
                .limit(1)
            )
            assert (
                graph
                and graph.payload["status"] == "completed"
                and payload_hash(graph.payload) == graph.content_hash
            )
            validate_cached_result(graph.payload, source, prepared)
            candidates.append(
                dict(
                    id=str(graph.id),
                    country=graph.payload["country_code"],
                    blocks=len(graph.payload["blocks"]),
                    linguistic_tables=sum(b["kind"] == "table" for b in graph.payload["blocks"]),
                    excluded_non_normative=sum(
                        b["status"] == "excluded_non_normative" for b in graph.payload["blocks"]
                    ),
                    admitted_relations=sum(
                        r["evaluation"]["admitted"]
                        for b in graph.payload["blocks"]
                        for r in b["relations"]
                    ),
                    rejections=dict(
                        Counter(
                            r["evaluation"]["stage"]
                            for b in graph.payload["blocks"]
                            for r in b["relations"]
                        )
                    ),
                    review_status=graph.payload["review_status"],
                    active=False,
                )
            )
        versions = list((await session.scalars(select(DocumentVersion))).all())
        for version in versions:
            ArchiveStore(settings.archive_dir).read(version.archive_path)
        result = dict(
            passed=True,
            old_rows_checked=protected,
            row_additions=additions,
            pending_edge_retirements_with_full_snapshots=retirements,
            source_and_approval_overwrites=0,
            archives_verified=len(versions),
            graph=dict(
                active_edges=len(edges),
                semantic_edges=sum(not e.is_structural for e in edges),
                structural_edges=sum(e.is_structural for e in edges),
                by_country=dict(Counter(e.country_code for e in edges)),
                invalid_evidence=0,
            ),
            layout_held=dict(
                clauses=len(held), by_country=dict(held_by_country), active_graph_edges=0
            ),
            parsed_graph_candidates=candidates,
            reports=report_impacts,
            human_approvals_created=0,
            human_acceptance_passed=False,
        )
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--json", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(verify(args.baseline, args.run, args.json))
