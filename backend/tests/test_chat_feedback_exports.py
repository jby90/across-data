"""Real isolated PostgreSQL HTTP checks; no business data/model calls."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.analysis import router
from app.analysis.events import append_event
from app.analysis.export_cache import ArtifactCache
from app.analysis.models import AnalysisStepTrace

from .conftest import bearer, login
from .test_s7_exports_events import fixture_report


def test_cache_bounded_ttl_singleflight_and_failure_retry():
    now = [0]
    cache = ArtifactCache(max_bytes=6, max_entries=2, ttl=10, clock=lambda: now[0])
    entered, release, waiting = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def build():
        calls.append(1)
        entered.set()
        assert release.wait(2)
        return b"PDF"

    def follower():
        waiting.set()
        return cache.get_or_build("a", build)

    with ThreadPoolExecutor(2) as pool:
        one = pool.submit(cache.get_or_build, "a", build)
        assert entered.wait(2)
        two = pool.submit(follower)
        assert waiting.wait(2)
        release.set()
        assert one.result() == (b"PDF", False)
        assert two.result() == (b"PDF", True)
    assert len(calls) == 1
    cache.get_or_build("b", lambda: b"1234")
    assert cache._bytes <= 6 and "a" not in cache._items
    now[0] = 11
    assert cache.get_or_build("b", lambda: b"new") == (b"new", False)
    cache.get_or_build("oversize", lambda: b"0123456789")
    assert "oversize" not in cache._items
    for key in range(5):
        cache.get_or_build(key, lambda: b"x")
    assert len(cache._items) <= 2 and cache._bytes <= 6
    for _ in range(2):
        with pytest.raises(ValueError):
            cache.get_or_build("failure", lambda: (_ for _ in ()).throw(ValueError("render")))
    assert "failure" not in cache._pending


async def test_pdf_cache_frozen_content_scope_and_guards(client, seed, session_factory, monkeypatch):
    from app.analysis import export_cache
    from app.analysis.models import ReportVersion

    monkeypatch.setattr(export_cache, "PDF_CACHE", ArtifactCache())
    calls = []

    def pdf(task, rv, dimension):
        calls.append((task.id, rv.executive_summary, dimension))
        return b"%PDF synthetic"

    monkeypatch.setattr(router, "build_report_pdf", pdf)
    task, rv = await fixture_report(session_factory, seed)
    auth = bearer(await login(client, "user", "a-member@example.com"))
    url = f"/api/analysis/tasks/{task.id}/reports/1/export.pdf"
    first = await client.get(url, headers=auth)
    second = await client.get(url, headers=auth)
    assert first.status_code == second.status_code == 200
    assert first.headers["x-export-cache"] == "miss"
    assert second.headers["x-export-cache"] == "hit" and len(calls) == 1
    assert "export;dur=" in second.headers["server-timing"]
    assert second.headers["cache-control"] == "private, no-store"
    assert (await client.get(url + "?dimension=D4", headers=auth)).status_code == 200
    assert len(calls) == 2
    async def changed_updates(*_):
        return [{"change_id": "new-source-update", "document_title": "Synthetic update",
                 "dimension_code": "D1", "state": "open"}]

    monkeypatch.setattr(router, "report_updates", changed_updates)
    assert (await client.get(url, headers=auth)).headers["x-export-cache"] == "miss"
    assert len(calls) == 3
    # Even an in-place snapshot change invalidates cache; no stale evidence reused.
    async with session_factory() as s:
        current = await s.get(ReportVersion, rv.id)
        current.executive_summary = "CHANGED_SYNTHETIC_SNAPSHOT"
        await s.commit()
    assert (await client.get(url, headers=auth)).headers["x-export-cache"] == "miss"
    assert len(calls) == 4
    foreign = bearer(await login(client, "user", "b-admin@example.com"))
    assert (await client.get(url, headers=foreign)).status_code == 404

    async def held(*_):
        return ["a newly held source"]

    monkeypatch.setattr(router, "report_held_citation_ids", held)
    assert (await client.get(url, headers=auth)).status_code == 409
    assert len(calls) == 4  # Hold is enforced BEFORE a warm cache read.


async def test_actual_activity_uses_committed_events_not_internal_node_labels(
    client, seed, session_factory
):
    from app.analysis.models import AnalysisTask

    task, _ = await fixture_report(session_factory, seed, state="running")
    async with session_factory() as s:
        current = await s.get(AnalysisTask, task.id)
        current.findings_json = []
        s.add(AnalysisStepTrace(
            task_id=task.id, seq_no=1, agent_node_id="PRIVATE_NODE", agent_name="PRIVATE_AGENT",
            phase="verification", status="failed", business_summary="PRIVATE_REASON",
        ))
        await append_event(s, current, "phase_started", {"phase": "retrieval", "node_id": "PRIVATE_NODE"})
        await append_event(s, current, "phase_started", {"phase": "analysis", "system_prompt": "PRIVATE_PROMPT"})
        await s.commit()
    auth = bearer(await login(client, "user", "a-member@example.com"))
    url = f"/api/analysis/tasks/{task.id}"
    result = await client.get(url, headers=auth)
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["activity"]["label"] == "分析合规要求"
    assert data["activity"]["started_at"] and data["activity"]["state"] == "running"
    assert data["progress"] == [{"label": "核对报告依据", "state": "failed"}]
    assert "PRIVATE_" not in result.text
    async with session_factory() as s:
        current = await s.get(AnalysisTask, task.id)
        current.status = "awaiting_user_facts"
        await s.commit()
    paused = (await client.get(url, headers=auth)).json()
    assert paused["activity"] is None and paused["progress"][-1]["state"] == "waiting"
