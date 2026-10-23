"""Append-only evidence/review snapshots and machine change proposals.

Revision ID: 0013
Revises: 0012
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade():
    for name in ("knowledge_audit_records", "knowledge_change_proposals"):
        extra = (
            [
                sa.Column("action", sa.String(64), nullable=False),
                sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
            ]
            if name == "knowledge_audit_records"
            else [
                sa.Column("status", sa.String(24), nullable=False, server_default="pending_review"),
                sa.UniqueConstraint(
                    "target_type", "target_id", "content_hash", name="uq_knowledge_proposal"
                ),
            ]
        )
        op.create_table(
            name,
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("target_type", sa.String(64), nullable=False),
            sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("payload", postgresql.JSONB(), nullable=False),
            sa.Column("content_hash", sa.String(64), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            *extra,
        )
        for col in ("target_type", "target_id"):
            op.create_index(f"ix_{name}_{col}", name, [col])
    op.execute("""
        CREATE FUNCTION protect_knowledge_audit() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'knowledge audit records are append-only'; END $$;
    """)
    op.execute("""
        CREATE TRIGGER knowledge_audit_immutable BEFORE UPDATE OR DELETE
        ON knowledge_audit_records FOR EACH ROW EXECUTE FUNCTION protect_knowledge_audit();
    """)


def downgrade():
    op.execute("DROP TRIGGER knowledge_audit_immutable ON knowledge_audit_records")
    op.execute("DROP FUNCTION protect_knowledge_audit()")
    op.drop_table("knowledge_change_proposals")
    op.drop_table("knowledge_audit_records")
