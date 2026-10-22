#!/usr/bin/env python3
"""本地一键演示环境(仅开发用,不用于生产)。

启动:嵌入式 PostgreSQL(数据保存在 ~/.cyshuhe-dev)、数据库迁移、演示账号与演示站点、API、后台 worker。
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
DEMO_SITE_URL = "https://example.com/"


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
    from app.core.db import get_sessionmaker
    from app.iam import service
    from app.iam.models import Organization, Role, User
    from app.ingestion.phase1_import import consolidate_egazette_sites
    from app.sources.models import SourceSite

    sf = get_sessionmaker()
    async with sf() as s:
        await consolidate_egazette_sites(s)
        org = (
            await s.execute(select(Organization).where(Organization.name == DEMO_ORG))
        ).scalar_one_or_none()
        if org is None:
            org = await service.create_org(s, DEMO_ORG)
        admin_id = None
        for email, name, role, in_org in DEMO_USERS:
            user = (await s.execute(select(User).where(User.email == email))).scalar_one_or_none()
            if user is None:
                user = await service.create_user(
                    s,
                    email=email,
                    display_name=name,
                    password=creds["users"][email],
                    role=Role(role),
                    org_id=org.id if in_org else None,
                )
            if role == "platform_admin":
                admin_id = user.id

        site = (
            await s.execute(select(SourceSite).where(SourceSite.entry_url == DEMO_SITE_URL))
        ).scalar_one_or_none()
        if site is None:
            from datetime import UTC, datetime

            s.add(
                SourceSite(
                    country_code="ZZ",
                    publisher="【演示数据】IANA 示例域名页面",
                    language="en",
                    document_type="OTHER",
                    entry_url=DEMO_SITE_URL,
                    strategy="DIRECT",
                    config={},
                    interval_hours=24,
                    terms_status="approved",
                    terms_note="本地演示:example.com 是 IANA 保留给文档示例的域名,不是真实法规来源。",
                    terms_decided_by=admin_id,
                    terms_decided_at=datetime.now(UTC),
                    enabled=True,
                    next_run_at=datetime.now(UTC),
                )
            )
            await s.commit()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--allow-fake-ip", action="store_true")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    creds = load_secrets()
    pg = pgserver.get_server(DEV / "pg", cleanup_mode="stop")
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
        for email, name, role, _ in DEMO_USERS:
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
