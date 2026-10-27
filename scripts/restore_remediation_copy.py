"""Restore a verified backup into a NEW isolated database; never replace an existing DB.

Run from backend: uv run python ../scripts/restore_remediation_copy.py BACKUP_DIR
"""

import argparse
import asyncio
import hashlib
import json
import re
import subprocess
from pathlib import Path

import asyncpg
import pgserver


async def restore(backup: Path, name: str):
    if not re.fullmatch(r"remediation_[a-z0-9_]+", name):
        raise ValueError("isolated database name must start with remediation_")
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    if hashlib.sha256((backup / "database.dump").read_bytes()).hexdigest() != manifest["database_sha256"]:
        raise ValueError("database backup hash mismatch")
    home = Path.home() / ".cyshuhe-dev"
    port = int((home / "pg" / "postmaster.pid").read_text().splitlines()[3])
    uri = f"postgresql://postgres@127.0.0.1:{port}/postgres"
    conn = await asyncpg.connect(uri)
    try:
        if await conn.fetchval("SELECT 1 FROM pg_database WHERE datname=$1", name):
            raise ValueError("target exists; refusing to replace it")
        await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()
    target = uri.rsplit("/", 1)[0] + "/" + name
    binary = Path(pgserver.__file__).parent / "pginstall/bin/pg_restore.exe"
    result = subprocess.run([str(binary), "--exit-on-error", "--dbname", target,
                             str(backup / "database.dump")], capture_output=True)
    if result.returncode:
        raise RuntimeError("restore failed; isolated DB retained for diagnosis")
    conn = await asyncpg.connect(target)
    try:
        for table, count in manifest["table_counts"].items():
            actual = await conn.fetchval('SELECT count(*) FROM "' + table.replace('"', '""') + '"')
            if actual != count:
                raise ValueError(f"restored count mismatch: {table}")
    finally:
        await conn.close()
    print(json.dumps(dict(database=name, counts_verified=True, source_unchanged=True,
        database_url=target.replace("postgresql://", "postgresql+asyncpg://")), ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup", type=Path)
    parser.add_argument("--database", default="remediation_20261023")
    args = parser.parse_args()
    asyncio.run(restore(args.backup, args.database))
