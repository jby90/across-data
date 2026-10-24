"""Read-only first-verification diagnosis. Empty/no-model verification is not success."""

import argparse
import asyncio
import json
from collections import Counter
from pathlib import Path

import asyncpg

CATEGORIES = {
    "quote_not_substring": (
        "不是该条款原文的连续子串",
        "not_substring",
        "引用片段过短",
        "sentence_id",
    ),
    "numeric_mismatch": ("结论中的数字", "数字不一致", "numeric_mismatch"),
    "outside_retrieval": ("检索结果之外", "不在检索结果", "outside_retrieval"),
    "missing_clause_number": ("条号不存在", "missing_clause_number"),
}


def summarize(tasks, traces):
    reasons = Counter()
    evaluated = failed = no_evidence_checked = technical_failures = 0
    details = []
    for task in tasks:
        rows = sorted(
            (t for t in traces if str(t["task_id"]) == str(task["id"])),
            key=lambda t: t["seq_no"],
        )
        for trace in rows:
            if trace["phase"] != "verification":
                continue
            out = trace.get("output_snapshot_json") or {}
            if isinstance(out, str):
                out = json.loads(out)
            problems = out.get("problems") or []
            checked = out.get("verification_items") or []
            is_failed = bool(problems) or trace["status"] in ("failed", "backtracked")
            did_check = bool(checked or problems)
            if did_check:
                evaluated += 1
                failed += int(is_failed)
            else:
                no_evidence_checked += 1
            preceding = [
                r
                for r in rows
                if r["seq_no"] < trace["seq_no"]
                and r["phase"] in ("analysis", "fact_check")
            ]
            prior_analysis = next(
                (r for r in reversed(preceding) if r["phase"] == "analysis"), None
            )
            if prior_analysis and prior_analysis["status"] == "failed":
                technical_failures += 1
            for problem in problems:
                # Corrupted historical text cannot be classified by guessing its meaning.
                matches = [
                    key
                    for key, words in CATEGORIES.items()
                    if "\ufffd" not in str(problem)
                    and any(w in str(problem) for w in words)
                ]
                reasons.update(matches or ["unclassifiable_historical_text"])
            details.append(
                {
                    "task_id": str(task["id"]),
                    "trace_id": str(trace["id"]),
                    "seq_no": trace["seq_no"],
                    "evaluated": did_check,
                    "first_verification_failed": is_failed if did_check else None,
                    "problem_entries": len(problems),
                }
            )
    entries = sum(reasons.values())
    return {
        "tasks": len(tasks),
        "first_verification_runs": len(details),
        "evaluated_runs": evaluated,
        "first_failed_runs": failed,
        "first_failure_rate_percent": round(100 * failed / evaluated, 2)
        if evaluated
        else None,
        "no_evidence_checked_runs": no_evidence_checked,
        "preceding_analysis_failed_runs": technical_failures,
        "reason_counts": dict(reasons),
        "reason_percent_of_categorized_entries": {
            k: round(100 * v / entries, 2) for k, v in reasons.items()
        },
        "backtrack_count_distribution": dict(
            Counter(str(t.get("backtrack_count", 0)) for t in tasks)
        ),
        "task_status_distribution": dict(Counter(t["status"] for t in tasks)),
        "partial_sample_rate_percent": round(
            100 * sum(t["status"] == "partial_sample" for t in tasks) / len(tasks), 2
        )
        if tasks
        else None,
        "details": details,
        "note": "原因允许多标签；没有实际核验结论的轨迹不计作首轮通过。历史乱码归为无法分类，不能推测。",
    }


async def diagnose(url, limit):
    conn = await asyncpg.connect(url.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        async with conn.transaction(readonly=True, isolation="repeatable_read"):
            tasks = [
                dict(r)
                for r in await conn.fetch(
                    """SELECT id,status,backtrack_count
                FROM analysis_tasks ORDER BY created_at DESC LIMIT $1""",
                    limit,
                )
            ]
            traces = [
                dict(r)
                for r in await conn.fetch(
                    """SELECT id,task_id,seq_no,phase,status,
                output_snapshot_json FROM analysis_step_traces WHERE task_id=ANY($1::uuid[])""",
                    [t["id"] for t in tasks],
                )
            ]
            return summarize(tasks, traces)
    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--json", type=Path, required=True)
    parser.add_argument("--compare", type=Path)
    args = parser.parse_args()
    result = asyncio.run(diagnose(args.database_url, args.limit))
    if args.compare:
        before = json.loads(args.compare.read_text(encoding="utf-8"))
        result["comparison"] = {
            k: {"before": before.get(k), "after": result.get(k)}
            for k in (
                "evaluated_runs",
                "first_failure_rate_percent",
                "backtrack_count_distribution",
                "partial_sample_rate_percent",
            )
        }
    args.json.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {k: v for k, v in result.items() if k != "details"},
            ensure_ascii=False,
            indent=2,
        )
    )
