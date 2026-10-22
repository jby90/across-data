import httpx
from sqlalchemy import select

from app.ingestion.archive import ArchiveStore
from app.ingestion.pipeline import ingest_site
from app.sources.models import SourceSite

from .conftest import bearer, login
from .helpers import make_fetcher
from .test_pipeline import Site

SITE = {
    "country_code": "SG",
    "publisher": "新加坡个人数据保护委员会",
    "language": "en",
    "document_type": "GUIDANCE",
    "entry_url": "https://example.org/index",
    "config": {"extensions": [".pdf", ".html"]},
}


async def admin_headers(client):
    return bearer(await login(client, "admin", "admin@example.com"))


async def test_create_site_defaults_are_safe(client, seed):
    h = await admin_headers(client)
    r = await client.post("/api/admin/sites", headers=h, json=SITE)
    assert r.status_code == 201
    body = r.json()
    assert (
        body["enabled"] is False
        and body["terms_status"] == "pending"
        and body["next_run_at"] is None
    )


async def test_duplicate_entry_conflicts(client, seed):
    h = await admin_headers(client)
    assert (await client.post("/api/admin/sites", headers=h, json=SITE)).status_code == 201
    assert (await client.post("/api/admin/sites", headers=h, json=SITE)).status_code == 409


async def test_rejects_bad_input(client, seed):
    h = await admin_headers(client)
    for patch in [
        {"entry_url": "http://localhost/x"},
        {"entry_url": "http://10.0.0.1/x"},
        {"entry_url": "ftp://example.org/x"},
        {"country_code": "sg"},
        {"language": "english-language"},
        {"document_type": "NOVEL"},
        {"interval_hours": 0},
        {"config": {"unknown_key": 1}},
        {"config": {"extensions": ["pdf"]}},
        {"config": {"max_pages": 999}},
        {"terms_url": "file:///etc/passwd"},
    ]:
        r = await client.post("/api/admin/sites", headers=h, json={**SITE, **patch})
        assert r.status_code == 422, (patch, r.text)


async def test_cannot_enable_before_terms_approved(client, seed):
    h = await admin_headers(client)
    sid = (await client.post("/api/admin/sites", headers=h, json=SITE)).json()["id"]
    r = await client.patch(f"/api/admin/sites/{sid}", headers=h, json={"enabled": True})
    assert r.status_code == 409
    assert (await client.post(f"/api/admin/sites/{sid}/run", headers=h)).status_code == 409


async def test_terms_decision_requires_note_and_records_approver(client, seed):
    h = await admin_headers(client)
    sid = (await client.post("/api/admin/sites", headers=h, json=SITE)).json()["id"]
    assert (
        await client.post(
            f"/api/admin/sites/{sid}/terms", headers=h, json={"status": "approved", "note": ""}
        )
    ).status_code == 422
    r = await client.post(
        f"/api/admin/sites/{sid}/terms",
        headers=h,
        json={"status": "approved", "note": "法务确认,见邮件 2026-10-22"},
    )
    assert r.status_code == 200
    assert r.json()["terms_status"] == "approved" and r.json()["terms_decided_by"] == str(
        seed["admin"].id
    )
    assert r.json()["terms_decided_at"] is not None


async def test_enable_schedules_immediately_and_run_now_flags(client, seed, session_factory):
    h = await admin_headers(client)
    sid = (await client.post("/api/admin/sites", headers=h, json=SITE)).json()["id"]
    await client.post(
        f"/api/admin/sites/{sid}/terms", headers=h, json={"status": "approved", "note": "ok"}
    )
    r = await client.patch(f"/api/admin/sites/{sid}", headers=h, json={"enabled": True})
    assert (
        r.status_code == 200 and r.json()["enabled"] is True and r.json()["next_run_at"] is not None
    )
    assert (await client.post(f"/api/admin/sites/{sid}/run", headers=h)).status_code == 202
    async with session_factory() as s:
        site = (await s.execute(select(SourceSite))).scalar_one()
        assert site.run_requested is True


async def test_rejecting_terms_disables_a_running_site(client, seed):
    h = await admin_headers(client)
    sid = (await client.post("/api/admin/sites", headers=h, json=SITE)).json()["id"]
    await client.post(
        f"/api/admin/sites/{sid}/terms", headers=h, json={"status": "approved", "note": "ok"}
    )
    await client.patch(f"/api/admin/sites/{sid}", headers=h, json={"enabled": True})
    r = await client.post(
        f"/api/admin/sites/{sid}/terms",
        headers=h,
        json={"status": "rejected", "note": "条款变更,禁止商用"},
    )
    assert r.json()["enabled"] is False and r.json()["terms_status"] == "rejected"


async def test_entry_url_cannot_be_changed(client, seed):
    # 条款确认绑定入口地址,改地址必须新建站点重新确认
    h = await admin_headers(client)
    sid = (await client.post("/api/admin/sites", headers=h, json=SITE)).json()["id"]
    r = await client.patch(
        f"/api/admin/sites/{sid}", headers=h, json={"entry_url": "https://evil.example.org/"}
    )
    assert r.status_code == 422


async def test_patch_config_and_interval(client, seed):
    h = await admin_headers(client)
    sid = (await client.post("/api/admin/sites", headers=h, json=SITE)).json()["id"]
    r = await client.patch(
        f"/api/admin/sites/{sid}",
        headers=h,
        json={"interval_hours": 24, "config": {"keywords": ["data"]}},
    )
    assert (
        r.status_code == 200
        and r.json()["interval_hours"] == 24
        and r.json()["config"]["keywords"] == ["data"]
    )


async def test_site_endpoints_forbidden_for_non_platform_admin(client, seed):
    user_h = bearer(await login(client, "user", "a-admin@example.com"))
    reviewer_h = bearer(await login(client, "admin", "reviewer@example.com"))
    assert (
        await client.get("/api/admin/sites", headers=user_h)
    ).status_code == 401  # 用户端令牌不能用于管理端
    assert (await client.get("/api/admin/sites", headers=reviewer_h)).status_code == 403
    assert (await client.post("/api/admin/sites", json=SITE)).status_code == 401


async def test_unknown_site_404(client, seed):
    h = await admin_headers(client)
    assert (
        await client.get("/api/admin/sites/00000000-0000-0000-0000-000000000000", headers=h)
    ).status_code == 404


async def test_documents_api_after_real_ingest(client, seed, session_factory, tmp_path):
    h = await admin_headers(client)
    sid = (await client.post("/api/admin/sites", headers=h, json=SITE)).json()["id"]
    await client.post(
        f"/api/admin/sites/{sid}/terms", headers=h, json={"status": "approved", "note": "ok"}
    )
    state = Site()
    import uuid as _uuid

    await ingest_site(
        session_factory,
        make_fetcher(state.handler),
        ArchiveStore(tmp_path / "a"),
        _uuid.UUID(sid),
        trigger="manual",
    )

    runs = (await client.get(f"/api/admin/sites/{sid}/runs", headers=h)).json()
    assert len(runs) == 1 and runs[0]["versions_new"] == 2

    reviewer_h = bearer(await login(client, "admin", "reviewer@example.com"))  # 复核员可读文档
    docs = (await client.get("/api/admin/documents?country_code=sg", headers=reviewer_h)).json()
    assert len(docs) == 2 and all(d["latest_version"]["state"] == "parsed" for d in docs)
    pdf = next(d for d in docs if d["source_url"].endswith("act.pdf"))

    detail = (await client.get(f"/api/admin/documents/{pdf['id']}", headers=reviewer_h)).json()
    assert [v["version_no"] for v in detail["versions"]] == [1]
    vid = detail["versions"][0]["id"]
    clauses = (await client.get(f"/api/admin/versions/{vid}/clauses", headers=reviewer_h)).json()
    assert [c["clause_number"] for c in clauses] == ["1", "2"]
    page = (
        await client.get(f"/api/admin/versions/{vid}/clauses?limit=1&offset=1", headers=reviewer_h)
    ).json()
    assert [c["clause_number"] for c in page] == ["2"]
    events = (await client.get(f"/api/admin/versions/{vid}/events", headers=reviewer_h)).json()
    assert [e["step"] for e in events] == ["download", "parse"]

    assert (
        await client.get(
            "/api/admin/documents",
            headers=bearer(await login(client, "user", "a-admin@example.com")),
        )
    ).status_code == 401
    assert (
        await client.get(
            "/api/admin/documents/00000000-0000-0000-0000-000000000000", headers=reviewer_h
        )
    ).status_code == 404
    assert (
        await client.get(
            "/api/admin/versions/00000000-0000-0000-0000-000000000000/clauses", headers=reviewer_h
        )
    ).status_code == 404
    assert httpx is not None
