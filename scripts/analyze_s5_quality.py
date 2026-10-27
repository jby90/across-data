"""Read-only diagnosis of frozen retrieval misses and independent judge rejections."""

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.analysis.evaluation import suite
from app.analysis.retrieval import task_query
from app.analysis.tools import (
    DIMENSION_KEYWORDS,
    ToolBox,
    clause_relevance,
    is_usable_clause_text,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


async def run(args):
    data = json.loads(args.evaluation.read_text(encoding="utf-8"))
    engine = create_async_engine(
        args.database_url.replace("postgresql://", "postgresql+asyncpg://")
    )
    misses, rejected = [], []
    try:
        async with async_sessionmaker(engine)() as session:
            await session.execute(text("SET TRANSACTION READ ONLY"))
            boxes = {}
            questions = {q["id"]: q for q in suite()["cases"]}
            for case in data["cases"]:
                cc, dim = case["country_code"], case["dimension_code"]
                box = boxes.setdefault(cc, ToolBox(session, cc))
                candidates = await box._load_candidates()
                ranked = sorted(
                    [
                        (c, clause_relevance(c, DIMENSION_KEYWORDS[dim])[0])
                        for c in candidates
                        if is_usable_clause_text(c.text)
                    ],
                    key=lambda row: (
                        -row[1],
                        row[0].document_title,
                        row[0].label,
                        row[0].clause_id,
                    ),
                )
                current = case.get("retrieval_version", "").startswith("query-bm25")
                if current:
                    question = questions[case["question_id"]]
                    await box.search_effective_clauses(
                        dim, query=task_query(question["facts"], dim)
                    )
                    from app.analysis.workflow_engine import EIGHT_DIMENSIONS_SPEC

                    ranked = box._index.rank(
                        " ".join(DIMENSION_KEYWORDS[dim])
                        + " "
                        + dict(EIGHT_DIMENSIONS_SPEC)[dim],
                        task_query(question["facts"], dim),
                    )
                for expected in case["expected_clauses"]:
                    if any(
                        c["clause_id"] == expected["clause_id"]
                        for c in case["retrieved"]
                    ):
                        continue
                    source = next(
                        (c for c in candidates if c.clause_id == expected["clause_id"]),
                        None,
                    )
                    rank = next(
                        (
                            i + 1
                            for i, (c, _) in enumerate(ranked)
                            if c.clause_id == expected["clause_id"]
                        ),
                        None,
                    )
                    category = (
                        "source_unavailable_or_held"
                        if source is None
                        else "text_quality_gate"
                        if not is_usable_clause_text(source.text)
                        else "query_lexical_miss"
                        if current and rank is None
                        else "dimension_keyword_miss"
                        if not current
                        and clause_relevance(source, DIMENSION_KEYWORDS[dim])[0] == 0
                        else "ranked_below_five"
                    )
                    misses.append(
                        {
                            "question_id": case["question_id"],
                            "country_code": cc,
                            "category": category,
                            "expected": expected,
                            "rank": rank,
                            "text_length": len(source.text) if source else None,
                            "original_text": source.text if source else None,
                        }
                    )
                for judge in case["judgments"]:
                    if judge["supported"] != "yes":
                        finding = case["findings"][judge["index"]]
                        rejected.append(
                            {
                                "question_id": case["question_id"],
                                "country_code": cc,
                                "conclusion": finding["obligation"],
                                "quote": finding.get("quote"),
                                "clause_id": finding["clause_id"],
                                "verdict": judge["supported"],
                                "reason": judge["reason"],
                            }
                        )
            await session.rollback()
    finally:
        await engine.dispose()
    result = {
        "suite_sha256": data["suite_sha256"],
        "source_batch": data["batch_id"],
        "miss_counts": dict(Counter(m["category"] for m in misses)),
        "unsupported_count": len(rejected),
        "misses": misses,
        "unsupported": rejected,
        "note": "按批次的检索版本只读诊断，保留原始Judge理由；不改评分、不做人工法律复核。",
    }
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {k: v for k, v in result.items() if k not in ("misses", "unsupported")},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--database-url", required=True)
    p.add_argument("--evaluation", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    asyncio.run(run(p.parse_args()))
