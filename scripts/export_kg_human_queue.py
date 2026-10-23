"""Export real source-bound candidates; no generated gold labels or automatic approvals."""

import argparse
import json
from collections import defaultdict
from pathlib import Path

import httpx

from audit_platform import _login_admin

RELATIONS = ("PUBLISHES", "AMENDS", "REPEALS", "GOVERNS", "PROHIBITS", "REQUIRES", "EXEMPTS",
             "SUPERVISES", "PENALIZES", "APPLIES_TO_SCENARIO", "RESTRICTS_CROSS_BORDER", "REQUIRES_ADMISSION")


def export(base_url):
    credentials = json.loads((Path.home() / ".cyshuhe-dev/credentials.json").read_text(encoding="utf-8"))
    with httpx.Client(base_url=base_url, timeout=60) as client:
        token = _login_admin(client, "admin@example.com", credentials["users"]["admin@example.com"])
        headers = {"Authorization": "Bearer " + token}
        groups = defaultdict(list)
        for country in ("SG", "EG", "TN", "MA", "LB"):
            response = client.get("/api/admin/knowledge/graph", headers=headers,
                                  params={"country_code": country})
            response.raise_for_status()
            for edge in response.json()["edges"]:
                groups[edge["relation_type"]].append(edge)
        selected, missing, clause_cache = [], {}, {}
        for relation in RELATIONS:
            choices = sorted(groups[relation], key=lambda edge: (edge["country_code"], edge["id"]))[:5]
            missing[relation] = max(0, 5 - len(choices))
            for edge in choices:
                version_id = edge["evidence_version_id"]
                if version_id not in clause_cache:
                    clauses = []
                    offset = 0
                    while True:
                        response = client.get(f"/api/admin/versions/{version_id}/clauses", headers=headers,
                                              params={"limit": 500, "offset": offset})
                        response.raise_for_status()
                        page = response.json()
                        clauses.extend(page)
                        if len(page) < 500:
                            break
                        offset += 500
                    clause_cache[version_id] = {row["id"]: row for row in clauses}
                clause = clause_cache[version_id][edge["evidence_clause_id"]]
                selected.append(dict(candidate=edge, original_text=clause["original_text"],
                    page_start=clause["page_start"], page_end=clause["page_end"],
                    quote_matches_source=edge["evidence_quote"] in clause["original_text"],
                    reviewer=None, human_supports=None, reviewed_at=None, human_reason=None))
    return dict(status="pending_human_review", requested_per_relation=5, required_confirmed=60,
        exported_count=len(selected), human_confirmed=0, missing_per_relation=missing,
        acceptance_passed=False, items=selected)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:18000")
    parser.add_argument("--json", type=Path, required=True)
    args = parser.parse_args()
    report = export(args.base_url)
    args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "items"}, ensure_ascii=False))
