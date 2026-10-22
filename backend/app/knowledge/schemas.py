from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class GlossaryTermCreate(BaseModel):
    concept_code: str = Field(min_length=1, max_length=64)
    country_code: str | None = Field(default=None, min_length=2, max_length=2)
    language: str = Field(default="en", min_length=2, max_length=8)
    source_term: str = Field(min_length=1, max_length=255)
    target_zh: str = Field(min_length=1, max_length=255)
    priority_level: int = Field(default=1, ge=1, le=4)
    statutory_definition: str | None = None
    source_reference: str | None = Field(default=None, max_length=255)
    evidence_clause_id: uuid.UUID | None = None


class GlossaryTermReviewIn(BaseModel):
    decision: str = Field(pattern="^(approved|rejected)$")
    target_zh: str | None = Field(default=None, max_length=255)
    statutory_definition: str | None = None


class GlossaryTermOut(BaseModel):
    id: uuid.UUID
    concept_code: str
    country_code: str | None
    language: str
    source_term: str
    target_zh: str
    priority_level: int
    statutory_definition: str | None
    source_reference: str | None
    evidence_clause_id: uuid.UUID | None
    review_status: str
    reviewed_at: datetime | None
    model_config = {"from_attributes": True}


class ClauseTranslationOut(BaseModel):
    id: uuid.UUID
    clause_id: uuid.UUID
    document_id: uuid.UUID
    country_code: str
    document_title: str
    clause_number: str | None
    heading: str | None
    section_path: str | None
    original_text: str
    translated_text: str
    engine: str
    model_name: str
    prompt_version: str
    glossary_hits: list[dict]
    unmatched_terms: list[dict]
    quality_check_passed: bool
    quality_issues: list[str]
    review_status: str
    usable_as_legal_basis: bool
    status_badge: str
    reviewer_note: str | None
    reviewed_at: datetime | None


class TranslationReviewIn(BaseModel):
    decision: str = Field(pattern="^(approved|rejected)$")
    translated_text: str | None = None
    reviewer_note: str | None = None


class ReviewPointCreate(BaseModel):
    country_code: str = Field(min_length=2, max_length=2)
    point_code: str = Field(min_length=2, max_length=64)
    title: str = Field(min_length=2, max_length=255)
    question: str = Field(min_length=5)
    impact_on_conclusion: str = Field(min_length=2)
    provisional_interpretation: str = Field(min_length=2)
    clause_id: uuid.UUID | None = None
    term_id: uuid.UUID | None = None


class ReviewPointDecideIn(BaseModel):
    status: str = Field(pattern="^(confirmed|needs_discussion)$")
    lawyer_reply: str = Field(min_length=2)


class ReviewPointImportItem(BaseModel):
    point_code: str
    status: str = Field(default="confirmed", pattern="^(open|confirmed|needs_discussion)$")
    lawyer_reply: str | None = None


class ReviewPointImportIn(BaseModel):
    items: list[ReviewPointImportItem]


class ReviewPointOut(BaseModel):
    id: uuid.UUID
    country_code: str
    point_code: str
    title: str
    question: str
    impact_on_conclusion: str
    provisional_interpretation: str
    clause_id: uuid.UUID | None
    term_id: uuid.UUID | None
    status: str
    lawyer_reply: str | None
    decided_at: datetime | None
    model_config = {"from_attributes": True}


class KgNodeOut(BaseModel):
    id: uuid.UUID
    country_code: str
    node_type: str
    node_key: str
    name_zh: str
    name_orig: str | None
    summary: str | None
    attributes: dict
    document_id: uuid.UUID | None
    evidence_clause_id: uuid.UUID | None
    review_status: str
    model_config = {"from_attributes": True}


class KgEdgeOut(BaseModel):
    id: uuid.UUID
    country_code: str
    source_node_id: uuid.UUID
    source_name_zh: str
    source_node_type: str
    target_node_id: uuid.UUID
    target_name_zh: str
    target_node_type: str
    relation_type: str
    evidence_clause_id: uuid.UUID
    evidence_quote: str
    clause_number: str | None
    document_title: str | None
    extractor_model: str
    review_status: str
    usable_as_legal_basis: bool


class EdgeReviewIn(BaseModel):
    decision: str = Field(pattern="^(approved|rejected)$")


class GraphPayloadOut(BaseModel):
    country_code: str
    node_counts_by_type: dict[str, int]
    edge_counts_by_relation: dict[str, int]
    nodes: list[KgNodeOut]
    edges: list[KgEdgeOut]
