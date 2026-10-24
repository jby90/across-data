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
