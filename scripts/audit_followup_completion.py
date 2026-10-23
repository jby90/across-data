"""Read-only final source/context/graph checks; never turn machine checks into human approval."""

import argparse
import asyncio
import hashlib
import json
import sys
import uuid
from collections import Counter
from pathlib import Path

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app import models_registry  # noqa: F401
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.ingestion.models import Clause, Document, DocumentVersion
from app.knowledge.extractor import validate_relation_type_constraints
from app.knowledge.graph_evidence import active_edge_filters
from app.knowledge.history import payload_hash
from app.knowledge.models import ClauseTranslation, KgEdge, KgNode, KnowledgeAuditRecord
from app.knowledge.translator import verify_translation


async def run(frozen):
    if not get_settings().database_url.rsplit("/", 1)[-1].startswith("remediation_"):
        raise ValueError("only isolated remediation copies are permitted")
    targets = json.loads(frozen.read_text(encoding="utf-8"))["invalid_current"]
    results, span_checks = [], 0
    async with get_sessionmaker()() as session:
        for item in targets:
            clause = await session.get(Clause, uuid.UUID(item["clause_id"]))
            if clause.original_text != item["original"]:
                raise ValueError("frozen source text changed")
            tr = await session.scalar(select(ClauseTranslation).where(
                ClauseTranslation.clause_id == clause.id, ClauseTranslation.is_current.is_(True)
            ).order_by(ClauseTranslation.updated_at.desc()).limit(1))
            passed = bool(tr and tr.engine == "llm" and tr.quality_check_passed
                and tr.review_status in ("machine_translated", "pending_review", "approved")
                and verify_translation(clause.original_text, tr.translated_text, tr.glossary_hits or [])[0])
            status, revision_id = "standalone_passed" if passed else "unresolved", None
            if not passed:
                records = (await session.scalars(select(KnowledgeAuditRecord).where(
                    KnowledgeAuditRecord.target_id == clause.id,
                    KnowledgeAuditRecord.action == "translation_context_revision"
                ).order_by(KnowledgeAuditRecord.created_at.desc()))).all()
                for record in records:
                    p = record.payload
                    if not p["quality_check_passed"]:
                        continue
                    assert payload_hash(p) == record.content_hash
                    assert p["review_status"] == "pending_review" and not p["usable_as_legal_basis"]
                    assert not p["standalone_translation"]
                    parts = []
                    for span in p["source_spans"]:
                        row = await session.get(Clause, uuid.UUID(span["clause_id"]))
                        assert row.version_id == clause.version_id
                        assert row.text_hash == span["text_hash"] == hashlib.sha256(
                            row.original_text.encode("utf-8")).hexdigest()
                        assert p["combined_source_text"][span["start"]:span["end"]] == row.original_text
                        parts.append(row.original_text)
                        span_checks += 1
                    assert "\n".join(parts) == p["combined_source_text"]
                    assert payload_hash(dict(spans=p["source_spans"],
                        combined_source_text=p["combined_source_text"])) == p["source_hash"]
                    if not p["quality_issues"] and verify_translation(p["combined_source_text"],
                            p["translated_text"], p["glossary_hits"])[0]:
                        status, revision_id = "context_passed", str(record.id)
                        break
            results.append(dict(clause_id=str(clause.id), status=status,
                translation_status=tr.review_status if tr else None, revision_id=revision_id))
        edges = (await session.scalars(select(KgEdge).where(*active_edge_filters()))).all()
        graph_issues = []
        for edge in edges:
            clause = await session.get(Clause, edge.evidence_clause_id)
            if not edge.evidence_quote or edge.evidence_quote not in clause.original_text:
                graph_issues.append(dict(id=str(edge.id), issue="quote_mismatch"))
            if edge.is_structural:
                continue
            source, target = await session.get(KgNode, edge.source_node_id), await session.get(KgNode, edge.target_node_id)
            version = await session.get(DocumentVersion, clause.version_id)
            doc = await session.get(Document, version.document_id)
            ok, reason = validate_relation_type_constraints(relation_type=edge.relation_type,
                source_type=source.node_type, target_type=target.node_type, evidence_quote=edge.evidence_quote,
                source_name=source.name_zh, target_name=target.name_zh, document_title=doc.title)
            if not ok or edge.judge_verdict != "yes":
                graph_issues.append(dict(id=str(edge.id), issue=reason))
        return dict(frozen_targets=len(results), outcomes=dict(Counter(row["status"] for row in results)),
            source_spans_checked=span_checks, results=results,
            graph=dict(active_edges=len(edges), semantic_edges=sum(not row.is_structural for row in edges),
                structural_edges=sum(row.is_structural for row in edges), validation_issues=graph_issues,
                pending_human_review=True, acceptance_passed=False), human_approvals_created=0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frozen", type=Path, required=True)
    parser.add_argument("--json", type=Path, required=True)
    args = parser.parse_args()
    if args.json.exists():
        raise ValueError("prior evidence must not be overwritten")
    result = asyncio.run(run(args.frozen))
    args.json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key != "results"}, ensure_ascii=False))
