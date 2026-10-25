"""Bounded, read-only source research. Downloading never approves legal evidence."""

from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.ingestion.fetcher import BlockedError, FetchError, validate_public_http_url


class ResearchLead(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    url: str
    publisher: str = Field(min_length=1, max_length=300)
    source_kind: str = Field(
        pattern=r"^(national_authority|gazette_publisher|intergovernmental_repository|secondary)$"
    )
    discovery_url: str
    note: str = Field(default="", max_length=1500)

    @field_validator("url", "discovery_url")
    @classmethod
    def public_url(cls, value):
        if urlsplit(value).username is not None or urlsplit(value).password is not None:
            raise ValueError("research URLs must not contain credentials")
        return validate_public_http_url(value)


async def probe_leads(fetcher, archive, leads, *, max_sources=16):
    """No database, scheduled crawling, guessed URL discovery, OCR or model calls."""
    items = [ResearchLead.model_validate(item) for item in leads]
    if len(items) > max_sources:
        raise ValueError("source budget exceeded; do not silently truncate")
    if len({item.url for item in items}) != len(items):
        raise ValueError("duplicate source URLs require an explicit single record")
    rows = []
    for item in items:
        row = item.model_dump() | {
            "state": "unconfirmed",
            "body_downloaded": False,
            "robots_permitted": False,
            "active": False,
            "usable_as_legal_basis": False,
            "review_status": "pending_review",
            "human_review": None,
        }
        try:
            response = await fetcher.get(item.url)
            if response.status != 200 or response.content is None:
                raise FetchError("research requires an actual HTTP 200 source body")
            digest, relative = archive.put(response.content)
            row.update(
                state="archived_candidate",
                body_downloaded=True,
                robots_permitted=True,
                final_url=response.url,
                mime=response.mime,
                bytes=len(response.content),
                sha256=digest,
                archive_path=relative,
            )
        except (BlockedError, FetchError) as exc:
            row.update(
                state="blocked" if isinstance(exc, BlockedError) else "fetch_failed",
                error_type=type(exc).__name__,
                error=str(exc),
            )
        rows.append(row)
    return {
        "records": rows,
        "robots": dict(fetcher._robots_note),
        "model_calls": 0,
        "database_writes": 0,
        "human_approvals": 0,
        "note": "来源性质是研究登记，不是官方真实性确认；归档候选也不能自动入库或作法律依据。",
    }
