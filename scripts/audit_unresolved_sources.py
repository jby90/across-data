"""Zero-model replay of retained gaps, bound to unchanged DB clauses and source archives."""

import argparse
import asyncio
import hashlib
import json
import re
import sys
import uuid
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.analysis.source_gap_audit import (
    pdf_source_profile,
    unresolved_cases,
    verify_retained_quote,
)
from app.ingestion.archive import ArchiveStore


async def run(args):
    if args.output.exists() or not re.fullmatch(r"remediation_[a-z0-9_]+", args.database):
        raise ValueError("fresh output and isolated remediation database required")
    evaluation_bytes = args.evaluation.read_bytes()
    evaluation = json.loads(evaluation_bytes)
    if args.country is not None and not re.fullmatch(r"[A-Z]{2}", args.country):
        raise ValueError("country scope must be an explicit two-letter code")
    input_cases = [
        c for c in evaluation["cases"] if args.country is None or c["country_code"] == args.country
    ]
    cases = unresolved_cases(evaluation, args.country)
    archive = ArchiveStore(args.archive_root)
    profiles, rows = {}, []
    conn = await asyncpg.connect(
        host="127.0.0.1", port=3739, user="postgres", database=args.database
    )
    try:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            for case in cases:
                findings = []
                for index, finding in enumerate(case["findings"]):
                    row = await conn.fetchrow(
                        """SELECT c.*,v.content_hash archive_sha256,
                        v.archive_path,v.document_id,v.text_source,v.review_status,
                        v.legal_status,v.size_bytes,v.mime_type,d.source_url,d.country_code
                        FROM clauses c JOIN document_versions v ON v.id=c.version_id
                        JOIN documents d ON d.id=v.document_id WHERE c.id=$1""",
                        uuid.UUID(finding["clause_id"]),
                    )
                    if row is None or row["country_code"] != case["country_code"]:
                        raise ValueError("retained clause missing or country binding drift")
                    verify_retained_quote(finding, row["original_text"])
                    assert (
                        hashlib.sha256(row["original_text"].encode()).hexdigest()
                        == row["text_hash"]
                    )
                    digest = row["archive_sha256"]
                    blob = archive.read(row["archive_path"])
                    if hashlib.sha256(blob).hexdigest() != digest or len(blob) != row["size_bytes"]:
                        raise ValueError("source archive binding drift")
                    if digest not in profiles and row["mime_type"] == "application/pdf":
                        profiles[digest] = pdf_source_profile(blob)
                    findings.append(
                        {
                            "attempt_index": index,
                            "clause_id": finding["clause_id"],
                            "version_id": str(row["version_id"]),
                            "document_id": str(row["document_id"]),
                            "source_url": row["source_url"],
                            "archive_sha256": digest,
                            "clause_number": row["clause_number"],
                            "page_start": row["page_start"],
                            "page_end": row["page_end"],
                            "text_source": row["text_source"],
                            "needs_review": row["needs_review"],
                            "review_status": row["review_status"],
                            "legal_status": row["legal_status"],
                            "source_text_sha256": row["text_hash"],
                            "quote_start": finding["quote_start"],
                            "quote_end": finding["quote_end"],
                            "quote": finding["quote"],
                            "binding_verified": True,
                            "retained_gap": finding.get("_generation_gap"),
                            "retained_issues": finding.get("_issues", []),
                            "runtime_accepted": finding.get("_ok"),
                            "human_review": None,
                        }
                    )
                rows.append(
                    {
                        "question_id": case["question_id"],
                        "country_code": case["country_code"],
                        "dimension_code": case["dimension_code"],
                        "question": case["scenario_question"],
                        "state": "evidence_gap_retained",
                        "findings": findings,
                        "retained_technical_error": case.get("analysis_error"),
                        "retained_gap_note": case.get("gap_note"),
                        "human_review": None,
                    }
                )
    finally:
        await conn.close()
    result = {
        "mode": "zero_model_replay_not_new_llm_evaluation",
        "evaluation_sha256": hashlib.sha256(evaluation_bytes).hexdigest(),
        "suite_sha256": evaluation["suite_sha256"],
        "retained_batch_id": evaluation["batch_id"],
        "workflow_version_id": evaluation["workflow_version_id"],
        "source_profiles": profiles,
        "unresolved_cases": rows,
        "unresolved_case_count": len(rows),
        "input_case_count": len(input_cases),
        "state": "no_input_cases" if not input_cases else "retained_gap_audit",
        "source_binding_checks": sum(len(r["findings"]) for r in rows),
        "new_model_calls": 0,
        "new_model_tokens": 0,
        "database_writes": 0,
        "new_legal_conclusions": 0,
        "new_human_approvals": 0,
        "note": "仅重验既有失败记录与原文绑定，不更新模型评分、不修改原文或批准状态。",
    }
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(
        json.dumps(
            {k: v for k, v in result.items() if k not in {"source_profiles", "unresolved_cases"}},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluation", type=Path, required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--archive-root", type=Path, required=True)
    parser.add_argument("--country")
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(run(parser.parse_args()))
