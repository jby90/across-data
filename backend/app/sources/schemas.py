import re
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.ingestion.fetcher import validate_public_http_url
from app.sources.models import Strategy

DocumentType = Literal["LAW", "REGULATION", "GUIDANCE", "STANDARD", "FORM", "OTHER"]
_LANG = re.compile(r"^[a-z]{2,3}(-[A-Za-z]{2,4})?$")


class SiteConfig(BaseModel):
    """采集规则。只允许这些键,避免把任意内容塞进配置。"""

    model_config = ConfigDict(extra="forbid")

    keywords: list[str] = Field(default_factory=list, max_length=50)
    extensions: list[str] = Field(default_factory=lambda: [".pdf"], max_length=10)
    link_selector: str = Field(default="a[href]", max_length=200)
    next_selector: str | None = Field(default=None, max_length=200)
    max_pages: int = Field(default=1, ge=1, le=20)
    max_candidates: int = Field(default=200, ge=1, le=500)

    @field_validator("extensions")
    @classmethod
    def _ext(cls, v: list[str]) -> list[str]:
        out = []
        for e in v:
            e = e.lower().strip()
            if not re.fullmatch(r"\.[a-z0-9]{1,8}", e):
                raise ValueError(f"扩展名不合法: {e}")
            out.append(e)
        return out


def _check_url(v: str) -> str:
    try:
        return validate_public_http_url(v)
    except ValueError as e:
        raise ValueError(str(e)) from None


class SiteCreate(BaseModel):
    country_code: str = Field(pattern=r"^[A-Z]{2}$")
    publisher: str = Field(min_length=1, max_length=255)
    language: str = Field(max_length=8)
    document_type: DocumentType
    entry_url: str = Field(max_length=1024)
    strategy: Strategy = Strategy.INDEX_PAGE
    config: SiteConfig = Field(default_factory=SiteConfig)
    interval_hours: int = Field(default=168, ge=1, le=24 * 90)
    terms_url: str | None = Field(default=None, max_length=1024)

    @field_validator("language")
    @classmethod
    def _lang(cls, v: str) -> str:
        if not _LANG.match(v):
            raise ValueError("语言代码不合法,例如 en / ar / fr / zh")
        return v

    @field_validator("entry_url")
    @classmethod
    def _entry(cls, v: str) -> str:
        return _check_url(v)

    @field_validator("terms_url")
    @classmethod
    def _terms(cls, v: str | None) -> str | None:
        return _check_url(v) if v else v


class SitePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    publisher: str | None = Field(default=None, min_length=1, max_length=255)
    config: SiteConfig | None = None
    interval_hours: int | None = Field(default=None, ge=1, le=24 * 90)
    enabled: bool | None = None
    terms_url: str | None = Field(default=None, max_length=1024)

    @field_validator("terms_url")
    @classmethod
    def _terms(cls, v: str | None) -> str | None:
        return _check_url(v) if v else v


class TermsDecision(BaseModel):
    status: Literal["approved", "rejected"]
    note: str = Field(min_length=1, max_length=2000)  # 必须写明依据,留痕


class SiteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    country_code: str
    publisher: str
    language: str
    document_type: str
    entry_url: str
    strategy: str
    config: dict[str, Any]
    enabled: bool
    interval_hours: int
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_status: str | None
    consecutive_failures: int
    terms_status: str
    terms_url: str | None
    terms_note: str | None
    terms_decided_by: uuid.UUID | None
    terms_decided_at: datetime | None


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    trigger: str
    started_at: datetime
    finished_at: datetime | None
    status: str
    pages_read: int
    links_found: int
    documents_new: int
    versions_new: int
    errors: int
    message: str | None
