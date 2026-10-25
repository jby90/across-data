"""Real LOGIN role, migrated database, no owner/superuser/SET ROLE shortcut."""

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.analysis.models import AnalysisMessage, AnalysisStepTrace, AnalysisTask, ReportVersion
from app.core.db import Base, get_session
from app.core.rls import (
    DatabaseIdentity,
    bind_identity,
    database_scope,
    system_scope,
    validate_runtime_role,
)
from app.iam import service
from app.iam.models import Role
from app.knowledge.models import KnowledgeAuditRecord
from app.main import create_app

from .conftest import PASSWORD, bearer, login


@pytest_asyncio.fixture(scope="module")
async def rls_database(pg_url, engine):
    suffix = uuid.uuid4().hex[:12]
    dbname, role = "rls_" + suffix, "runtime_" + suffix
    async with engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(text(f'CREATE DATABASE "{dbname}"'))
    url = make_url(pg_url).set(database=dbname)
    migrated = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parents[1],
        env=dict(os.environ, CYSH_DATABASE_URL=url.render_as_string(hide_password=False)),
        capture_output=True,
        text=True,
    )
    assert migrated.returncode == 0, migrated.stderr
    root = create_async_engine(url)
    async with root.begin() as conn:
        await conn.execute(
            text(
                f'CREATE ROLE "{role}" LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION'
            )
        )
        await conn.execute(text(f'GRANT cyshuhe_app TO "{role}"'))
    # Loopback-only pgserver uses trust authentication. No production credential is involved.
    ordinary = create_async_engine(
        url.set(username=role, password=None), pool_size=1, max_overflow=1
    )
    try:
        yield root, ordinary, role
    finally:
        await ordinary.dispose()
        await root.dispose()
        async with engine.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            await conn.execute(text(f'DROP DATABASE "{dbname}"'))
            await conn.execute(text(f'DROP ROLE "{role}"'))


@pytest_asyncio.fixture
async def rls_data(rls_database):
    root, ordinary, role = rls_database
    root_sf = async_sessionmaker(root, expire_on_commit=False)
    sf = async_sessionmaker(ordinary, expire_on_commit=False)
    with system_scope("isolated RLS fixture seed"):
        async with root.begin() as conn:
            tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
            await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        async with root_sf() as s:
            org_a = await service.create_org(s, "RLS Alpha")
            org_b = await service.create_org(s, "RLS Beta")
            users = {}
            for key, email, user_role, org in [
                ("admin", "admin@example.com", Role.PLATFORM_ADMIN, None),
                ("reviewer", "reviewer@example.com", Role.REVIEWER, None),
                ("a", "a-admin@example.com", Role.ORG_ADMIN, org_a.id),
                ("a_member", "a-member@example.com", Role.MEMBER, org_a.id),
                ("b", "b-admin@example.com", Role.ORG_ADMIN, org_b.id),
            ]:
                users[key] = await service.create_user(
                    s, email=email, display_name=key, password=PASSWORD, role=user_role, org_id=org
                )
            tasks = []
            for user, org in [(users["a"], org_a), (users["b"], org_b)]:
                task = AnalysisTask(
                    org_id=org.id,
                    created_by=user.id,
                    title=org.name,
                    country_code="XX",
                    vehicle_model="Synthetic",
                    scenario_type="Synthetic",
                    status="completed",
                )
                s.add(task)
                await s.flush()
                tasks.append(task)
                s.add(
                    AnalysisMessage(
                        task_id=task.id,
                        seq_no=1,
                        role="user",
                        content="Private business facts " + org.name,
                    )
                )
                s.add(
                    AnalysisStepTrace(
                        task_id=task.id,
                        seq_no=1,
                        agent_node_id="test",
                        agent_name="Synthetic",
                        phase="test",
                        business_summary="Private trace " + org.name,
                    )
                )
                s.add(
                    ReportVersion(
                        task_id=task.id,
                        version_no=1,
                        change_note="Synthetic",
                        executive_summary="Private report " + org.name,
                        dimensions_json=[],
                    )
                )
                s.add(
                    KnowledgeAuditRecord(
                        target_type="organization_redaction",
                        target_id=org.id,
                        action="redaction_terms_changed",
                        payload={"private_alias": org.name},
                        content_hash="synthetic",
                    )
                )
            await s.commit()
    app = create_app()

    async def scoped_session():
        async with sf() as s:
            yield s

    app.dependency_overrides[get_session] = scoped_session
    with database_scope():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://rls-test"
        ) as client:
            yield {
                "root": root,
                "engine": ordinary,
                "root_sf": root_sf,
                "sf": sf,
                "users": users,
                "org_a": org_a,
                "org_b": org_b,
                "a_task": tasks[0],
                "b_task": tasks[1],
                "client": client,
                "role": role,
            }


async def test_actual_role_default_deny_and_forced_rls(rls_data):
    d = rls_data
    await validate_runtime_role(d["engine"])
    with pytest.raises(RuntimeError, match="non-owner ordinary"):
        await validate_runtime_role(d["root"])
    with database_scope():
        async with d["engine"].connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT current_user,rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"
                    )
                )
            ).one()
            assert row.current_user == d["role"] and row.rolsuper is row.rolbypassrls is False
            # PostgreSQL RLS does not cover TRUNCATE; the runtime role must lack it.
            with pytest.raises(DBAPIError, match="permission denied"):
                async with conn.begin_nested():
                    await conn.execute(text("TRUNCATE analysis_tasks CASCADE"))
            for table in (
                "analysis_tasks",
                "analysis_messages",
                "analysis_step_traces",
                "report_versions",
                "users",
                "organizations",
                "llm_call_audit",
                "update_impacts",
                "user_notifications",
                "analysis_events",
            ):
                assert await conn.scalar(text(f"SELECT count(*) FROM {table}")) == 0, table
            assert (
                await conn.scalar(
                    text(
                        "SELECT count(*) FROM knowledge_audit_records WHERE target_type='organization_redaction'"
                    )
                )
                == 0
            )
            policies = (
                await conn.execute(
                    text(
                        "SELECT relname,relrowsecurity,relforcerowsecurity FROM pg_class WHERE relname IN ('analysis_tasks','report_versions','analysis_messages','analysis_step_traces','users','organizations')"
                    )
                )
            ).all()
            assert len(policies) == 6 and all(
                r.relrowsecurity and r.relforcerowsecurity for r in policies
            )


async def test_unfiltered_queries_and_writes_remain_in_organization(rls_data):
    d = rls_data
    identity = DatabaseIdentity("tenant", d["org_a"].id, d["users"]["a"].id)
    with database_scope(identity):
        async with d["sf"]() as s:
            assert (await s.scalars(select(AnalysisTask))).one().id == d["a_task"].id
            assert await s.get(AnalysisTask, d["b_task"].id) is None
            for model in (AnalysisMessage, AnalysisStepTrace, ReportVersion):
                rows = (await s.scalars(select(model))).all()
                assert len(rows) == 1 and rows[0].org_id == d["org_a"].id
            audits = (await s.scalars(select(KnowledgeAuditRecord))).all()
            assert len(audits) == 1 and audits[0].org_id == d["org_a"].id
            with pytest.raises(DBAPIError):
                async with s.begin_nested():
                    await s.execute(
                        text(
                            "INSERT INTO analysis_messages(id,task_id,seq_no,role,content,options_json) VALUES (:id,:task,2,'user','forged','[]')"
                        ),
                        {"id": uuid.uuid4(), "task": d["b_task"].id},
                    )
            assert (
                await s.execute(
                    text("UPDATE analysis_tasks SET title='FORGED' WHERE id=:id"),
                    {"id": d["b_task"].id},
                )
            ).rowcount == 0
            with pytest.raises(DBAPIError, match="row-level security"):
                async with s.begin_nested():
                    await s.execute(
                        text(
                            "INSERT INTO analysis_tasks(id,org_id,title,country_code,vehicle_model,scenario_type,status,facts_json,missing_facts_json,findings_json,total_tokens,total_duration_ms,mcp_calls_count,backtrack_count,workflow_version_no,sufficiency_score) VALUES (:id,:org,'forged','XX','x','x','completed','{}','[]','[]',0,0,0,0,1,0)"
                        ),
                        {"id": uuid.uuid4(), "org": d["org_b"].id},
                    )
            await s.commit()
            # after_begin must re-apply the same tenant after every commit.
            assert (await s.scalars(select(AnalysisTask))).one().id == d["a_task"].id
    # Reusing the pool connection must clear both organization and privileged flags.
    with database_scope():
        async with d["sf"]() as s:
            assert (await s.scalars(select(AnalysisTask))).all() == []


async def test_authentication_is_bounded_and_http_cannot_choose_tenant(rls_data):
    d = rls_data
    c = d["client"]
    a = bearer(await login(c, "user", "a-admin@example.com"))
    b = bearer(await login(c, "user", "b-admin@example.com"))
    admin = bearer(await login(c, "admin", "admin@example.com"))
    reviewer = bearer(await login(c, "admin", "reviewer@example.com"))
    for headers, own, foreign in [(a, d["a_task"], d["b_task"]), (b, d["b_task"], d["a_task"])]:
        response = await c.get("/api/analysis/tasks", headers=headers)
        assert response.status_code == 200, response.text
        assert [t["id"] for t in response.json()] == [str(own.id)]
        response = await c.get(
            f"/api/analysis/tasks/{foreign.id}",
            headers=dict(headers, **{"X-Org-Id": str(foreign.org_id)}),
        )
        assert response.status_code == 404, response.text
        response = await c.get(f"/api/analysis/tasks/{own.id}", headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["report_versions"][0]["executive_summary"].endswith(own.title)
    for headers in (admin, reviewer):
        response = await c.get("/api/admin/agent-studio/tasks", headers=headers)
        assert response.status_code == 200, response.text
        assert len(response.json()) == 2
    with database_scope():
        async with d["sf"]() as s:
            await bind_identity(s, DatabaseIdentity(scope="auth", auth_email="a-admin@example.com"))
            assert await s.scalar(text("SELECT count(*) FROM users")) == 1
            assert await s.scalar(text("SELECT count(*) FROM organizations")) == 1
            assert (await s.execute(text("UPDATE users SET display_name='forged'"))).rowcount == 0


async def test_public_legal_provenance_is_readable_private_audit_is_not(rls_data):
    d = rls_data
    with system_scope("synthetic public audit fixture"):
        async with d["root_sf"]() as s:
            s.add(
                KnowledgeAuditRecord(
                    target_type="clauses",
                    target_id=uuid.uuid4(),
                    action="public_source",
                    payload={"source": "https://example.org/fixture"},
                    content_hash="synthetic",
                )
            )
            s.add(
                KnowledgeAuditRecord(
                    target_type="unknown_private_type",
                    target_id=uuid.uuid4(),
                    action="private",
                    payload={"confidential": "must not be public"},
                    content_hash="synthetic",
                )
            )
            await s.commit()
    with database_scope():
        async with d["sf"]() as s:
            rows = (await s.scalars(select(KnowledgeAuditRecord))).all()
            assert len(rows) == 1 and rows[0].target_type == "clauses"
    with database_scope(DatabaseIdentity("platform", user_id=d["users"]["admin"].id)):
        async with d["sf"]() as s:
            assert len((await s.scalars(select(KnowledgeAuditRecord))).all()) == 4


async def test_user_fact_pause_resume_and_sse_with_real_runtime_role(rls_data, monkeypatch):
    from app.analysis.events import close_hubs, stream_events
    from app.iam.deps import Principal

    from .test_s6_user_facts import answer, start, wait_state
    from .test_s7_exports_events import ConnectedRequest

    d = rls_data
    headers, calls, paused = await start(d["client"], d, d["root_sf"], monkeypatch)
    assert paused["status"] == "awaiting_user_facts"
    before = len(calls)
    response = await answer(d["client"], headers, paused, "中国上海研发中心")
    assert response.status_code == 200, response.text
    done = await wait_state(d["client"], headers, paused["id"], {"completed", "partial_sample"})
    assert done["status"] == "completed", done
    assert {c["code"] for c in calls[before:]} == {"D3"}
    task_id = uuid.UUID(paused["id"])
    async with d["root_sf"]() as s:
        for model in (AnalysisMessage, AnalysisStepTrace, ReportVersion):
            rows = (await s.scalars(select(model).where(model.task_id == task_id))).all()
            assert rows and {r.org_id for r in rows} == {d["org_a"].id}
    p = Principal(d["users"]["a_member"].id, Role.MEMBER, d["org_a"].id)
    with database_scope():
        stream = stream_events(ConnectedRequest(), d["engine"], p, task_id=task_id)
        try:
            assert "connected" in await anext(stream)
            events = []
            async for message in stream:
                events.append(message)
            assert any("phase_finished" in event for event in events)
            assert '"status": "completed"' in events[-1]
        finally:
            await stream.aclose()
            await close_hubs()


async def test_system_update_scan_and_own_notification_with_real_runtime_role(rls_data, tmp_path):
    from app.updates.models import UpdateImpact, UserNotification
    from app.updates.service import monitor_once

    from .test_s10_updates import change_source, dimensions, setup_source

    d = rls_data
    state, site_id, doc, v1, clause = await setup_source(d["root_sf"], tmp_path)
    async with d["root_sf"]() as s:
        rv = await s.scalar(select(ReportVersion).where(ReportVersion.task_id == d["a_task"].id))
        rv.dimensions_json = dimensions(doc, v1, clause)
        await s.commit()
    await change_source(d["root_sf"], tmp_path, state, site_id)
    with system_scope("RLS synthetic scheduler update scan"):
        result = await monitor_once(d["sf"])
    assert result["impacts"] == 8 and result["notifications"] == 2
    a = bearer(await login(d["client"], "user", "a-admin@example.com"))
    b = bearer(await login(d["client"], "user", "b-admin@example.com"))
    response = await d["client"].get("/api/notifications", headers=a)
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1
    assert (await d["client"].get("/api/notifications", headers=b)).json()["total"] == 0
    with database_scope(DatabaseIdentity("tenant", d["org_b"].id, d["users"]["b"].id)):
        async with d["sf"]() as s:
            assert (await s.scalars(select(UpdateImpact))).all() == []
            assert (await s.scalars(select(UserNotification))).all() == []


async def test_runtime_bootstrap_is_dedicated_and_privileged_membership_is_rejected(rls_data):
    from app.core.runtime_roles import provision_runtime_role

    d = rls_data
    name = "cyshuhe_fixture_" + uuid.uuid4().hex[:12]
    password = "SYNTHETIC_" + "x" * 36
    url = await provision_runtime_role(d["root"], name, password=password)
    runtime = create_async_engine(url)
    try:
        await validate_runtime_role(runtime)
        async with d["root"].connect() as conn:
            verifier = await conn.scalar(
                text("SELECT rolpassword FROM pg_authid WHERE rolname=:name"), {"name": name}
            )
            assert verifier.startswith("SCRAM-SHA-256$4096:") and password not in verifier
        owner = d["root"].url.username
        async with d["root"].begin() as conn:
            await conn.execute(text(f'GRANT "{owner}" TO "{name}"'))
        with pytest.raises(RuntimeError, match="ordinary role"):
            await validate_runtime_role(runtime)
        with pytest.raises(ValueError, match="privileged"):
            await provision_runtime_role(d["root"], name, password=password)
    finally:
        await runtime.dispose()
        async with d["root"].begin() as conn:
            await conn.execute(text(f'DROP ROLE "{name}"'))


async def test_concurrent_requests_do_not_reuse_another_tenant_or_admin_context(rls_data):
    import asyncio

    d = rls_data
    c = d["client"]
    tokens = {
        key: bearer(await login(c, side, email))
        for key, side, email in [
            ("a", "user", "a-admin@example.com"),
            ("b", "user", "b-admin@example.com"),
            ("admin", "admin", "admin@example.com"),
        ]
    }

    async def check(key):
        endpoint = "/api/admin/agent-studio/tasks" if key == "admin" else "/api/analysis/tasks"
        response = await c.get(endpoint, headers=tokens[key])
        assert response.status_code == 200, response.text
        expected = (
            {str(d["a_task"].id), str(d["b_task"].id)}
            if key == "admin"
            else {str(d[key + "_task"].id)}
        )
        assert {row["id"] for row in response.json()} == expected

    await asyncio.gather(*(check(key) for key in ["a", "admin", "b"] * 5))
    with database_scope():
        async with d["sf"]() as s:
            assert (await s.scalars(select(AnalysisTask))).all() == []
