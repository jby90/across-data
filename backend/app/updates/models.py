"""Content-change observations and operational reminders, separate from legal approvals."""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class RegulatoryDiscovery(Base):
    __tablename__ = "regulatory_discoveries"
    __table_args__ = (
        UniqueConstraint("site_id", "source_url", name="uq_discovery_site_url"),
        Index("ix_discovery_country_time", "country_code", "discovered_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    site_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("source_sites.id", ondelete="SET NULL"), index=True
    )
    country_code: Mapped[str] = mapped_column(String(8))
    source_url: Mapped[str] = mapped_column(String(2048))
    title: Mapped[str] = mapped_column(String(500))
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id"), index=True, nullable=True
    )


class SourceFetchObservation(Base):
    __tablename__ = "source_fetch_observations"
    __table_args__ = (Index("ix_source_fetch_doc_sequence", "document_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id"), index=True
    )
    version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document_versions.id")
    )
    previous_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document_versions.id"), nullable=True
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    comparison_basis: Mapped[str] = mapped_column(String(32))


class VersionChange(Base):
    __tablename__ = "version_changes"
    __table_args__ = (UniqueConstraint("origin_key", name="uq_version_change_origin"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    origin_key: Mapped[str] = mapped_column(String(100))
    source_observation_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("source_fetch_observations.id"), nullable=True, unique=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("documents.id"))
    old_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document_versions.id"), index=True
    )
    new_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document_versions.id")
    )
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    change_kind: Mapped[str] = mapped_column(String(40))
    summary: Mapped[str] = mapped_column(Text)
    changes_json: Mapped[list] = mapped_column(JSONB)
    graph_edge_ids: Mapped[list] = mapped_column(JSONB)
    content_hashes: Mapped[dict] = mapped_column(JSONB)


class UpdateImpact(Base):
    __tablename__ = "update_impacts"
    __table_args__ = (
        UniqueConstraint(
            "report_version_id", "dimension_code", "change_id", name="uq_report_change_impact"
        ),
        Index("ix_update_impacts_state_due", "state", "due_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), index=True
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("analysis_tasks.id"), index=True
    )
    report_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("report_versions.id")
    )
    dimension_code: Mapped[str] = mapped_column(String(8))
    change_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("version_changes.id"), index=True
    )
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(24), default="open")
    resolved_report_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("report_versions.id")
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserNotification(Base):
    __tablename__ = "user_notifications"
    __table_args__ = (
        UniqueConstraint("task_id", "change_id", "user_id", name="uq_change_recipient"),
        Index("ix_notifications_user_created", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), index=True
    )
    impact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("update_impacts.id"), index=True
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("analysis_tasks.id"), index=True
    )
    change_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("version_changes.id"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
