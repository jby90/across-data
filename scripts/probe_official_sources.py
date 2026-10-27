"""Research manifest URLs with the product's robots/SSRF/rate/size checks; no LLM/DB."""

import argparse
import asyncio
import json
import os
import sys
import urllib.request
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.ingestion.archive import ArchiveStore
from app.ingestion.fetcher import PoliteFetcher
from app.ingestion.source_research import probe_leads


async def main(args):
    if args.output.exists() or not args.archive_root.exists():
        raise ValueError("a fresh output and an existing dedicated research archive are required")
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    proxies = urllib.request.getproxies()
    proxy = os.environ.get("HTTPS_PROXY") or proxies.get("https")
    async with (
        httpx.AsyncClient(proxy=proxy, timeout=30) as client,
        httpx.AsyncClient(trust_env=False, timeout=60) as direct,
    ):
        fetcher = PoliteFetcher(
            client,
            user_agent="CyShuheSourceAudit/1.0",
            delay_seconds=2,
            allow_resolved_cidrs=args.allow_resolved_cidr,
            fallback_client=direct if proxy else None,
        )
        result = await probe_leads(fetcher, ArchiveStore(args.archive_root), manifest["leads"])
    result["manifest_sha256"] = ArchiveStore.sha256(args.manifest.read_bytes())
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(
        json.dumps(
            {
                "sources": len(result["records"]),
                "downloaded": sum(r["body_downloaded"] for r in result["records"]),
                "model_calls": 0,
                "database_writes": 0,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--archive-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-resolved-cidr", action="append", default=[])
    asyncio.run(main(parser.parse_args()))
