"""S7 uses an isolated PostgreSQL database and synthetic report snapshots only."""

import asyncio
import io
import json
import time
import zipfile

import pytest
from sqlalchemy import func, select

from app.analysis.events import append_event, close_hubs, stream_events
from app.analysis.models import AnalysisEvent, AnalysisTask, ReportVersion
from app.iam.deps import Principal
from app.iam.models import Role, User

from .conftest import bearer, login


async def fixture_report(session_factory, seed, *, other=False, legacy=False, state="completed"):
    async with session_factory() as session:
        task = AnalysisTask(
            org_id=seed["org_b" if other else "org_a"].id,
            created_by=seed["b_admin" if other else "a_member"].id,
            title="S7 synthetic snapshot",
            country_code="XX",
            vehicle_model="synthetic",
            scenario_type="synthetic",
            status=state,
            findings_json=[{"business_fact": "LATEST_TASK_SECRET_MUST_NOT_LEAK"}],
        )
        session.add(task)
        await session.flush()
        dims = [
            {
                "dimension_code": f"D{i}",
                "dimension_title": f"DIMENSION_TITLE_D{i}",
                "content_markdown": f"BODY_MARKER_D{i}",
                "status": "COMPLETE",
                "risk_level": "LOW",
                "findings": []
                if legacy
                else [
                    {
                        "business_fact": f"HISTORICAL_FACT_D{i}",
                        "compliance_obligation": f"ORIGINAL_RULE_D{i}",
                        "basis_quote": f"QUOTE_D{i}",
                    }
                ],
                "citations": [],
                "key_recommendations": [],
            }
            for i in range(1, 9)
        ]
        if legacy:
            for d in dims:
                d.pop("findings")
        rv = ReportVersion(
            task_id=task.id,
            version_no=1,
            status="full_draft",
            is_partial_sample=False,
            change_note="synthetic",
            executive_summary="FULL_SUMMARY_MARKER",
            dimensions_json=dims,
        )
        session.add(rv)
        await session.commit()
        return task, rv


def doc_text(blob):
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        return z.read("word/document.xml").decode(), z.read("word/header1.xml").decode()


@pytest.mark.parametrize("dimension", [f"D{i}" for i in range(1, 9)])
async def test_single_dimension_and_zip_keep_selected_version_only(
    client, seed, session_factory, dimension
):
    task, rv = await fixture_report(session_factory, seed)
    h = bearer(await login(client, "user", "a-member@example.com"))
    url = f"/api/analysis/tasks/{task.id}/reports/1/export."
    r = await client.get(url + f"docx?dimension={dimension}", headers=h)
    assert r.status_code == 200, r.text
    text, header = doc_text(r.content)
    assert "BODY_MARKER_" + dimension in text and "HISTORICAL_FACT_" + dimension in text
    assert "FULL_SUMMARY_MARKER" not in text and "LATEST_TASK_SECRET" not in text
    for other in set(f"D{i}" for i in range(1, 9)) - {dimension}:
        assert "BODY_MARKER_" + other not in text and "HISTORICAL_FACT_" + other not in text
    assert "CONV-" in header and dimension in header and "v1" in header and "法律" in header
    assert dimension in r.headers["content-disposition"]
    if dimension == "D3":
        pdf = await client.get(url + "pdf?dimension=D3", headers=h)
        assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
        bundle = await client.get(url + "zip", headers=h)
        assert bundle.status_code == 200
        with zipfile.ZipFile(io.BytesIO(bundle.content)) as z:
            assert len(z.namelist()) == 9
            for name in z.namelist():
                x, _ = doc_text(z.read(name))
                assert "LATEST_TASK_SECRET" not in x
                if "_D3_" in name:
                    assert "BODY_MARKER_D2" not in x
    async with session_factory() as s:
        current = await s.get(ReportVersion, rv.id)
        assert current.dimensions_json == rv.dimensions_json  # Export cannot rewrite evidence.
    assert (await client.get(url + "docx?dimension=D9", headers=h)).status_code == 422
    assert (await client.get(url + "zip?dimension=D3", headers=h)).status_code == 400
    foreign = bearer(await login(client, "user", "b-admin@example.com"))
    assert (await client.get(url + "zip", headers=foreign)).status_code == 404


async def test_legacy_snapshot_does_not_borrow_latest_findings(client, seed, session_factory):
    task, _ = await fixture_report(session_factory, seed, legacy=True)
    h = bearer(await login(client, "user", "a-member@example.com"))
    r = await client.get(f"/api/analysis/tasks/{task.id}/reports/1/export.docx", headers=h)
    text, _ = doc_text(r.content)
    assert "LATEST_TASK_SECRET" not in text and "未保存判定矩阵快照" in text
    assert "BODY_MARKER_D1" in text and "BODY_MARKER_D8" in text


def parse_events(text):
    return [
        {
            "type": next(line[7:] for line in block.splitlines() if line.startswith("event: ")),
            "data": json.loads(
                next(line[6:] for line in block.splitlines() if line.startswith("data: "))
            ),
            "id": next(
                (int(line[4:]) for line in block.splitlines() if line.startswith("id: ")), None
            ),
        }
        for block in text.split("\n\n")
        if "data: " in block
    ]


async def test_http_stream_order_replay_and_tenant_auth(client, seed, session_factory):
    task, _ = await fixture_report(session_factory, seed)
    foreign, _ = await fixture_report(session_factory, seed, other=True)
    kinds = ["phase_started", "tool_called", "phase_finished", "token_usage", "completed"]
    async with session_factory() as s:
        for kind in kinds:
            await append_event(s, task, kind, {"phase": "synthetic"})
        await append_event(s, foreign, "failed", {"secret": "OTHER_TENANT"})
        await s.commit()
    h = bearer(await login(client, "user", "a-member@example.com"))
    url = f"/api/analysis/tasks/{task.id}/events"
    try:
        async with client.stream("GET", url, headers=h) as r:
            payload = (await r.aread()).decode()
            assert r.headers["content-type"].startswith("text/event-stream")
            assert r.headers["x-accel-buffering"] == "no"
        events = parse_events(payload)
        assert [e["type"] for e in events] == ["connected", *kinds, "snapshot"]
        assert "OTHER_TENANT" not in payload
        after = events[2]["id"]
        again = await client.get(url, headers=h | {"Last-Event-ID": str(after)})
        assert all(e["id"] > after for e in parse_events(again.text) if e["id"] is not None)
        assert (await client.get(url, headers=h | {"Last-Event-ID": "-1"})).status_code == 400
        b = bearer(await login(client, "user", "b-admin@example.com"))
        assert (await client.get(url, headers=b)).status_code == 404
        assert (await client.get(url + "?token=not-header-auth")).status_code == 401
        assert (await client.get("/api/admin/agent-studio/events", headers=h)).status_code == 401
    finally:
        await close_hubs()


class ConnectedRequest:
    async def is_disconnected(self):
        return False


async def test_pg_notify_is_after_commit_and_survives_listener_reconnect(
    seed, session_factory, engine
):
    task, _ = await fixture_report(session_factory, seed, state="running")
    p = Principal(seed["a_member"].id, Role.MEMBER, seed["org_a"].id)
    gen = stream_events(ConnectedRequest(), engine, p, task_id=task.id)
    try:
        assert "connected" in await anext(gen)
        pending = asyncio.create_task(anext(gen))
        await asyncio.sleep(0.05)
        async with session_factory() as s:
            await append_event(s, task, "phase_started", {"phase": "retrieval"})
            await asyncio.sleep(0.05)
            assert not pending.done()  # Uncommitted progress must not reach the browser.
            started = time.monotonic()
            await s.commit()
        event = await asyncio.wait_for(pending, 1)
        assert "phase_started" in event and time.monotonic() - started < 1
        await gen.aclose()
        await close_hubs()  # Process/listener restart: replay still reads durable rows.
        gen = stream_events(ConnectedRequest(), engine, p, task_id=task.id)
        await anext(gen)
        assert "phase_started" in await anext(gen)
    finally:
        await gen.aclose()
        await close_hubs()


async def test_progress_rollback_and_revoked_reader(seed, session_factory, engine):
    task, _ = await fixture_report(session_factory, seed, state="running")
    async with session_factory() as s:
        await append_event(s, task, "phase_started")
        await s.rollback()
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(AnalysisEvent)) == 0
    p = Principal(seed["a_member"].id, Role.MEMBER, seed["org_a"].id)
    gen = stream_events(ConnectedRequest(), engine, p, task_id=task.id)
    try:
        await anext(gen)
        async with session_factory() as s:
            user = await s.get(User, p.user_id)
            user.is_active = False
            await s.commit()
        assert "access_revoked" in await anext(gen)
    finally:
        await gen.aclose()
        await close_hubs()
