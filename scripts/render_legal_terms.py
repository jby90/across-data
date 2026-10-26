"""Bounded, cached Chinese rendering of archived definitions. Run from repository root.

CYSH_DATABASE_URL / CYSH_LLM_API_KEY enter only through the operator environment.
No network crawl, source modification, or human approval occurs here.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app import models_registry  # noqa: F401
from app.analysis.llm import LlmUnavailableError, call_llm_json
from app.core.db import get_engine, get_sessionmaker
from app.core.rls import DatabaseIdentity, database_scope
from app.knowledge.legal_terms import CATALOG, current_definitions
from app.knowledge.term_rendering import RenderingBatch, append_rendering


async def run(args):
    stats = {
        "model_calls": 0,
        "request_attempts": 0,
        "tokens": 0,
        "accepted": 0,
        "failed": [],
        "skipped_cached": 0,
    }
    async with get_sessionmaker()() as session:
        from app.ingestion.models import Document
        from sqlalchemy import select
        from sqlalchemy.exc import SQLAlchemyError

        codes = (
            [args.country]
            if args.country
            else list(
                (await session.scalars(select(Document.country_code).distinct())).all()
            )
        )
        rows = {
            r.id: r for code in codes for r in await current_definitions(session, code)
        }
        pending = []
        for row in rows.values():
            if getattr(row, "definition_zh_display", None) or row.definition_zh:
                stats["skipped_cached"] += 1
            else:
                pending.append(row)
        cache = {}
        while (
            pending
            and stats["model_calls"] < args.max_calls
            and stats["tokens"] < args.token_budget
        ):
            batch = []
            chars = 0
            while pending and len(batch) < args.batch_size:
                row = pending.pop(0)
                cache_key = (row.source_term, row.definition_original, row.target_zh)
                if cache_key in cache:
                    from app.knowledge.term_rendering import Rendering

                    value = Rendering(definition_id=row.id, **cache[cache_key])
                    try:
                        await append_rendering(session, row, value)
                        stats["accepted"] += 1
                    except ValueError as e:
                        stats["failed"].append({"id": str(row.id), "reason": str(e)})
                    continue
                if chars + len(row.definition_original) > 18000 and batch:
                    pending.insert(0, row)
                    break
                batch.append(row)
                chars += len(row.definition_original)
            if not batch:
                await session.commit()
                continue
            await (
                session.commit()
            )  # Cached, verified renderings are durable before a network call.
            remaining_ids = [r.id for r in pending]
            accepted_before = stats["accepted"]
            prescribed = {code: target for code, target, _ in CATALOG}
            payload = [
                {
                    "definition_id": str(r.id),
                    "source_term": r.source_term,
                    "definition_original": r.definition_original,
                    "prescribed_target": (
                        r.target_zh
                        if r.registration_origin == "manual"
                        else prescribed.get(r.concept_code)
                    ),
                }
                for r in batch
            ]
            try:
                result = await call_llm_json(
                    [
                        {
                            "role": "system",
                            "content": "你是法律术语翻译员。只根据所给定义生成中文暂译和完整中文定义。"
                            "不添加任何法律判断、引用或背景事实，保留全部数字、编号、金额、日期、例外与条件。"
                            "prescribed_target不为空时必须逐字使用；否则给出简洁中文暂译。"
                            "definition_zh必须包含target_zh。仅返回指定JSON。",
                        },
                        {
                            "role": "user",
                            "content": json.dumps(
                                {
                                    "definitions": payload,
                                    "output_schema": RenderingBatch.model_json_schema(),
                                },
                                ensure_ascii=False,
                            ),
                        },
                    ],
                    temperature=0,
                    max_tokens=8000,
                    timeout=150,
                )
                stats["tokens"] += result.total_tokens
                stats["request_attempts"] += result.request_attempts
                parsed = RenderingBatch.model_validate(result.data)
                allowed = {r.id: r for r in batch}
                seen = set()
                for value in parsed.definitions:
                    if (
                        value.definition_id not in allowed
                        or value.definition_id in seen
                    ):
                        stats["failed"].append(
                            {"reason": "unbound_or_duplicate_definition"}
                        )
                        continue
                    row = allowed[value.definition_id]
                    seen.add(row.id)
                    try:
                        await append_rendering(session, row, value)
                        stats["accepted"] += 1
                        cache[
                            (row.source_term, row.definition_original, row.target_zh)
                        ] = value.model_dump(exclude={"definition_id"})
                    except ValueError as e:
                        stats["failed"].append({"id": str(row.id), "reason": str(e)})
                stats["failed"] += [
                    {"id": str(r.id), "reason": "missing_model_item"}
                    for r in batch
                    if r.id not in seen
                ]
                await session.commit()
            except (LlmUnavailableError, ValueError, SQLAlchemyError) as e:
                await session.rollback()
                stats["accepted"] = accepted_before
                from app.knowledge.models import LegalTermDefinition

                pending = (
                    list(
                        (
                            await session.scalars(
                                select(LegalTermDefinition).where(
                                    LegalTermDefinition.id.in_(remaining_ids)
                                )
                            )
                        ).all()
                    )
                    if remaining_ids
                    else []
                )
                stats["tokens"] += getattr(e, "tokens", 0)
                stats["request_attempts"] += len(getattr(e, "failures", []))
                stats["failed"].append({"reason": type(e).__name__})
            stats["model_calls"] += 1
            print(
                json.dumps({k: v for k, v in stats.items() if k != "failed"}),
                flush=True,
            )
        stats["remaining_not_called"] = len(pending)
    await get_engine().dispose()
    Path(args.output).write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--country")
    parser.add_argument("--max-calls", type=int, default=30)
    parser.add_argument("--batch-size", type=int, choices=range(1, 11), default=8)
    parser.add_argument("--token-budget", type=int, default=100000)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    with database_scope(DatabaseIdentity(scope="system")):
        asyncio.run(run(args))
