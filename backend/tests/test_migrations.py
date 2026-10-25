import os
import subprocess
import sys
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.db import Base
from app.iam import models as _models  # noqa: F401

BACKEND = Path(__file__).resolve().parents[1]


async def test_migrations_apply_and_match_models(pg_url, engine):
    """全新数据库执行 alembic upgrade head 后,表结构必须与 ORM 模型完全一致(防止漏写迁移)。"""
    async with engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(text("DROP DATABASE IF EXISTS mig_test"))
        await conn.execute(text("CREATE DATABASE mig_test"))

    mig_url = pg_url.rsplit("/", 1)[0] + "/mig_test"
    env = dict(os.environ, CYSH_DATABASE_URL=mig_url)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    mig_engine = create_async_engine(mig_url)
    try:
        async with mig_engine.connect() as conn:
            diff = await conn.run_sync(
                lambda sync_conn: compare_metadata(
                    MigrationContext.configure(sync_conn), Base.metadata
                )
            )
        assert diff == [], f"模型与迁移不一致: {diff}"
        async with mig_engine.begin() as conn:
            await conn.execute(
                text("""INSERT INTO knowledge_audit_records
                (id,target_type,target_id,action,payload,content_hash)
                VALUES ('00000000-0000-0000-0000-000000000001','test',
                '00000000-0000-0000-0000-000000000002','original','{}','hash')""")
            )
            for statement in (
                "UPDATE knowledge_audit_records SET action='changed'",
                "DELETE FROM knowledge_audit_records",
            ):
                with pytest.raises(DBAPIError, match="append-only"):
                    async with conn.begin_nested():
                        await conn.execute(text(statement))
            assert (
                await conn.scalar(text("SELECT action FROM knowledge_audit_records")) == "original"
            )
            await conn.execute(
                text("""INSERT INTO agent_workflow_configs
                (id,workflow_key,version_no,name,description,dsl_version,is_active,
                 readonly_locked,nodes_json,edges_json)
                VALUES ('00000000-0000-0000-0000-000000000003','s4-test',1,
                 'test','test','test',true,true,'[]','[]')""")
            )
            for statement in (
                "UPDATE agent_workflow_configs SET nodes_json='[{}]'",
                "DELETE FROM agent_workflow_configs",
            ):
                with pytest.raises(DBAPIError, match="immutable|append-only"):
                    async with conn.begin_nested():
                        await conn.execute(text(statement))
            await conn.execute(text("UPDATE agent_workflow_configs SET is_active=false"))
            await conn.execute(
                text("""INSERT INTO llm_call_audit
                (id,call_id,purpose,model,input_hash,redaction_mode,redaction_hits,
                 stage,tokens_in,tokens_out,duration_ms)
                VALUES ('00000000-0000-0000-0000-000000000004',
                '00000000-0000-0000-0000-000000000005','test','synthetic','hash',
                'on','{}','started',0,0,0)""")
            )
            for statement in (
                "UPDATE llm_call_audit SET model='changed'",
                "DELETE FROM llm_call_audit",
            ):
                with pytest.raises(DBAPIError, match="append-only"):
                    async with conn.begin_nested():
                        await conn.execute(text(statement))
            from datetime import UTC, datetime

            from sqlalchemy.ext.asyncio import AsyncSession

            from app.ingestion.models import Document, DocumentVersion
            from app.sources.models import SourceSite
            from app.updates.models import SourceFetchObservation
            from app.updates.service import create_change

            async with AsyncSession(bind=conn, expire_on_commit=False) as session:
                site = SourceSite(
                    country_code="XX",
                    publisher="Migration fixture",
                    language="en",
                    document_type="LAW",
                    entry_url="https://example.org/synthetic",
                    strategy="DIRECT",
                    config={},
                )
                session.add(site)
                await session.flush()
                doc = Document(
                    site_id=site.id,
                    country_code="XX",
                    source_url="https://example.org/synthetic",
                    title="Migration fixture",
                    language="en",
                    document_type="LAW",
                )
                session.add(doc)
                await session.flush()
                versions = []
                for number in (1, 2):
                    version = DocumentVersion(
                        document_id=doc.id,
                        version_no=number,
                        content_hash=str(number) * 64,
                        archive_path="synthetic",
                        size_bytes=1,
                        mime_type="application/pdf",
                        final_url=doc.source_url,
                        state="needs_ocr",
                    )
                    session.add(version)
                    versions.append(version)
                await session.flush()
                observed = SourceFetchObservation(
                    document_id=doc.id,
                    version_id=versions[1].id,
                    previous_version_id=versions[0].id,
                    observed_at=datetime.now(UTC),
                    comparison_basis="previous_fetch",
                )
                session.add(observed)
                await session.flush()
                change = await create_change(
                    session,
                    versions[0],
                    versions[1],
                    origin_key="migration-fixture",
                    detected_at=observed.observed_at,
                    observation_id=observed.id,
                )
                for table, identifier in (
                    ("source_fetch_observations", observed.id),
                    ("version_changes", change.id),
                ):
                    for statement in (
                        f"UPDATE {table} SET document_id=document_id WHERE id=:id",
                        f"DELETE FROM {table} WHERE id=:id",
                    ):
                        with pytest.raises(DBAPIError, match="append-only"):
                            async with conn.begin_nested():
                                await conn.execute(text(statement), {"id": identifier})
    finally:
        await mig_engine.dispose()


async def test_db_constraint_blocks_invalid_role_org(session_factory):
    """即使绕过应用层,数据库也拒绝"用户端角色没有企业"的数据。"""
    from app.iam.models import User

    async with session_factory() as s:
        s.add(
            User(
                email="bad@example.com",
                display_name="bad",
                password_hash="x",
                role="member",
                org_id=None,
            )
        )
        with pytest.raises(IntegrityError):
            await s.commit()
