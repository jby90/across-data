"""Validate append-only identity observations against immutable official archives.

A matching instrument key permits comparison only. It never proves that
editions are equivalent, in force or approved by a human.
"""

from functools import lru_cache

from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field

from app.ingestion.parsing import detect_kind, parse_document


@lru_cache(maxsize=16)
def observation_text(blob, mime):
    if detect_kind(blob, mime) == "html":
        # Identity metadata may be outside the legal-body container. Preserve
        # its source location independently of the text admitted as clauses.
        soup = BeautifulSoup(blob, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        return soup.get_text("\n")
    return "\n".join(parse_document(blob, mime).pages)


class SourceObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version_id: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    country_code: str = Field(pattern=r"^[A-Z]{2}$")
    instrument_key: str = Field(min_length=3, max_length=160)
    instrument_role: str = Field(pattern=r"^(base|amendment|guidance)$")
    source_url: str
    publisher: str
    source_nature: str = Field(pattern=r"^(official_publication|statutory_body_republication)$")
    source_excerpt: str = Field(min_length=20)
    review_status: str = Field(default="pending_review", pattern=r"^pending_review$")
    human_reviewed: bool = False
    usable_as_legal_basis: bool = False


def validate_observation(payload, version, document, source_text):
    item = SourceObservation.model_validate(payload)
    if (
        item.version_id != str(version.id)
        or version.document_id != document.id
        or item.content_hash != version.content_hash
        or item.country_code != document.country_code
        or item.source_url != document.source_url
        or item.human_reviewed
        or item.usable_as_legal_basis
    ):
        raise ValueError("identity observation cannot change source or approval")
    if " ".join(item.source_excerpt.split()) not in " ".join(source_text.split()):
        raise ValueError("identity excerpt absent from the archived source")
    return item.country_code + ":" + item.instrument_key, item.instrument_role
