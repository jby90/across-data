"""Synthetic metadata only; no real laws or business database writes."""

from sqlalchemy import event, select

from app.ingestion.models import Document
from app.knowledge.countries import country_catalog
from app.knowledge.models import KnowledgeChangeProposal
from app.sources.models import SourceSite

from .conftest import bearer, login


def test_unknown_country_labels_are_configuration_not_a_legal_allowlist():
    configured = country_catalog(
        ["BR", "JP"],
        [
            ("BR", {"country_name_zh": "测试国别名", "country_name_orig": "Synthetic fixture"}),
            ("JP", {"country_name_zh": "冲突名称甲"}),
            ("JP", {"country_name_zh": "冲突名称乙"}),
        ],
    )
    by_code = {item["country_code"]: item for item in configured}
    assert by_code["BR"]["country_name_zh"] == "测试国别名"
    assert by_code["JP"]["country_name_zh"] == "JP"
    assert len(configured) == 7  # Original pilot labels retained, not an exclusive allowlist.


async def test_new_country_visible_without_core_changes_and_hold_not_exposed(
    client, seed, session_factory
):
    async with session_factory() as session:
        for code in ("BR", "CA"):
            site = SourceSite(
                country_code=code,
                publisher="Synthetic fixture",
                language="pt",
                document_type="LAW",
                entry_url=f"https://{code.lower()}.example.test/",
                config={"country_name_zh": f"测试国别 {code}"},
                strategy="DIRECT",
                enabled=False,
                terms_status="pending",
            )
            session.add(site)
            await session.flush()
            doc = Document(
                site_id=site.id,
                country_code=code,
                title="合成测试文件，并非真实法规",
                document_type="LAW",
                language="pt",
                source_url=site.entry_url,
            )
            session.add(doc)
            await session.flush()
            if code == "CA":
                session.add(
                    KnowledgeChangeProposal(
                        target_type="documents",
                        target_id=doc.id,
                        content_hash="a" * 64,
                        payload={"safety_hold": True},
                        status="pending_review",
                    )
                )
        await session.commit()

    admin = bearer(await login(client, "admin", "admin@example.com"))
    member = bearer(await login(client, "user", "a-member@example.com"))
    result = await client.get("/api/admin/knowledge/countries", headers=admin)
    assert result.status_code == 200
    assert {"BR", "CA"} <= {c["country_code"] for c in result.json()}
    overview = await client.get("/api/knowledge/overview", headers=member)
    assert overview.status_code == 200, overview.text
    by_code = {c["country_code"]: c for c in overview.json()}
    assert by_code["BR"]["documents_count"] == 1
    assert by_code["BR"]["country_name_zh"] == "测试国别 BR"
    assert by_code["BR"]["approved_translations_count"] == 0
    assert "CA" not in by_code, "source-integrity hold remains enforced"
    # Country discovery does not approve a site, translate, extract or accept any report.
    async with session_factory() as session:
        sites = (await session.scalars(select(SourceSite))).all()
        assert all(not s.enabled and s.terms_status == "pending" for s in sites)
    assert (await client.get("/api/admin/knowledge/countries", headers=member)).status_code == 401
    revisions = await client.get(
        "/api/admin/knowledge/parsed-graph-revisions?country_code=br", headers=admin
    )
    assert revisions.status_code == 200 and revisions.json() == []


async def test_thirty_additional_countries_do_not_multiply_overview_queries(
    client, seed, session_factory, engine
):
    member = bearer(await login(client, "user", "a-member@example.com"))
    count = 0

    def count_query(*_):
        nonlocal count
        count += 1

    event.listen(engine.sync_engine, "before_cursor_execute", count_query)
    try:
        baseline = await client.get("/api/knowledge/overview", headers=member)
        assert baseline.status_code == 200
        empty_queries = count
        codes = "US CA BR MX AR CL PE CO GB FR DE ES IT PT NL BE SE NO FI DK PL CZ AT CH JP KR IN ID TH VN".split()
        async with session_factory() as session:
            for code in codes:
                site = SourceSite(
                    country_code=code,
                    publisher="Synthetic fixture",
                    language="en",
                    document_type="LAW",
                    entry_url=f"https://{code.lower()}.example.test/",
                    strategy="DIRECT",
                    enabled=False,
                    terms_status="pending",
                )
                session.add(site)
                await session.flush()
                session.add(
                    Document(
                        site_id=site.id,
                        country_code=code,
                        title=f"合成测试文件 {code}",
                        document_type="LAW",
                        language="en",
                        source_url=site.entry_url,
                    )
                )
            await session.commit()
        count = 0
        result = await client.get("/api/knowledge/overview", headers=member)
        assert result.status_code == 200, result.text
        assert len(result.json()) == 35
        assert sum(c["documents_count"] for c in result.json()) == 30
        assert all(c["approved_translations_count"] == 0 for c in result.json())
        assert count == empty_queries, "query count must not grow per country"
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", count_query)
