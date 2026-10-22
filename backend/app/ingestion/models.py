import enum
import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class VersionState(enum.StrEnum):
    DOWNLOADED = "downloaded"  # 已下载并归档
    PARSED = "parsed"  # 已解析、已条款化
    NEEDS_OCR = "needs_ocr"  # 没有文字层(扫描件),待 OCR
    FAILED = "failed"  # 解析失败(原文已归档,可重跑)


class Document(Base):
    """一份官方文件(以来源 URL 为身份)。内容变化时产生新的版本,旧版本保留。"""

    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("site_id", "source_url", name="uq_documents_site_url"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    site_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("source_sites.id", ondelete="CASCADE"), index=True
    )
    country_code: Mapped[str] = mapped_column(String(2), index=True)
    source_url: Mapped[str] = mapped_column(String(2048))
    title: Mapped[str] = mapped_column(String(500))
    language: Mapped[str] = mapped_column(String(8))
    document_type: Mapped[str] = mapped_column(String(32))
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 文件所属的法规体系(如 sg-av-rules)与文件性质(ACT/REGULATION/AMENDMENT/GUIDANCE...)
    instrument_family: Mapped[str | None] = mapped_column(String(128), nullable=True)
    instrument_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)


class LegalStatus(enum.StrEnum):
    UNKNOWN = "unknown"  # 现行状态未经核实(默认,不得当作现行有效)
    IN_FORCE = "in_force"
    AMENDED = "amended"
    REPEALED = "repealed"


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version_no", name="uq_versions_doc_no"),
        UniqueConstraint("document_id", "content_hash", name="uq_versions_doc_hash"),
        CheckConstraint(
            "state IN ('downloaded','parsed','needs_ocr','failed')", name="ck_versions_state"
        ),
        CheckConstraint(
            "legal_status IN ('unknown','in_force','amended','repealed')",
            name="ck_versions_legal_status",
        ),
        CheckConstraint("text_source IN ('text_layer','ocr')", name="ck_versions_text_source"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    version_no: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))  # SHA-256(原文字节)
    archive_path: Mapped[str] = mapped_column(String(512))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    mime_type: Mapped[str] = mapped_column(String(128))
    final_url: Mapped[str] = mapped_column(String(2048))
    http_etag: Mapped[str | None] = mapped_column(String(255), nullable=True)
    http_last_modified: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    state: Mapped[str] = mapped_column(String(16), default=VersionState.DOWNLOADED.value)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    char_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parse_strategy: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 文本来源:PDF 文字层,或 OCR(OCR 文本必须人工核对,准确率不能当作原文)
    text_source: Mapped[str] = mapped_column(
        String(16), default="text_layer", server_default="text_layer"
    )
    ocr_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    # 版本元数据:来自官方文本或一期核对,metadata_source 说明出处;未知就留空,不猜
    official_number: Mapped[str | None] = mapped_column(String(128), nullable=True)
    version_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    published_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    repealed_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    legal_status: Mapped[str] = mapped_column(
        String(16), default=LegalStatus.UNKNOWN.value, server_default="unknown"
    )
    metadata_source: Mapped[str | None] = mapped_column(String(32), nullable=True)


class PipelineEvent(Base):
    """流水线每一步的结果,用于排错和重跑。"""

    __tablename__ = "pipeline_events"
    __table_args__ = (CheckConstraint("status IN ('ok','error')", name="ck_events_status"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document_versions.id", ondelete="CASCADE"), index=True
    )
    step: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(8))
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Clause(Base):
    """从原文切出的条款。original_text 与原文逐字一致,不做改写。"""

    __tablename__ = "clauses"
    __table_args__ = (
        UniqueConstraint("version_id", "ordinal", name="uq_clauses_version_ordinal"),
        Index("ix_clauses_version", "version_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document_versions.id", ondelete="CASCADE")
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    clause_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    heading: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # 所在的 PART/CHAPTER/附表,如 "PART 6A NOTIFICATION OF DATA BREACHES";与条号一起唯一定位一条
    section_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    original_text: Mapped[str] = mapped_column(Text)
    page_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    text_hash: Mapped[str] = mapped_column(String(64))
    language: Mapped[str] = mapped_column(String(8))
