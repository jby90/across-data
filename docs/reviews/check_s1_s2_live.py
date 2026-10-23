"""Read-only API supplement to audit_platform.py; never starts LLM/crawl/review jobs.

Run from backend with its existing Python environment. Reads local demo credentials
without printing or saving passwords/tokens; writes aggregate counts and public-law
samples for engineering review, not a claim of human legal verification.
"""

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import httpx


def main():
    credentials = json.loads(
        (Path.home() / ".cyshuhe-dev/credentials.json").read_text(encoding="utf-8")
    )
    countries = ["SG", "EG", "TN", "MA", "LB"]
    summary = {}
    samples = []
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=60) as client:
        login = client.post(
            "/api/auth/admin/login",
            json={
                "email": "admin@example.com",
                "password": credentials["users"]["admin@example.com"],
            },
        )
        login.raise_for_status()
        client.headers["Authorization"] = "Bearer " + login.json()["access_token"]

        def get(path, **params):
            response = client.get(path, params=params)
            response.raise_for_status()
            return response.json()

        docs = get("/api/admin/documents", limit=200)
        for country in countries:
            all_clause_ids = set()
            in_scope_clause_ids = set()
            translated_ids = set()
            good_ids = set()
            total_tokens_in = total_tokens_out = 0
            sample_count = 0
            raw_translation_count = 0
            for doc in [d for d in docs if d["country_code"] == country]:
                detail = get(f"/api/admin/documents/{doc['id']}")
                latest = detail.get("latest_version")
                if not latest:
                    continue
                offset = 0
                while True:
                    clauses = get(
                        f"/api/admin/versions/{latest['id']}/clauses", limit=500, offset=offset
                    )
                    ids = {item["id"] for item in clauses}
                    all_clause_ids.update(ids)
                    if doc["relevance"] in ("relevant", "related"):
                        in_scope_clause_ids.update(ids)
                    if len(clauses) < 500:
                        break
                    offset += 500
                translations = get(
                    "/api/admin/knowledge/translations", document_id=doc["id"], limit=500
                )
                if len(translations) == 500:
                    raise RuntimeError(
                        "Document translation endpoint hit its pagination limit; audit incomplete"
                    )
                raw_translation_count += len(translations)
                for translation in translations:
                    translated_ids.add(translation["clause_id"])
                    total_tokens_in += translation.get("tokens_in", 0)
                    total_tokens_out += translation.get("tokens_out", 0)
                    if translation["quality_check_passed"] and translation["review_status"] in (
                        "machine_translated",
                        "pending_review",
                        "approved",
                    ):
                        good_ids.add(translation["clause_id"])
                        if sample_count < 2 and doc["relevance"] in ("relevant", "related"):
                            sample_count += 1
                            samples.append(
                                {
                                    "country": country,
                                    "source_url": doc["source_url"],
                                    "document_title": doc["title"],
                                    "document_relevance": doc["relevance"],
                                    "clause_id": translation["clause_id"],
                                    "clause_number": translation["clause_number"],
                                    "original": translation["original_text"],
                                    "translation": translation["translated_text"],
                                    "review_status": translation["review_status"],
                                    "human_legal_review_performed_in_this_audit": False,
                                }
                            )
            graph = get("/api/admin/knowledge/graph", country_code=country)
            semantic = [edge for edge in graph["edges"] if not edge["is_structural"]]
            structural = [edge for edge in graph["edges"] if edge["is_structural"]]
            court_nodes = [node for node in graph["nodes"] if "Courts (Civil" in node["name_zh"]]
            summary[country] = {
                "latest_clause_count": len(all_clause_ids),
                "in_scope_latest_clause_count": len(in_scope_clause_ids),
                "active_translation_rows": raw_translation_count,
                "unique_translated_latest_clause_count": len(translated_ids & all_clause_ids),
                "qualified_latest_clause_count": len(good_ids & all_clause_ids),
                "qualified_in_scope_latest_clause_count": len(good_ids & in_scope_clause_ids),
                "tokens_in_recorded": total_tokens_in,
                "tokens_out_recorded": total_tokens_out,
                "semantic_edge_review_status": dict(
                    Counter(edge["review_status"] for edge in semantic)
                ),
                "structural_edge_review_status": dict(
                    Counter(edge["review_status"] for edge in structural)
                ),
                "semantic_judge_reason_matches_rule_fallback": sum(
                    edge.get("judge_reason", "").startswith("证据片段明确包含支撑")
                    for edge in semantic
                ),
                "out_of_scope_courts_node_attributes": [
                    {"name": node["name_zh"], "attributes": node["attributes"]}
                    for node in court_nodes
                ],
            }
    gold = json.loads(
        (Path(__file__).parents[2] / "backend/tests/fixtures/kg_gold.json").read_text(
            encoding="utf-8"
        )
    )
    result = {
        "generated_at": datetime.now(UTC).isoformat(),
        "countries": summary,
        "gold_count": len(gold),
        "gold_positive_count": sum(item["expected_valid"] for item in gold),
        "gold_negative_count": sum(not item["expected_valid"] for item in gold),
        "gold_available_fields": sorted(set().union(*(item.keys() for item in gold))),
        "translation_samples": samples,
    }
    output = Path(__file__).with_name("s1-s2-supplement-20261023.json")
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    compact = {
        country: {
            key: value
            for key, value in values.items()
            if key != "out_of_scope_courts_node_attributes"
        }
        for country, values in summary.items()
    }
    print(
        json.dumps(
            {"countries": compact, "gold_count": len(gold), "saved": str(output)},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
