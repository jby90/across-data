import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
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


class TermsStatus(enum.StrEnum):
    PENDING = "pending"  # 待确认:不得自动抓取
    APPROVED = "approved"  # 已确认可抓取(法务/负责人确认)
    REJECTED = "rejected"  # 已否决


class Strategy(enum.StrEnum):
    DIRECT = "DIRECT"  # entry_url 本身就是一份原文
    INDEX_PAGE = "INDEX_PAGE"  # entry_url 是目录页,从中发现原文链接


class RunStatus(enum.StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"  # 部分文档失败
    FAILED = "failed"
    SKIPPED = "skipped"  # 条款未确认等原因未执行


class SourceSite(Base):
    """一个官方来源站点的抓取配置。新增国家/站点只加配置,不改代码。"""

    __tablename__ = "source_sites"
    __table_args__ = (
        UniqueConstraint("country_code", "entry_url", name="uq_source_sites_country_entry"),
        # 数据库层兜底:条款未确认的站点不可能处于启用状态
        CheckConstraint(
            "NOT enabled OR terms_status = 'approved'", name="ck_sites_enabled_needs_terms"
        ),
        CheckConstraint(
            "terms_status IN ('pending','approved','rejected')", name="ck_sites_terms_status"
        ),
        CheckConstraint("strategy IN ('DIRECT','INDEX_PAGE')", name="ck_sites_strategy"),
        CheckConstraint("interval_hours >= 1", name="ck_sites_interval"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    country_code: Mapped[str] = mapped_column(String(2))
    publisher: Mapped[str] = mapped_column(String(255))
    language: Mapped[str] = mapped_column(String(8))
    document_type: Mapped[str] = mapped_column(String(32))
    entry_url: Mapped[str] = mapped_column(String(1024))
    strategy: Mapped[str] = mapped_column(String(32), default=Strategy.INDEX_PAGE.value)
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )

    enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    interval_hours: Mapped[int] = mapped_column(Integer, default=168, server_default=text("168"))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    run_requested: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))

    terms_status: Mapped[str] = mapped_column(
        String(16), default=TermsStatus.PENDING.value, server_default=text("'pending'")
    )
    terms_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    terms_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    terms_decided_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    terms_decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CrawlRun(Base):
    __tablename__ = "crawl_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running','success','partial','failed','skipped')", name="ck_runs_status"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    site_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("source_sites.id", ondelete="CASCADE"), index=True
    )
    trigger: Mapped[str] = mapped_column(String(16))  # schedule / manual
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=RunStatus.RUNNING.value)
    pages_read: Mapped[int] = mapped_column(Integer, default=0)
    links_found: Mapped[int] = mapped_column(Integer, default=0)
    documents_new: Mapped[int] = mapped_column(Integer, default=0)
    versions_new: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
