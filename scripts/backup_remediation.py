"""Read-only consistent local PG + content-addressed archive backup. No cleanup/reset.

Run from backend with its uv environment. Output stays outside Git by default.
The database snapshot can contain private records; never commit its contents.
"""

import argparse
import asyncio
import hashlib
import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import asyncpg
import pgserver


async def backup(home: Path, output: Path):
    port = int((home / "pg" / "postmaster.pid").read_text().splitlines()[3])
    uri = f"postgresql://postgres@127.0.0.1:{port}/postgres"
    bindir = Path(pgserver.__file__).parent / "pginstall" / "bin"
    output.mkdir(parents=True, exist_ok=False)
    conn = await asyncpg.connect(uri)
    try:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            snapshot = await conn.fetchval("SELECT pg_export_snapshot()")
            tables = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
            counts = {}
            for row in tables:
                name = row["tablename"]
                counts[name] = await conn.fetchval('SELECT count(*) FROM "' + name.replace('"', '""') + '"')
            result = await asyncio.to_thread(subprocess.run, [str(bindir / "pg_dump.exe"),
                "--dbname", uri, "--format=custom", "--snapshot", snapshot,
                "--file", str(output / "database.dump")], capture_output=True)
            if result.returncode:
                raise RuntimeError("pg_dump failed; backup incomplete")
            versions = await conn.fetch("SELECT id, content_hash, archive_path FROM document_versions")
            archives = []
            root = (home / "archive").resolve()
            for version in versions:
                relative = Path(version["archive_path"])
                source = (root / relative).resolve()
                if root not in source.parents:
                    raise ValueError("archive path outside configured root")
                content = source.read_bytes()
                digest = hashlib.sha256(content).hexdigest()
                if digest != version["content_hash"]:
                    raise ValueError(f"archive integrity failed: version {version['id']}")
                target = output / "archive" / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                archives.append(dict(version_id=str(version["id"]), path=str(relative), sha256=digest))
    finally:
        await conn.close()
    listing = subprocess.run([str(bindir / "pg_restore.exe"), "--list", str(output / "database.dump")], capture_output=True)
    if listing.returncode:
        raise RuntimeError("backup catalog validation failed")
    manifest = dict(created_at=datetime.now().astimezone().isoformat(), table_counts=counts,
        database_sha256=hashlib.sha256((output / "database.dump").read_bytes()).hexdigest(),
        archives=archives, catalog_valid=True)
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(dict(output=str(output), tables=len(counts), versions=len(archives),
        database_sha256=manifest["database_sha256"], catalog_valid=True), ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home() / ".cyshuhe-dev")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or args.home / "backups" / datetime.now().strftime("%Y%m%d-%H%M%S-S0")
    asyncio.run(backup(args.home, output))
