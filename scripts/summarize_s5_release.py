"""Summarize immutable eval/report cohorts, including gaps and cold/warm costs."""

import argparse
import json
from collections import Counter
from pathlib import Path


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def final_judgments(case):
    return [
        j
        for j in case["judgments"]
        if j["index"] >= case.get("final_findings_offset", 0)
    ]


def evaluation_summary(data):
    assert data["completed_cases"] == data["total_cases"]
    supported_cases = [
        c
        for c in data["cases"]
        if any(j["supported"] == "yes" for j in final_judgments(c))
    ]
    return {
        k: data[k]
        for k in (
            "batch_id",
            "retrieval_hits",
            "retrieval_expected",
            "faithful_findings",
            "findings_total",
            "supported_findings",
            "judged_findings",
            "total_tokens",
            "technical_error_cases",
            "human_review_count",
            "elapsed_seconds",
        )
    } | {
        "retrieval_tokens": sum(c["retrieval_tokens"] for c in data["cases"]),
        "cases_with_supported_final_findings": len(supported_cases),
        "cases_without_supported_final_findings": [
            c["question_id"] for c in data["cases"] if c not in supported_cases
        ],
        "supported_does_not_certify_question_completeness": True,
    }


def stability_summary(data):
    runs = data["runs"]
    rows = []
    for r in runs:
        plans = r.get("retrieval_planning", [])
        rows.append(
            {
                "task_id": r["task_id"],
                "status": r["status"],
                "tokens": r["tokens"],
                "retrieval_tokens": sum(p["tokens"] for p in plans),
                "cache_hits": sum(bool(p.get("cache_hit")) for p in plans),
                "planning_requests": sum(p.get("requests", 0) for p in plans),
                "elapsed_seconds": r["elapsed_seconds"],
                "first_failed": r["first_failed"],
            }
        )
    return {
        "runs": rows,
        "statuses": dict(Counter(r["status"] for r in runs)),
        "backtrack_distribution": dict(Counter(r["backtrack_count"] for r in runs)),
        "first_failed": sum(r["first_failed"] for r in runs),
        "known_tokens": sum(r["tokens"] for r in runs),
        "elapsed_seconds": sum(r["elapsed_seconds"] for r in runs),
        "cache_hits": sum(r["cache_hits"] for r in rows),
        "retrieval_tokens": sum(r["retrieval_tokens"] for r in rows),
        "planning_requests": sum(r["planning_requests"] for r in rows),
    }


def compare(before, after, before_stability, after_stability):
    assert before["suite_sha256"] == after["suite_sha256"]
    assert {c["judge_prompt_sha256"] for c in before["cases"]} == {
        c["judge_prompt_sha256"] for c in after["cases"]
    }
    previous, current = evaluation_summary(before), evaluation_summary(after)
    old_runs, new_runs = (
        stability_summary(before_stability),
        stability_summary(after_stability),
    )
    by_question = {c["question_id"]: c for c in after["cases"]}
    unresolved = []
    for c in before["cases"]:
        for j in c["judgments"]:
            if j["supported"] == "yes":
                continue
            old = c["findings"][j["index"]]
            now = by_question[c["question_id"]]
            final_yes = [
                now["findings"][k["index"]]
                for k in final_judgments(now)
                if k["supported"] == "yes"
            ]
            unresolved.append(
                {
                    "question_id": c["question_id"],
                    "old_finding_index": j["index"],
                    "old_clause_id": old["clause_id"],
                    "old_reason": j["reason"],
                    "same_source_supported_now": any(
                        f["clause_id"] == old["clause_id"] for f in final_yes
                    ),
                    "other_final_supported_sources": [
                        f["clause_id"]
                        for f in final_yes
                        if f["clause_id"] != old["clause_id"]
                    ],
                    "human_review": None,
                    "note": "其他法源获支持不等于原条款已修复或问题完整回答",
                }
            )
    return {
        "before": previous,
        "after": current,
        "previous_unsupported_attempts": unresolved,
        "baseline_stability": old_runs,
        "release_stability": new_runs,
        "cold_eval_token_reduction": 1
        - current["total_tokens"] / previous["total_tokens"],
        "ten_report_token_reduction": 1
        - new_runs["known_tokens"] / old_runs["known_tokens"],
        "note": "真实计费以供应商账单为准；缓存冷/热分开，拒答不计通过，人工复核仍待核对。",
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("before", "after", "before-stability", "after-stability", "output"):
        p.add_argument("--" + name, type=Path, required=True)
    args = p.parse_args()
    if args.output.exists():
        raise ValueError("refuse to overwrite previous evidence")
    result = compare(
        read(args.before),
        read(args.after),
        read(args.before_stability),
        read(args.after_stability),
    )
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                k: v
                for k, v in result.items()
                if k
                in (
                    "before",
                    "after",
                    "cold_eval_token_reduction",
                    "ten_report_token_reduction",
                )
            },
            ensure_ascii=False,
        )
    )
