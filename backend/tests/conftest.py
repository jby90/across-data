import atexit
import os
import shutil
import tempfile

# 必须在导入 app 之前设置测试配置(归档目录强制指向临时目录,防止写入 backend/data)
_TEST_ARCHIVE_DIR = tempfile.mkdtemp(prefix="cyshuhe-archive-")
atexit.register(shutil.rmtree, _TEST_ARCHIVE_DIR, True)
os.environ.setdefault("CYSH_JWT_SECRET", "test-secret-only-for-pytest-" + "x" * 16)
os.environ.setdefault("CYSH_ENV", "dev")
os.environ["CYSH_ARCHIVE_DIR"] = _TEST_ARCHIVE_DIR

import pgserver  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core.db import Base, get_session  # noqa: E402
from app.iam import models as _models  # noqa: E402, F401
from app.iam import service  # noqa: E402
from app.iam.models import Role  # noqa: E402
from app.ingestion import models as _ingestion_models  # noqa: E402, F401
from app.main import create_app  # noqa: E402
from app.sources import models as _sources_models  # noqa: E402, F401

PASSWORD = "correct-horse-battery"


@pytest.fixture(scope="session")
def pg_server():
    """嵌入式 PostgreSQL,仅用于测试。"""
    pgdata = tempfile.mkdtemp(prefix="cyshuhe-pg-")
    server = pgserver.get_server(pgdata, cleanup_mode="delete")
    yield server
    server.cleanup()


@pytest.fixture(scope="session")
def pg_url(pg_server) -> str:
    uri = pg_server.get_uri()  # postgresql://postgres:@127.0.0.1:port/postgres
    return uri.replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest_asyncio.fixture(scope="session")
async def engine(pg_url):
    eng = create_async_engine(pg_url)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine):
    async with engine.begin() as conn:
        tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture(autouse=True)
async def isolated_model_audit(session_factory):
    """Real model-transport tests audit only to the isolated test database."""
    from app.core.redaction import llm_scope

    with llm_scope(session_factory=session_factory):
        yield


@pytest_asyncio.fixture
async def client(session_factory):
    app = create_app()

    async def _override():
        async with session_factory() as s:
            yield s

    app.dependency_overrides[get_session] = _override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def seed(session_factory):
    """两家企业加平台管理员与复核员。"""
    async with session_factory() as s:
        org_a = await service.create_org(s, "甲车企")
        org_b = await service.create_org(s, "乙车企")
        users = {}
        for key, email, role, org in [
            ("admin", "admin@example.com", Role.PLATFORM_ADMIN, None),
            ("reviewer", "reviewer@example.com", Role.REVIEWER, None),
            ("a_admin", "a-admin@example.com", Role.ORG_ADMIN, org_a.id),
            ("a_member", "a-member@example.com", Role.MEMBER, org_a.id),
            ("b_admin", "b-admin@example.com", Role.ORG_ADMIN, org_b.id),
        ]:
            users[key] = await service.create_user(
                s, email=email, display_name=key, password=PASSWORD, role=role, org_id=org
            )
        return {"org_a": org_a, "org_b": org_b, **users}


async def login(client, side: str, email: str, password: str = PASSWORD) -> str:
    r = await client.post(f"/api/auth/{side}/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
