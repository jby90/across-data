#!/usr/bin/env python3
"""本地一键演示环境(仅开发用,不用于生产)。

启动:嵌入式 PostgreSQL(数据保存在 ~/.cyshuhe-dev)、数据库迁移、演示账号、API、后台 worker。
前端请另开终端:pnpm dev:user / pnpm dev:admin。

用法(在 backend 目录):
  uv run python ../scripts/dev_local.py [--allow-fake-ip]

--allow-fake-ip:开发机开着代理软件 fake-IP 模式时需要,放行解析到 198.18.0.0/15 的域名。
演示账号的密码是随机生成的,保存在 ~/.cyshuhe-dev/credentials.json,不入库。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pgserver

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
DEV = Path.home() / ".cyshuhe-dev"

DEMO_ORG = "演示车企"
DEMO_USERS = [
    ("admin@example.com", "平台管理员", "platform_admin", False),
    ("reviewer@example.com", "律所复核员", "reviewer", False),
    ("org-admin@example.com", "演示企业管理员", "org_admin", True),
    ("member@example.com", "演示企业成员", "member", True),
]


def load_secrets() -> dict:
    DEV.mkdir(parents=True, exist_ok=True)
    path = DEV / "credentials.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    data = {
        "jwt_secret": secrets.token_urlsafe(48),
        "users": {email: secrets.token_urlsafe(12) + "-Aa1" for email, *_ in DEMO_USERS},
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


async def seed(creds: dict) -> None:
    from sqlalchemy import select

    from app import models_registry  # noqa: F401
    from app.cli import _ocr_engine, load_sites
    from app.core.config import get_settings
    from app.core.db import get_sessionmaker
    from app.iam import service
    from app.iam.models import Organization, Role, User
    from app.ingestion.archive import ArchiveStore
    from app.ingestion.phase1_import import consolidate_egazette_sites, import_phase1

    sf = get_sessionmaker()
    seed_file = ROOT / "data" / "seed" / "source_sites.json"
    if seed_file.exists():
        await load_sites(seed_file, sf=sf)
    async with sf() as s:
        await consolidate_egazette_sites(s)
        org = (
            await s.execute(select(Organization).where(Organization.name == DEMO_ORG))
        ).scalar_one_or_none()
        if org is None:
            org = await service.create_org(s, DEMO_ORG)
        for email, name, role, in_org in DEMO_USERS:
            user = (await s.execute(select(User).where(User.email == email))).scalar_one_or_none()
            if user is None:
                await service.create_user(
                    s,
                    email=email,
                    display_name=name,
                    password=creds["users"][email],
                    role=Role(role),
                    org_id=org.id if in_org else None,
                )
        await s.commit()

    phase1_dir = ROOT.parent / "AutoDataCross" / "docs" / "phase2" / "data"
    if phase1_dir.exists():
        settings = get_settings()
        archive = ArchiveStore(settings.archive_dir)
        ocr = _ocr_engine()
        for cc in ("SG", "EG", "TN", "MA", "LB"):
            rep = await import_phase1(phase1_dir, cc, sf, archive, ocr)
            if rep.versions_new:
                print(
                    f"[phase1-import] {cc}: 新增文档 {rep.documents_new}, "
                    f"新增版本 {rep.versions_new}, 状态 {dict(rep.states)}",
                    flush=True,
                )

    from app.analysis.workflow_engine import ensure_workflow_config
    from app.knowledge.extractor import extract_knowledge_for_country
    from app.knowledge.models import KgNode

    async with sf() as s:
        has_any_node = (await s.execute(select(KgNode.id).limit(1))).first() is not None
        if not has_any_node:
            for cc in ("SG", "EG", "TN", "MA", "LB"):
                await extract_knowledge_for_country(s, cc, translate_sample_per_doc=4)
        await ensure_workflow_config(s)
        # Startup never deletes existing records. Cleanup requires an explicit backed-up operation.
        await s.commit()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--allow-fake-ip", action="store_true")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    creds = load_secrets()
    pg_dir = DEV / "pg"
    if (pg_dir / "postmaster.pid").exists():
        pg_ctl_bin = Path(pgserver.__file__).parent / "pginstall" / "bin" / "pg_ctl.exe"
        if pg_ctl_bin.exists():
            subprocess.run(
                [str(pg_ctl_bin), "-D", str(pg_dir), "-m", "fast", "stop"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(1)
            # Windows 下 pgserver 把日志写在 PGDATA/log,非正常退出会因该文件被占用报错
            if (pg_dir / "postmaster.pid").exists():
                (pg_dir / "log").unlink(missing_ok=True)
                rec_log = DEV / "pg-recovery.log"
                subprocess.run(
                    [
                        str(pg_ctl_bin),
                        "-D",
                        str(pg_dir),
                        "-w",
                        "-o",
                        '-h "127.0.0.1" -p 55431',
                        "-l",
                        str(rec_log),
                        "start",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                subprocess.run(
                    [str(pg_ctl_bin), "-D", str(pg_dir), "-m", "fast", "stop"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
    pg = pgserver.get_server(pg_dir, cleanup_mode="stop")
    url = pg.get_uri().replace("postgresql://", "postgresql+asyncpg://", 1)
    env = dict(
        os.environ,
        CYSH_ENV="dev",
        CYSH_DATABASE_URL=url,
        CYSH_JWT_SECRET=creds["jwt_secret"],
        CYSH_ARCHIVE_DIR=str(DEV / "archive"),
        CYSH_CRAWL_DELAY_SECONDS="1",
        CYSH_WORKER_POLL_SECONDS="15",
        PYTHONUTF8="1",
    )
    # 本机装了 Tesseract(开发用,放在 ~/tools/tess-env)就自动启用 OCR
    tess = Path.home() / "tools" / "tess-env" / "Library"
    if (tess / "bin" / "tesseract.exe").exists():
        env.setdefault("CYSH_TESSERACT_CMD", str(tess / "bin" / "tesseract.exe"))
        env.setdefault("CYSH_TESSDATA_DIR", str(tess / "share" / "tessdata"))
    if args.allow_fake_ip:
        env["CYSH_CRAWL_ALLOW_RESOLVED_CIDRS"] = '["198.18.0.0/15"]'
    os.environ.update(env)

    r = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=BACKEND, env=env)
    if r.returncode != 0:
        return r.returncode
    asyncio.run(seed(creds))

    # API/worker never retain the migration owner's database identity. This
    # development cluster is loopback-only and already uses trust authentication.
    async def ordinary_runtime_url():
        import hashlib
        from sqlalchemy.ext.asyncio import create_async_engine
        from app.core.runtime_roles import provision_runtime_role

        role = "cyshuhe_local_" + hashlib.sha256(str(pg_dir.resolve()).encode()).hexdigest()[:12]
        migration_engine = create_async_engine(url)
        try:
            runtime = await provision_runtime_role(migration_engine, role, local_trust_only=True)
            return runtime.render_as_string(hide_password=False)
        finally:
            await migration_engine.dispose()

    env["CYSH_DATABASE_URL"] = asyncio.run(ordinary_runtime_url())

    children = [
        subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(args.port)],
            cwd=BACKEND,
            env=env,
        ),
        subprocess.Popen([sys.executable, "-m", "app.worker"], cwd=BACKEND, env=env),
    ]
    try:
        for _ in range(60):
            try:
                if httpx.get(f"http://127.0.0.1:{args.port}/api/health").status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.5)
        print("\n=== 本地演示环境已就绪 ===", flush=True)
        print(f"API: http://127.0.0.1:{args.port}/api/docs", flush=True)
        for email, _name, role, _ in DEMO_USERS:
            side = "管理端" if role in ("platform_admin", "reviewer") else "用户端"
            print(f"  [{side}] {email}  密码: {creds['users'][email]}", flush=True)
        print(f"(账号密码也保存在 {DEV / 'credentials.json'})", flush=True)
        while all(c.poll() is None for c in children):
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for c in children:
            c.terminate()
        pg.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
