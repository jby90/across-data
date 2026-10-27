"""Read-only regression of frozen PDFs, bilingual revisions and pre-run row hashes."""

import argparse
import asyncio
import hashlib
import json
import re
import sys
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.ingestion.archive import ArchiveStore
from app.ingestion.clauses import split_clauses
from app.ingestion.parsing import parse_document
from app.knowledge.history import payload_hash
from app.knowledge.source_repairs import prepare_page, verify_bilingual
from app.knowledge.translator import verify_translation


async def verify(run_path, before_path, archive_path, output):
    if output.exists():
        raise ValueError("existing verification evidence cannot be overwritten")
    run = json.loads(run_path.read_text(encoding="utf-8"))
    before = json.loads(before_path.read_text(encoding="utf-8"))
    conn = await asyncpg.connect(
        "postgresql://postgres@127.0.0.1:3739/remediation_20261023"
    )
    rows, additions = [], {}
    try:
        for table, expected in before.items():
            current = {}
            for row in await conn.fetch(
                f"SELECT row_to_json(t)::text AS value FROM {table} t ORDER BY id"
            ):
                value = json.loads(row["value"])
                current[value["id"]] = hashlib.sha256(
                    json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
                ).hexdigest()
            assert all(current.get(key) == value for key, value in expected.items()), (
                table
            )
            additions[table] = len(current) - len(expected)
            if table not in ("knowledge_audit_records", "knowledge_change_proposals"):
                assert additions[table] == 0, table
        for item in run["items"]:
            frozen, bilingual = item["prepared"], item["translation"]
            digest = bilingual["pdf_content_hash"]
            blob = ArchiveStore(archive_path).read(digest[:2] + "/" + digest)
            current = prepare_page(blob, bilingual["page_number"])
            assert current == frozen, "re-parsed glyphs/fields differ"
            verify_bilingual(current, bilingual)
            unit_checks = []
            for unit in bilingual["units"]:
                passed, issues, _ = verify_translation(
                    unit["source_text"], unit["translated_text"], unit["glossary_hits"]
                )
                assert passed and not issues and unit["quality_check_passed"], issues
                for attempt in unit["attempts"]:
                    if attempt["passed"]:
                        assert verify_translation(
                            attempt["source_text"],
                            attempt["translated_text"],
                            unit["glossary_hits"],
                        )[0]
                unit_checks.append(passed)
            parsed = parse_document(blob, "application/pdf")
            drafts, strategy = split_clauses(
                [parsed.pages[bilingual["page_number"] - 1]]
            )
            assert strategy == "layout"
            tables = [d for d in drafts if (d.section_path or "").startswith("TABLE /")]
            references = [
                d for d in drafts if (d.section_path or "").startswith("REFERENCE /")
            ]
            if tables:
                assert len(tables) == 2
                assert [
                    len(re.findall(r"^\d+\.\s", d.text, flags=re.MULTILINE))
                    for d in tables
                ] == [6, 7]
                assert "7. 100,001 and above $3,200" in tables[-1].text
                assert "Made this" not in tables[-1].text
                assert drafts[-1].section_path == "SIGNATURE"
                assert "AUBECK KAM" in drafts[-1].text
            if references:
                assert len(references) == 16
                assert [
                    int(re.match(r"\d+", d.text).group()) for d in references
                ] == list(range(14, 30))
                assert (
                    sum(f["kind"] == "source_url" for f in current["literal_fields"])
                    == 19
                )
                assert len(current["layout"]["glyph_decodings"]) == 2
                assert "\ufffe" not in current["layout"]["text"]
            for key, action in (
                ("source_revision_id", "source_parse_revision"),
                ("bilingual_revision_id", "parsed_source_translation"),
            ):
                record = await conn.fetchrow(
                    "SELECT content_hash,payload,action FROM knowledge_audit_records "
                    "WHERE id=$1::uuid",
                    item[key],
                )
                payload = json.loads(record["payload"])
                assert (
                    record["content_hash"] == payload_hash(payload)
                    and record["action"] == action
                )
                assert (
                    not payload["active"]
                    and not payload["human_reviewed"]
                    and not payload["usable_as_legal_basis"]
                )
            latest = await conn.fetchrow(
                "SELECT review_status,quality_check_passed FROM clause_translations "
                "WHERE clause_id=$1::uuid AND is_current ORDER BY updated_at DESC LIMIT 1",
                item["clause_id"],
            )
            assert (
                latest["review_status"] == "translation_failed"
                and not latest["quality_check_passed"]
            )
            rows.append(
                {
                    "clause_id": item["clause_id"],
                    "linguistic_units": len(unit_checks),
                    "literal_fields": len(current["literal_fields"]),
                    "tables": len(tables),
                    "bibliography_entries": len(references),
                    "passed": True,
                    "original_failed_row_retained": True,
                    "review_status": "pending_review",
                }
            )
        versions = await conn.fetch(
            "SELECT archive_path,content_hash FROM document_versions"
        )
        for version in versions:
            assert (
                ArchiveStore.sha256(
                    ArchiveStore(archive_path).read(version["archive_path"])
                )
                == version["content_hash"]
            )
        result = {
            "passed": True,
            "items": rows,
            "existing_rows_unchanged": {t: len(v) for t, v in before.items()},
            "row_additions": additions,
            "archives_verified": len(versions),
            "human_approved": 0,
            "whole_clause_pass_claimed": False,
            "before_snapshot_sha256": hashlib.sha256(
                before_path.read_bytes()
            ).hexdigest(),
        }
        output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(result, ensure_ascii=False))
    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--before-row-hashes", type=Path, required=True)
    parser.add_argument("--archive-root", type=Path, required=True)
    parser.add_argument("--json", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(verify(args.run, args.before_row_hashes, args.archive_root, args.json))
