"""Compare immutable evaluation artifacts, using recorded candidate pools, not rerunning models."""

import argparse
import json
from collections import defaultdict
from pathlib import Path


def summarize(data):
    countries = defaultdict(
        lambda: {"hits": 0, "expected": 0, "supported": 0, "judged": 0}
    )
    misses, rejected, failures = [], [], []
    for case in data["cases"]:
        country = countries[case["country_code"]]
        for key, field in [
            ("hits", "retrieval_hits"),
            ("expected", "retrieval_expected"),
            ("supported", "supported_findings"),
            ("judged", "judged_findings"),
        ]:
            country[key] += case[field]
        planning = next(
            (
                c["arguments"].get("planning", {})
                for c in case.get("retrieval_calls", [])
                if c["tool_name"] == "search_effective_clauses"
            ),
            {},
        )
        for expected in case["expected_clauses"]:
            if any(c["clause_id"] == expected["clause_id"] for c in case["retrieved"]):
                continue
            alternatives = [
                c
                for c in case["retrieved"]
                if c["document_title"] == expected["document_title"]
                and c["clause_number"] == expected["clause_number"]
            ]
            misses.append(
                {
                    "question_id": case["question_id"],
                    "country": case["country_code"],
                    "expected": expected,
                    "stage": "retrieval_failure_pool_unrecorded"
                    if planning.get("error") and "pool_ids" not in planning
                    else "reranking"
                    if expected["clause_id"] in planning.get("pool_ids", [])
                    else "candidate_generation",
                    "same_title_number_other_identity": alternatives,
                    "queries": planning.get("queries"),
                    "heading_queries": planning.get("heading_queries"),
                    "note": "同标题条号不等于已确认同一版本；标注疑点只能人工核查，不改评分",
                }
            )
        for judgment in case["judgments"]:
            if judgment["supported"] == "yes":
                continue
            finding = case["findings"][judgment["index"]]
            rejected.append(
                {
                    "question_id": case["question_id"],
                    "verdict": judgment,
                    "clause_id": finding["clause_id"],
                    "conclusion": finding["obligation"],
                    "quote": finding.get("quote"),
                }
            )
        gaps = [
            {
                "clause_id": f.get("clause_id"),
                "gap": f.get("_generation_gap"),
                "error": f.get("_generation_error"),
                "issues": f.get("_issues"),
            }
            for f in case["findings"]
            if not f.get("_ok")
        ]
        if (
            gaps
            or case.get("analysis_error")
            or case.get("judge_error")
            or planning.get("error")
        ):
            failures.append(
                {
                    "question_id": case["question_id"],
                    "analysis_error": case.get("analysis_error"),
                    "judge_error": case.get("judge_error"),
                    "retrieval_error": planning.get("error"),
                    "attempted_gaps": gaps,
                }
            )
    return {
        "batch_id": data["batch_id"],
        "suite_sha256": data["suite_sha256"],
        "country_metrics": dict(countries),
        "misses": misses,
        "unsupported": rejected,
        "failures_and_gaps": failures,
        "retrieval_tokens": sum(c.get("retrieval_tokens", 0) for c in data["cases"]),
        "final_supported_cases": sum(
            any(
                j["supported"] == "yes"
                and j["index"] >= c.get("final_findings_offset", 0)
                for j in c["judgments"]
            )
            for c in data["cases"]
        ),
        "human_review": "0/32，待核对；本程序只汇总工程与模型指标",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = summarize(json.loads(args.evaluation.read_text(encoding="utf-8")))
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                k: v
                for k, v in result.items()
                if k not in ("misses", "unsupported", "failures_and_gaps")
            },
            ensure_ascii=False,
        )
    )
