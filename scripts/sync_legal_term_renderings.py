"""Export/import verified terminology translations without repeating model calls.

Only derived rows are appended. Original evidence and manual approvals are never updated.
Every imported item must match an eligible current definition and pass the normal validators.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app import models_registry  # noqa: F401
from app.core.db import get_engine, get_sessionmaker
from app.core.rls import system_scope
from app.ingestion.models import Document
from app.knowledge.legal_terms import (
    current_definitions,
    rebuild_library,
    sha,
    term_payload,
)
from app.knowledge.term_rendering import Rendering, append_rendering
from sqlalchemy import select


def signature(term):
    fields = (
        "concept_code",
        "country_code",
        "language",
        "source_term",
        "document_id",
        "version_id",
        "clause_id",
        "source_kind",
        "applies_to_document_id",
        "quote_start",
        "quote_end",
        "source_text_sha256",
        "definition_original",
    )
    return sha(
        json.dumps(
            {k: str(getattr(term, k)) for k in fields},
            sort_keys=True,
            ensure_ascii=False,
        )
    )


async def run(args):
    result = {
        "mode": args.mode,
        "imported": 0,
        "skipped_cached": 0,
        "unmatched": 0,
        "failed": [],
        "model_calls": 0,
    }
    async with get_sessionmaker()() as session:
        await rebuild_library(session)
        await session.commit()
        codes = list(
            (await session.scalars(select(Document.country_code).distinct())).all()
        )
        rows = {
            signature(row): row
            for country in codes
            for row in await current_definitions(session, country)
        }
        if args.mode == "export":
            items = [
                {
                    "source_signature": key,
                    "source_quote": row.definition_original,
                    "target_zh": row.target_zh,
                    "definition_zh": term_payload(row)["definition_zh"],
                }
                for key, row in rows.items()
                if term_payload(row)["definition_zh"]
            ]
            Path(args.path).write_text(
                json.dumps(
                    {"schema_version": 1, "items": items}, ensure_ascii=False, indent=2
                ),
                encoding="utf-8",
            )
            result["exported"] = len(items)
        else:
            cache = json.loads(Path(args.path).read_text(encoding="utf-8"))
            if (
                cache.get("schema_version") != 1
                or not isinstance(cache.get("items"), list)
                or len(cache["items"]) > 10000
            ):
                raise ValueError("Unsupported or oversized terminology cache")
            for item in cache["items"]:
                row = rows.get(item.get("source_signature"))
                if row is None or item.get("source_quote") != row.definition_original:
                    result["unmatched"] += 1
                    continue
                if term_payload(row)["definition_zh"]:
                    result["skipped_cached"] += 1
                    continue
                try:
                    await append_rendering(
                        session,
                        row,
                        Rendering(
                            definition_id=row.id,
                            target_zh=item["target_zh"],
                            definition_zh=item["definition_zh"],
                        ),
                    )
                    await session.commit()
                    result["imported"] += 1
                except ValueError as exc:
                    result["failed"].append(
                        {
                            "source_signature": item["source_signature"],
                            "reason": str(exc),
                        }
                    )
        if args.result:
            Path(args.result).write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    await get_engine().dispose()
    print(json.dumps(result, ensure_ascii=False))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["export", "import"])
    parser.add_argument("path")
    parser.add_argument("--result")
    args = parser.parse_args()
    with system_scope("Source-bound terminology cache transfer"):
        asyncio.run(run(args))
