"""Read-only checks for newly generated report cohorts; never upgrades a review."""

import argparse
import asyncio
import hashlib
import json
import sys
import uuid
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.analysis.tools import verify_finding


async def run(args):
    if not args.database.startswith("remediation_") or args.output.exists():
        raise ValueError("isolated database and fresh output required")
    cohort = json.loads(args.cohort.read_text(encoding="utf-8"))
    conn = await asyncpg.connect(
        f"postgresql://postgres@127.0.0.1:3739/{args.database}"
    )
    checked = reports = 0
    try:
        async with conn.transaction(readonly=True, isolation="repeatable_read"):
            for r in cohort["runs"]:
                task = await conn.fetchrow(
                    "SELECT * FROM analysis_tasks WHERE id=$1", uuid.UUID(r["task_id"])
                )
                assert task["status"] == "completed"
                findings = json.loads(task["findings_json"])
                for f in findings:
                    clause = await conn.fetchrow(
                        """SELECT c.original_text,c.version_id,d.id document_id,
                        d.title,d.source_url,v.version_no FROM clauses c JOIN document_versions v
                        ON v.id=c.version_id JOIN documents d ON d.id=v.document_id WHERE c.id=$1""",
                        uuid.UUID(f["clause_id"]),
                    )
                    text = clause["original_text"]
                    assert (
                        hashlib.sha256(text.encode()).hexdigest()
                        == f["source_text_sha256"]
                    )
                    assert text[f["quote_start"] : f["quote_end"]] == f["basis_quote"]
                    assert str(clause["document_id"]) == f["document_id"]
                    assert (
                        clause["version_no"] == f["version_no"]
                        and clause["source_url"] == f["source_url"]
                    )
                    assert verify_finding(
                        {
                            "quote": f["basis_quote"],
                            "obligation": f["compliance_obligation"],
                        },
                        text,
                    )[0]
                    assert f["source_support"]["supported"] == "yes"
                    assert f["source_support"]["human_review"] is None
                    checked += 1
                report = await conn.fetchrow(
                    "SELECT * FROM report_versions WHERE task_id=$1 ORDER BY version_no DESC LIMIT 1",
                    task["id"],
                )
                assert (
                    report["status"] == "full_draft" and not report["is_partial_sample"]
                )
                dims = json.loads(report["dimensions_json"])
                assert len(dims) == 8
                cites = [c for d in dims for c in d["citations"]]
                assert len(cites) == len(findings)
                assert all(
                    c["source_support"]["supported"] == "yes"
                    and c["source_support"]["human_review"] is None
                    for c in cites
                )
                reports += 1
    finally:
        await conn.close()
    result = {
        "passed": True,
        "tasks_checked": reports,
        "final_findings_checked": checked,
        "source_sha256_and_offsets": True,
        "document_and_version_identity": True,
        "numeric_amount_date_checks": True,
        "machine_source_support_present": True,
        "new_human_approvals": 0,
        "note": "程序与机器质检不是人工法律适用确认",
    }
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--database", required=True)
    p.add_argument("--cohort", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    asyncio.run(run(p.parse_args()))
