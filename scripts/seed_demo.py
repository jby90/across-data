"""Idempotent DEMO-only account bootstrap after restoring verified five-country data.

Restore a protected pg_dump and its hash-checked archive into a separate *_demo
database first. This command never imports legacy seed text or invents reports.
Passwords are accepted only through CYSH_DEMO_PASSWORDS_JSON in the environment.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))


async def main():
    from sqlalchemy import func, select
    from sqlalchemy.engine import make_url

    from app import models_registry  # noqa: F401
    from app.analysis.models import AnalysisTask
    from app.core.config import get_settings
    from app.core.db import get_sessionmaker
    from app.core.rls import system_scope
    from app.iam.models import Organization, Role, User
    from app.iam.service import create_org, create_user
    from app.ingestion.archive import ArchiveStore
    from app.ingestion.models import Document, DocumentVersion

    settings = get_settings()
    if settings.env != "demo" or not (make_url(settings.database_url).database or "").endswith(
        "_demo"
    ):
        raise RuntimeError("拒绝在非独立 *_demo 数据库运行")
    passwords = json.loads(os.environ["CYSH_DEMO_PASSWORDS_JSON"])
    emails = {
        "demo-admin@example.invalid": Role.PLATFORM_ADMIN,
        "demo-reviewer@example.invalid": Role.REVIEWER,
        "demo-org-admin@example.invalid": Role.ORG_ADMIN,
        "demo-member@example.invalid": Role.MEMBER,
    }
    if set(passwords) != set(emails) or any(len(p) < 16 for p in passwords.values()):
        raise RuntimeError("四个演示账号需要独立的至少16字符密码，不打印密码")
    with system_scope("explicit isolated demonstration bootstrap"):
        async with get_sessionmaker()() as session:
            countries = set(await session.scalars(select(Document.country_code).distinct()))
            if not {"SG", "EG", "TN", "MA", "LB"} <= countries:
                raise RuntimeError("请先恢复已核验的五国资产，不能用虚构法源补齐")
            store = ArchiveStore(settings.archive_dir)
            for version in await session.scalars(select(DocumentVersion)):
                data = store.read(version.archive_path)
                if len(data) != version.size_bytes:
                    raise RuntimeError("原文长度不匹配")
            org_name = os.environ.get("CYSH_DEMO_ORG_NAME", "演示车企")
            org = await session.scalar(select(Organization).where(
                Organization.name == org_name))
            if org is None:
                org = await create_org(session, org_name)
            created = 0
            for email, role in emails.items():
                existing = await session.scalar(select(User).where(User.email == email))
                expected_org = org.id if role in (Role.ORG_ADMIN, Role.MEMBER) else None
                if existing:
                    if existing.org_id != expected_org or existing.role != role:
                        raise RuntimeError("已有演示账号归属不匹配，禁止覆盖")
                    continue
                await create_user(session, email=email, display_name="演示" + role.value,
                                  password=passwords[email], role=role, org_id=expected_org)
                created += 1
            completed = await session.scalar(select(func.count()).select_from(AnalysisTask).where(
                AnalysisTask.status == "completed", AnalysisTask.org_id == org.id))
            await session.commit()
            print(json.dumps({"new_accounts": created, "preserved_completed_tasks": completed,
                              "sample_gap": max(0, 2 - (completed or 0)),
                              "note": "示例仅复用恢复的历史任务，不伪造已完成报告或审批。"},
                             ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
