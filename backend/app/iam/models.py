import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, String, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Role(enum.StrEnum):
    # 管理端
    PLATFORM_ADMIN = "platform_admin"  # 平台管理员
    REVIEWER = "reviewer"  # 复核员(律所,外部)
    # 用户端
    ORG_ADMIN = "org_admin"  # 企业管理员
    MEMBER = "member"  # 企业成员


ADMIN_ROLES = frozenset({Role.PLATFORM_ADMIN, Role.REVIEWER})
USER_ROLES = frozenset({Role.ORG_ADMIN, Role.MEMBER})


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        # 管理端角色不属于任何企业;用户端角色必须属于某个企业
        CheckConstraint(
            "(role IN ('platform_admin','reviewer') AND org_id IS NULL) OR "
            "(role IN ('org_admin','member') AND org_id IS NOT NULL)",
            name="ck_users_role_org",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), index=True, nullable=True
    )
    # 邮箱统一小写存储,全局唯一
    email: Mapped[str] = mapped_column(String(320), unique=True)
    display_name: Mapped[str] = mapped_column(String(100))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(32))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
