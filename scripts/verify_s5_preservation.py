"""Read-only old-row/approval/archive verification after the isolated S5 upgrade."""

import argparse
import asyncio
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

import asyncpg


def equivalent(key, before, after):
    if key.endswith("_at") and isinstance(before, str) and isinstance(after, str):
        return datetime.fromisoformat(before) == datetime.fromisoformat(after)
    return before == after


def baseline_rows(rows, json_columns):
    if isinstance(rows, dict):
        assert all(str(row["id"]) == key for key, row in rows.items())
        rows = list(rows.values())
    normalized = []
    for row in rows:
        values = dict(row)
        for key in json_columns:
            if isinstance(values.get(key), str):
                values[key] = json.loads(values[key])
        normalized.append(values)
    assert len({str(r["id"]) for r in normalized}) == len(normalized)
    return normalized


async def verify(baseline, output, database, archive_directory=None):
    if not database.startswith("remediation_"):
        raise ValueError("Only isolated remediation databases are allowed")
    before = json.loads(baseline.read_text(encoding="utf-8"))
    conn = await asyncpg.connect(f"postgresql://postgres@127.0.0.1:3739/{database}")
    additions, activation_changes = {}, []
    checked = archives = 0
    try:
        async with conn.transaction(readonly=True, isolation="repeatable_read"):
            snapshots = []
            for row in await conn.fetch("""SELECT payload,content_hash FROM knowledge_audit_records
                WHERE action='workflow_before_activation'"""):
                payload = json.loads(row["payload"])
                digest = hashlib.sha256(
                    json.dumps(
                        payload,
                        sort_keys=True,
                        ensure_ascii=False,
                    ).encode()
                ).hexdigest()
                assert digest == row["content_hash"]
                snapshots.append(payload)
            for table, rows in before.items():
                assert re.fullmatch(r"[a-z_]+", table)
                json_columns = [
                    r["column_name"]
                    for r in await conn.fetch(
                        """SELECT column_name FROM information_schema.columns WHERE
                    table_schema='public' AND table_name=$1 AND data_type IN ('json','jsonb')""",
                        table,
                    )
                ]
                rows = baseline_rows(rows, json_columns)
                current = {
                    str(item["id"]): item
                    for row in await conn.fetch(
                        f'SELECT row_to_json(t)::text payload FROM "{table}" t'
                    )
                    if (item := json.loads(row["payload"]))
                }
                additions[table] = len(current) - len(rows)
                assert additions[table] >= 0
                for old in rows:
                    checked += 1
                    new = current[str(old["id"])]
                    changed = [
                        key
                        for key, value in old.items()
                        if not equivalent(key, value, new[key])
                    ]
                    if changed:
                        assert table == "agent_workflow_configs" and changed == [
                            "is_active"
                        ], (
                            table,
                            old["id"],
                            changed,
                        )
                        assert any(
                            all(
                                equivalent(k, v, payload.get(k)) for k, v in old.items()
                            )
                            for payload in snapshots
                        ), "activation must have original snapshot"
                        activation_changes.append(old["version_no"])
            archive_root = Path(
                archive_directory
                or "C:/Users/Jin/.cyshuhe-dev/remediation_20261023/archive"
            ).resolve()
            for row in await conn.fetch(
                "SELECT archive_path,content_hash FROM document_versions"
            ):
                path = (archive_root / row["archive_path"]).resolve()
                assert archive_root in path.parents
                assert (
                    hashlib.sha256(path.read_bytes()).hexdigest() == row["content_hash"]
                )
                archives += 1
            active = await conn.fetch("""SELECT version_no,runtime_schema_version
                FROM agent_workflow_configs WHERE is_active""")
            result = {
                "passed": True,
                "baseline_sha256": hashlib.sha256(baseline.read_bytes()).hexdigest(),
                "database": database,
                "old_rows_checked": checked,
                "old_tables_checked": len(before),
                "source_evidence_approval_overwrites": 0,
                "legacy_execution_ids_backfilled": 0,
                "archives_verified": archives,
                "old_activation_only_changes_with_snapshots": activation_changes,
                "additions": additions,
                "active_workflows": [dict(r) for r in active],
            }
    finally:
        await conn.close()
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--json", type=Path, required=True)
    parser.add_argument("--database", default="remediation_s5_20261024")
    parser.add_argument("--archive-root", type=Path)
    args = parser.parse_args()
    asyncio.run(verify(args.baseline, args.json, args.database, args.archive_root))
