import os
import subprocess
import sys
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.db import Base
from app.iam import models as _models  # noqa: F401

pytestmark = pytest.mark.asyncio(loop_scope="session")

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
