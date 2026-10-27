"""Trusted, transaction-local database identity; requests cannot choose a tenant.

RLS is a second boundary for accidentally unfiltered SQL, not a substitute for
JWT validation, endpoint permissions, SQL parameterization or secret isolation.
"""

import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from sqlalchemy import ForeignKey, event, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, Session, mapped_column


class TenantOwnedMixin:
    org_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=True, index=True
    )


@dataclass(frozen=True)
class DatabaseIdentity:
    scope: str = "anonymous"
    org_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    auth_email: str = ""


_ANONYMOUS = DatabaseIdentity()
_identity = ContextVar("database_identity", default=_ANONYMOUS)
PUBLIC_AUDIT_TYPES = (
    "documents",
    "document_versions",
    "clauses",
    "clause_translations",
    "kg_nodes",
    "kg_edges",
    "kg_country",
    "glossary_terms",
    "source_sites",
)
SET_IDENTITY = text("""SELECT set_config('app.scope', :scope, true),
    set_config('app.org_id', :org_id, true),
    set_config('app.user_id', :user_id, true),
    set_config('app.auth_email', :auth_email, true)""")


def identity_values(identity):
    return {
        "scope": identity.scope,
        "org_id": str(identity.org_id) if identity.org_id else "",
        "user_id": str(identity.user_id) if identity.user_id else "",
        "auth_email": identity.auth_email,
    }


def current_identity():
    return _identity.get()


@contextmanager
def database_scope(identity=None):
    token = _identity.set(identity or DatabaseIdentity())
    try:
        yield
    finally:
        _identity.reset(token)


@contextmanager
def system_scope(reason):
    """Only explicit trusted worker/CLI entrypoints may use this scope."""
    if not reason.strip():
        raise ValueError("system database access requires an operational reason")
    with database_scope(DatabaseIdentity(scope="system")):
        yield


def principal_identity(principal):
    role = str(principal.role)
    scope = (
        "platform" if role == "platform_admin" else "reviewer" if role == "reviewer" else "tenant"
    )
    return DatabaseIdentity(scope, principal.org_id, principal.user_id)


async def bind_identity(session, identity):
    _identity.set(identity)
    session.info["database_identity"] = identity
    if session.in_transaction():
        connection = await session.connection()
        await connection.execute(SET_IDENTITY, identity_values(identity))


async def bind_principal(session, principal):
    await bind_identity(session, principal_identity(principal))


def install_rls_hooks():
    if event.contains(Session, "after_begin", _after_begin):
        return
    event.listen(Session, "after_begin", _after_begin)
    event.listen(Session, "before_flush", _before_flush)


def _after_begin(session, transaction, connection):
    if connection.dialect.name == "postgresql":
        identity = session.info.get("database_identity", current_identity())
        connection.execute(SET_IDENTITY, identity_values(identity))


def _before_flush(session, flush_context, instances):
    from app.analysis.models import AnalysisTask
    from app.knowledge.models import KnowledgeAuditRecord

    for row in session.new | session.dirty:
        if isinstance(row, TenantOwnedMixin) and hasattr(row, "task_id"):
            parent = session.get(AnalysisTask, row.task_id)
            if parent is not None:
                if row.org_id is not None and row.org_id != parent.org_id:
                    raise ValueError("child record organization must match its analysis task")
                row.org_id = parent.org_id
        if isinstance(row, KnowledgeAuditRecord) and row in session.new:
            if row.target_type == "organization_redaction":
                row.org_id = row.target_id
            elif row.target_type not in PUBLIC_AUDIT_TYPES and row.org_id is None:
                row.org_id = session.info.get("database_identity", current_identity()).org_id


async def validate_runtime_role(engine):
    """Fail startup with privileged runtime credentials; migrations use a separate identity."""
    async with engine.connect() as connection:
        role = (
            (
                await connection.execute(
                    text("""SELECT rolname,rolsuper,rolbypassrls,
                rolcreaterole,rolcreatedb,rolreplication
                FROM pg_roles WHERE rolname=current_user""")
                )
            )
            .mappings()
            .one()
        )
        owned = await connection.scalar(
            text("""SELECT count(*) FROM pg_tables
            WHERE schemaname='public' AND tableowner=current_user""")
        )
        inherited_privilege = await connection.scalar(
            text("""SELECT EXISTS(
            SELECT FROM pg_roles r WHERE pg_has_role(current_user,r.oid,'MEMBER') AND
            (r.rolsuper OR r.rolbypassrls OR r.rolcreatedb OR r.rolcreaterole OR r.rolreplication OR
             EXISTS(SELECT FROM pg_tables t WHERE t.schemaname='public' AND t.tableowner=r.rolname))
        )""")
        )
        if (
            any(
                role[key]
                for key in (
                    "rolsuper",
                    "rolbypassrls",
                    "rolcreaterole",
                    "rolcreatedb",
                    "rolreplication",
                )
            )
            or owned
            or inherited_privilege
        ):
            raise RuntimeError(
                "API/worker must use a non-owner ordinary role without BYPASSRLS; "
                "use separate migration credentials"
            )
        from app.core.db import Base

        expected = {t.name for t in Base.metadata.tables.values() if "org_id" in t.c}
        expected.add("organizations")
        flags = (
            await connection.execute(
                text("""SELECT c.relname,c.relrowsecurity,
            c.relforcerowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='public' AND c.relname = ANY(:names)"""),
                {"names": sorted(expected)},
            )
        ).all()
        if {
            row.relname for row in flags if row.relrowsecurity and row.relforcerowsecurity
        } != expected:
            raise RuntimeError(
                "tenant RLS migration is missing or not forced; API/worker startup refused"
            )
