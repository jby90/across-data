from datetime import UTC, datetime, timedelta

from app.core.config import get_settings
from app.ingestion.archive import ArchiveStore
from app.ingestion.models import PipelineEvent
from app.sources.models import SourceSite
from app.sources.permissions import terms_permit_crawl
from app.sources.scheduler import claim_due_site

from .conftest import bearer, login
from .review_s1_s2_contracts import make_document


async def test_development_approval_does_not_authorize_production(session_factory, monkeypatch):
    settings = get_settings()
    async with session_factory() as session:
        site = SourceSite(
            country_code="EG",
            publisher="Test",
            language="ar",
            document_type="LAW",
            entry_url="https://example.org/test.pdf",
            strategy="DIRECT",
            terms_status="approved",
            enabled=True,
            terms_approval_scope="development",
            next_run_at=datetime.now(UTC) - timedelta(hours=1),
        )
        session.add(site)
        await session.commit()
        assert terms_permit_crawl(site)
        monkeypatch.setattr(settings, "env", "production")
        assert not terms_permit_crawl(site)
        assert await claim_due_site(session_factory) is None
        site.terms_approval_scope = "production"
        await session.commit()
        assert terms_permit_crawl(site)
        assert await claim_due_site(session_factory) is not None


async def test_provenance_does_not_treat_manual_upload_as_crawl_or_approval(
    client, seed, session_factory, tmp_path, monkeypatch
):
    monkeypatch.setattr(get_settings(), "archive_dir", str(tmp_path))
    async with session_factory() as session:
        doc, version, _ = await make_document(session)
        archive = ArchiveStore(tmp_path)
        raw = b"original file"
        version.content_hash, version.archive_path = archive.put(raw)
        version.size_bytes = len(raw)
        session.add(
            PipelineEvent(version_id=version.id, step="upload", status="ok", message="test")
        )
        await session.commit()
        doc_id = doc.id
    headers = bearer(await login(client, "admin", "admin@example.com"))
    result = await client.get(f"/api/admin/documents/{doc_id}/provenance", headers=headers)
    assert result.status_code == 200, result.text
    version = result.json()["versions"][0]
    assert version["integrity_ok"]
    assert version["acquisition"] == "manual_upload"
    assert version["official_authenticity"] == "pending_human_review"
    (tmp_path / version_path(archive, raw)).write_bytes(b"damaged")
    result = await client.get(f"/api/admin/documents/{doc_id}/provenance", headers=headers)
    assert not result.json()["versions"][0]["integrity_ok"]


def version_path(archive, raw):
    digest = archive.sha256(raw)
    return f"{digest[:2]}/{digest}"
