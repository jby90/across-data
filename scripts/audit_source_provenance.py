"""Read-only API audit of all source archive hashes and acquisition evidence."""

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

import httpx

from audit_platform import _fetch_all_documents, _login_admin


def audit(base_url: str):
    credentials = json.loads((Path.home() / ".cyshuhe-dev/credentials.json").read_text(encoding="utf-8"))
    with httpx.Client(base_url=base_url, timeout=60) as client:
        token = _login_admin(client, "admin@example.com", credentials["users"]["admin@example.com"])
        headers = {"Authorization": "Bearer " + token}
        documents = _fetch_all_documents(client, headers)
        results = []
        for doc in documents:
            response = client.get(f"/api/admin/documents/{doc['id']}/provenance", headers=headers)
            response.raise_for_status()
            results.append(response.json())
        response = client.get("/api/admin/sites", headers=headers)
        response.raise_for_status()
        sites = [{key: site.get(key) for key in ("id", "country_code", "entry_url", "terms_status",
            "terms_note", "terms_approval_scope", "terms_decided_at")} for site in response.json()]
    versions = [version for result in results for version in result["versions"]]
    by_hash = defaultdict(list)
    for result in results:
        for version in result["versions"]:
            by_hash[version["content_hash"]].append({"document_id": result["document_id"],
                "title": result["title"], "source_url": result["source_url"]})
    collisions = [{"sha256": digest, "documents": documents,
        "assessment": "requires_source_binding_review"} for digest, documents in by_hash.items()
        if len({row["title"] for row in documents}) > 1]
    return dict(documents=results, source_decisions=sites,
        duplicate_hash_different_titles=collisions,
        summary=dict(document_counts=dict(Counter(row["country_code"] for row in results)),
            held_documents=sum(bool(row.get("source_integrity_holds")) for row in results),
            duplicate_hash_different_title_groups=len(collisions),
            versions=len(versions), integrity_passed=sum(row["integrity_ok"] for row in versions),
            acquisition=dict(Counter(row["acquisition"] for row in versions)),
            missing_effective_date=sum(not row["effective_date"] for row in versions),
            official_authenticity="pending_human_review"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:18000")
    parser.add_argument("--json", type=Path, required=True)
    args = parser.parse_args()
    report = audit(args.base_url)
    args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False))
