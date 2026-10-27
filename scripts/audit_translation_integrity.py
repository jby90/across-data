"""Read-only API revalidation of translations, coverage and human review sample queue."""

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.knowledge.translator import verify_translation
from audit_platform import _fetch_all_documents, _login_admin


def audit(base_url):
    credentials = json.loads((Path.home() / ".cyshuhe-dev/credentials.json").read_text(encoding="utf-8"))
    with httpx.Client(base_url=base_url, timeout=60) as client:
        token = _login_admin(client, "admin@example.com", credentials["users"]["admin@example.com"])
        headers = {"Authorization": "Bearer " + token}
        docs = _fetch_all_documents(client, headers)
        rows = []
        offset = 0
        while True:
            response = client.get("/api/admin/knowledge/translations", headers=headers,
                params={"limit": 500, "offset": offset, "include_history": "true"})
            response.raise_for_status()
            page = response.json()
            rows.extend(page)
            if len(page) < 500:
                break
            offset += 500
        clause_ids, held_clause_ids, counts = set(), set(), Counter()
        latest_by_doc = {}
        for doc in docs:
            response = client.get(f"/api/admin/documents/{doc['id']}", headers=headers)
            response.raise_for_status()
            detail = response.json()
            version = next((v for v in detail["versions"] if v["state"] == "parsed"), None)
            if version is None:
                continue
            latest_by_doc[doc["id"]] = version
            offset = 0
            while True:
                response = client.get(f"/api/admin/versions/{version['id']}/clauses", headers=headers,
                    params={"limit": 500, "offset": offset})
                response.raise_for_status()
                page = response.json()
                if doc.get("safety_hold"):
                    held_clause_ids.update(row["id"] for row in page)
                else:
                    clause_ids.update(row["id"] for row in page)
                    counts[doc["country_code"]] += len(page)
                if len(page) < 500:
                    break
                offset += 500
    current = [row for row in rows if row["is_current"] and row["clause_id"] in clause_ids
               and row["review_status"] not in ("superseded", "stale", "rejected")]
    qualified, invalid = [], []
    for row in current:
        passed, issues, _ = verify_translation(row["original_text"], row["translated_text"], row["glossary_hits"])
        if passed and row["engine"] == "llm" and row["review_status"] in ("machine_translated", "pending_review", "approved"):
            qualified.append(row)
        else:
            doc = next(item for item in docs if item["id"] == row["document_id"])
            invalid.append({"translation_id": row["id"], "clause_id": row["clause_id"], "issues": issues,
                "status": row["review_status"], "country_code": row["country_code"],
                "document_title": doc["title"], "source_url": doc["source_url"],
                "original": row["original_text"], "translated": row["translated_text"],
                "gap_kind": "numeric_or_citation_fragment" if not re.search(
                    r"[^\W\d_]", row["original_text"], re.UNICODE) else "translation_quality_failure"})
    doc_by_id = {doc["id"]: doc for doc in docs}
    samples = []
    for country in ("SG", "EG", "TN", "MA", "LB"):
        candidates = [row for row in qualified if row["country_code"] == country
                      and doc_by_id[row["document_id"]]["relevance"] in ("relevant", "related")]
        for row in candidates[:2]:
            doc = doc_by_id[row["document_id"]]
            version = latest_by_doc[doc["id"]]
            samples.append(dict(country_code=country, translation_id=row["id"], clause_id=row["clause_id"],
                clause_number=row["clause_number"], source_url=doc["source_url"], document_title=doc["title"],
                version_id=version["id"], content_hash=version["content_hash"], original=row["original_text"],
                translated=row["translated_text"], reviewer=None, decision="pending_human_review", comments=None))
    qualified_ids = {row["clause_id"] for row in qualified}
    return dict(summary=dict(total_latest_clauses=len(clause_ids | held_clause_ids),
        eligible_latest_clauses=len(clause_ids), source_held_clauses=len(held_clause_ids), current=len(current),
        missing_qualified_clauses=len(clause_ids - qualified_ids),
        qualified_coverage_percent=round(100 * len(qualified_ids) / len(clause_ids), 3) if clause_ids else None,
        gap_kinds=dict(Counter(row["gap_kind"] for row in invalid)),
        identical_to_original=sum(row["original_text"].strip() == row["translated_text"].strip() for row in qualified),
        current_engines=dict(Counter(row["engine"] for row in current)),
        attempt_statuses=dict(Counter(row["review_status"] for row in rows)),
        qualified_unique_clauses=len({row["clause_id"] for row in qualified}), invalid_current=len(invalid),
        by_country={cc: dict(clauses=total, qualified=len({row["clause_id"] for row in qualified
            if row["country_code"] == cc})) for cc, total in counts.items()},
        known_tokens_in=sum(row["tokens_in"] for row in rows),
        known_tokens_out=sum(row["tokens_out"] for row in rows),
        human_approved=sum(row["review_status"] == "approved" for row in current)),
        invalid_current=invalid, human_samples=samples)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:18000")
    parser.add_argument("--json", type=Path, required=True)
    args = parser.parse_args()
    report = audit(args.base_url)
    args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False))
