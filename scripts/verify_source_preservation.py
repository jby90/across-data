"""Compare protected source/evidence/approval fields between original and isolated copy, read-only."""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

import asyncpg

FIELDS = {
    "documents": "id,site_id,country_code,title,document_type,language,source_url,relevance,relevance_note,relevance_source",
    "document_versions": "id,document_id,version_no,content_hash,archive_path,size_bytes,mime_type,final_url,state,review_status,official_number,version_label,published_date,effective_date,repealed_date,legal_status,metadata_source",
    "clauses": "id,version_id,ordinal,clause_number,heading,section_path,language,original_text,text_hash,page_start,page_end,ocr_required,needs_review",
    "source_sites": "id,terms_status,terms_note,terms_decided_by,terms_decided_at",
    "kg_edges": "id,source_node_id,target_node_id,relation_type,evidence_clause_id,evidence_quote,extractor_model,review_status,reviewed_by,reviewed_at",
    "kg_nodes": "id,node_type,node_key,name_zh,name_orig,summary,attributes,document_id,evidence_clause_id,review_status",
    "glossary_terms": "id,source_term,target_zh,statutory_definition,review_status,reviewed_by,reviewed_at",
    "clause_translations": "id,clause_id,translated_text,review_status,reviewer_id,reviewer_note,reviewed_at",
}


def digest(rows):
    return hashlib.sha256(json.dumps([dict(row) for row in rows], default=str,
        sort_keys=True, ensure_ascii=False).encode()).hexdigest()


async def verify(copy_name):
    if not copy_name.startswith("remediation_"):
        raise ValueError("copy database name must start with remediation_")
    home = Path.home() / ".cyshuhe-dev"
    port = int((home / "pg/postmaster.pid").read_text().splitlines()[3])
    root = f"postgresql://postgres@127.0.0.1:{port}/"
    original, copy = await asyncpg.connect(root + "postgres"), await asyncpg.connect(root + copy_name)
    try:
        result = {}
        for table, fields in FIELDS.items():
            predicate = (
                " WHERE reviewed_at IS NOT NULL OR reviewer_id IS NOT NULL OR review_status IN ('approved','rejected')" if table == "clause_translations"
                else " WHERE review_status IN ('approved','rejected')" if table in ("kg_nodes", "glossary_terms")
                else ""
            )
            query = f"SELECT {fields} FROM {table}{predicate} ORDER BY id"
            old, new = await original.fetch(query), await copy.fetch(query)
            new_by_id = {row["id"]: dict(row) for row in new}
            matched, mismatches, verified_retirements = [], [], []
            for row in old:
                current = new_by_id.get(row["id"])
                if current is None:
                    mismatches.append(str(row["id"]))
                    continue
                if (table == "kg_edges" and row["review_status"] == "pending_review"
                    and current["review_status"] in ("superseded", "stale")):
                    expected = json.loads(json.dumps(dict(row), default=str))
                    snapshots = await copy.fetch("SELECT payload FROM knowledge_audit_records "
                        "WHERE target_id=$1 AND action LIKE 'before_%'", row["id"])
                    immutable = [key for key in expected if key != "review_status"]
                    identical = all(current[key] == row[key] for key in immutable)
                    retained = any(all(json.loads(item["payload"]).get(key) == expected[key]
                        for key in expected) for item in snapshots)
                    if identical and retained:
                        verified_retirements.append(str(row["id"]))
                        current["review_status"] = row["review_status"]
                if dict(row) != current:
                    mismatches.append(str(row["id"]))
                matched.append(current)
            appended = set(new_by_id) - {row["id"] for row in old}
            result[table] = dict(original_count=len(old), copy_count=len(new), appended_count=len(appended),
                original_sha256=digest(old), matched_original_sha256=digest(matched),
                audited_machine_retirements=verified_retirements, mismatched_original_ids=mismatches,
                equal=not mismatches and len(matched) == len(old))
        versions = await original.fetch("SELECT archive_path, content_hash FROM document_versions")
        checked = 0
        for version in versions:
            for directory in (home / "archive", home / copy_name / "archive"):
                actual = hashlib.sha256((directory / version["archive_path"]).read_bytes()).hexdigest()
                if actual != version["content_hash"]:
                    raise ValueError("archive integrity mismatch")
            checked += 1
        return dict(protected_tables=result, archive_versions_verified=checked,
            all_protected_fields_equal=all(item["equal"] for item in result.values()))
    finally:
        await original.close()
        await copy.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--copy", default="remediation_20261023")
    parser.add_argument("--json", type=Path, required=True)
    args = parser.parse_args()
    report = asyncio.run(verify(args.copy))
    args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(dict(all_protected_fields_equal=report["all_protected_fields_equal"],
        archive_versions_verified=report["archive_versions_verified"]), ensure_ascii=False))
