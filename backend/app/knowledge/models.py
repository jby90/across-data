from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.rls import TenantOwnedMixin


class NodeType(enum.StrEnum):
    """知识图谱六要素节点类型。"""

    COUNTRY = "COUNTRY"  # 国家/地区
    REGULATOR = "REGULATOR"  # 监管机构
    INSTRUMENT = "INSTRUMENT"  # 法规文件
    DATA_CATEGORY = "DATA_CATEGORY"  # 数据类型
    OBLIGATION = "OBLIGATION"  # 合规义务
    PENALTY = "PENALTY"  # 违规罚则


class RelationType(enum.StrEnum):
    """知识图谱十二类语义关系(结构包含关系文件->条款不混入此表)。"""

    PUBLISHES = "PUBLISHES"  # 发布
    AMENDS = "AMENDS"  # 修订
    REPEALS = "REPEALS"  # 废止
    GOVERNS = "GOVERNS"  # 约束
    PROHIBITS = "PROHIBITS"  # 禁止
    REQUIRES = "REQUIRES"  # 强制要求
    EXEMPTS = "EXEMPTS"  # 豁免
    SUPERVISES = "SUPERVISES"  # 监管
    PENALIZES = "PENALIZES"  # 处罚
    APPLIES_TO_SCENARIO = "APPLIES_TO_SCENARIO"  # 适用场景
    RESTRICTS_CROSS_BORDER = "RESTRICTS_CROSS_BORDER"  # 数据出境限制
    REQUIRES_ADMISSION = "REQUIRES_ADMISSION"  # 准入要求


class TranslationReviewStatus(enum.StrEnum):
    MACHINE_TRANSLATED = "machine_translated"
    TRANSLATION_FAILED = "translation_failed"
    NOT_TRANSLATED = "not_translated"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    STALE = "stale"
    SUPERSEDED = "superseded"


class GraphReviewStatus(enum.StrEnum):
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    STALE = "stale"
    SUPERSEDED = "superseded"


class ReviewPointStatus(enum.StrEnum):
    OPEN = "open"
    CONFIRMED = "confirmed"
    NEEDS_DISCUSSION = "needs_discussion"


class KnowledgeAuditRecord(TenantOwnedMixin, Base):
    """Immutable snapshots survive target replacement/deletion; contain no credentials."""

    __tablename__ = "knowledge_audit_records"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    target_type: Mapped[str] = mapped_column(String(64), index=True)
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    action: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class KnowledgeChangeProposal(Base):
    """Changed machine output for a protected approved/rejected record; never auto-applied."""

    __tablename__ = "knowledge_change_proposals"
    __table_args__ = (
        UniqueConstraint("target_type", "target_id", "content_hash", name="uq_knowledge_proposal"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    target_type: Mapped[str] = mapped_column(String(64), index=True)
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    payload: Mapped[dict] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(
        String(24), default="pending_review", server_default="pending_review"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GlossaryTerm(Base):
    """两层法律术语库(通用概念 + 国家覆盖),带四级来源优先级与依据条款。"""

    __tablename__ = "glossary_terms"
    __table_args__ = (
        CheckConstraint("priority_level IN (1, 2, 3, 4)", name="ck_glossary_priority"),
        CheckConstraint(
            "review_status IN ('pending','approved','rejected')",
            name="ck_glossary_review_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    concept_code: Mapped[str] = mapped_column(String(64), index=True)
    # None 表示跨国家复用的通用核心术语(如 GDPR 基准概念);两字母国家码表示特定国家的法定覆盖
    country_code: Mapped[str | None] = mapped_column(String(2), nullable=True, index=True)
    language: Mapped[str] = mapped_column(String(8))
    source_term: Mapped[str] = mapped_column(String(255))
    target_zh: Mapped[str] = mapped_column(String(255))
    # 1=法律自身定义条款(最高) 2=监管指南与问答 3=官方多语种版本 4=国际通行概念
    priority_level: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    statutory_definition: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    evidence_clause_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clauses.id", ondelete="SET NULL"), nullable=True, index=True
    )
    review_status: Mapped[str] = mapped_column(
        String(16), default="pending", server_default="pending"
    )
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LegalTermDefinition(Base):
    """Append-only, source-bound definitions; machine checks never confer human approval."""

    __tablename__ = "legal_term_definitions"
    __table_args__ = (UniqueConstraint("fingerprint", name="uq_legal_term_fingerprint"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    concept_code: Mapped[str] = mapped_column(String(128), index=True)
    country_code: Mapped[str | None] = mapped_column(String(2), nullable=True, index=True)
    language: Mapped[str] = mapped_column(String(8))
    source_term: Mapped[str] = mapped_column(String(255))
    target_zh: Mapped[str] = mapped_column(String(255))
    definition_original: Mapped[str] = mapped_column(Text)
    definition_zh: Mapped[str | None] = mapped_column(Text, nullable=True)
    applies_to_document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="RESTRICT"), nullable=True
    )
    source_kind: Mapped[str] = mapped_column(String(32))
    source_url: Mapped[str] = mapped_column(String(2048))
    source_reference: Mapped[str] = mapped_column(String(500))
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document_versions.id", ondelete="RESTRICT"), nullable=True
    )
    clause_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clauses.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    quote_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quote_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_text_sha256: Mapped[str] = mapped_column(String(64))
    fingerprint: Mapped[str] = mapped_column(String(64))
    verification_status: Mapped[str] = mapped_column(String(32), default="source_matched")
    registration_origin: Mapped[str] = mapped_column(
        String(24), default="auto", server_default="auto"
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    # Separate from GlossaryTerm.review_status: never auto-approve legacy/manual records.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ClauseTranslation(Base):
    """条款级受术语约束机器翻译与译后程序检查结果。"""

    __tablename__ = "clause_translations"
    __table_args__ = (
        CheckConstraint(
            "review_status IN ("
            "'machine_translated','translation_failed','not_translated',"
            "'pending_review','approved','rejected','stale','superseded'"
            ")",
            name="ck_translations_review_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    clause_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clauses.id", ondelete="CASCADE"), index=True
    )
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    translated_text: Mapped[str] = mapped_column(Text)
    engine: Mapped[str] = mapped_column(String(32))
    model_name: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(32), default="v1", server_default="v1")
    tokens_in: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    tokens_out: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    glossary_hits: Mapped[list] = mapped_column(JSONB, default=list)
    unmatched_terms: Mapped[list] = mapped_column(JSONB, default=list)
    quality_check_passed: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    quality_issues: Mapped[list] = mapped_column(JSONB, default=list)
    review_status: Mapped[str] = mapped_column(
        String(24),
        default=TranslationReviewStatus.MACHINE_TRANSLATED.value,
        server_default="machine_translated",
    )
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reviewer_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    @property
    def status(self) -> str:
        return self.review_status


class ReviewPoint(Base):
    """解读要点确认单条目:法律未明确定义、官方无直接解释、但直接影响车企合规结论的少数关键问题。"""

    __tablename__ = "review_points"
    __table_args__ = (
        UniqueConstraint("point_code", name="uq_review_points_code"),
        CheckConstraint(
            "status IN ('open','confirmed','needs_discussion')",
            name="ck_review_points_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    country_code: Mapped[str] = mapped_column(String(2), index=True)
    point_code: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(255))
    question: Mapped[str] = mapped_column(Text)
    impact_on_conclusion: Mapped[str] = mapped_column(Text)
    provisional_interpretation: Mapped[str] = mapped_column(Text)
    clause_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clauses.id", ondelete="SET NULL"), nullable=True, index=True
    )
    term_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("glossary_terms.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(24), default=ReviewPointStatus.OPEN.value, server_default="open"
    )
    lawyer_reply: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class KgNode(Base):
    """六要素知识图谱节点(国家/地区、监管机构、法规文件、数据类型、合规义务、违规罚则)。"""

    __tablename__ = "kg_nodes"
    __table_args__ = (
        UniqueConstraint("country_code", "node_key", name="uq_kg_nodes_country_key"),
        CheckConstraint(
            "node_type IN ("
            "'COUNTRY','REGULATOR','INSTRUMENT','DATA_CATEGORY','OBLIGATION','PENALTY'"
            ")",
            name="ck_kg_nodes_type",
        ),
        CheckConstraint(
            "review_status IN ('pending_review','approved','rejected','stale','superseded')",
            name="ck_kg_nodes_review_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    country_code: Mapped[str] = mapped_column(String(2), index=True)
    node_type: Mapped[str] = mapped_column(String(24), index=True)
    node_key: Mapped[str] = mapped_column(String(128))
    name_zh: Mapped[str] = mapped_column(String(255))
    name_orig: Mapped[str | None] = mapped_column(String(255), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    attributes: Mapped[dict] = mapped_column(JSONB, default=dict)
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    evidence_clause_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clauses.id", ondelete="SET NULL"), nullable=True, index=True
    )
    review_status: Mapped[str] = mapped_column(
        String(24), default=GraphReviewStatus.PENDING_REVIEW.value, server_default="pending_review"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class KgEdge(Base):
    """十二类语义关系边。每条语义边必须挂证据条款(evidence_clause_id 非空)。"""

    __tablename__ = "kg_edges"
    __table_args__ = (
        CheckConstraint(
            "relation_type IN ("
            "'PUBLISHES','AMENDS','REPEALS','GOVERNS','PROHIBITS','REQUIRES',"
            "'EXEMPTS','SUPERVISES','PENALIZES','APPLIES_TO_SCENARIO',"
            "'RESTRICTS_CROSS_BORDER','REQUIRES_ADMISSION'"
            ")",
            name="ck_kg_edges_relation",
        ),
        CheckConstraint(
            "review_status IN ('pending_review','approved','rejected','stale','superseded')",
            name="ck_kg_edges_review_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    country_code: Mapped[str] = mapped_column(String(2), index=True)
    source_node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("kg_nodes.id", ondelete="CASCADE"), index=True
    )
    target_node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("kg_nodes.id", ondelete="CASCADE"), index=True
    )
    relation_type: Mapped[str] = mapped_column(String(32), index=True)
    evidence_clause_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clauses.id", ondelete="CASCADE"), index=True
    )
    evidence_quote: Mapped[str] = mapped_column(Text)
    extractor_model: Mapped[str] = mapped_column(String(128))
    confidence: Mapped[float] = mapped_column(Float, default=0.85, server_default="0.85")
    judge_verdict: Mapped[str] = mapped_column(String(16), default="yes", server_default="yes")
    judge_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_structural: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    review_status: Mapped[str] = mapped_column(
        String(24), default=GraphReviewStatus.PENDING_REVIEW.value, server_default="pending_review"
    )
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
