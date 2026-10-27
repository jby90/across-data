"""LLM egress metadata and tenant aliases. Existing evidence is unchanged."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("organizations", sa.Column("llm_redaction_terms", pg.JSONB(), nullable=False,
                                             server_default=sa.text("'[]'::jsonb")))
    op.create_table(
        "llm_call_audit",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("call_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("org_id", pg.UUID(as_uuid=True), sa.ForeignKey("organizations.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("purpose", sa.String(64), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("redaction_mode", sa.String(8), nullable=False),
        sa.Column("redaction_hits", pg.JSONB(), nullable=False),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("tokens_in", sa.Integer(), nullable=False),
        sa.Column("tokens_out", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
    )
    op.create_index("ix_llm_call_audit_call_id", "llm_call_audit", ["call_id"])
    op.create_index("ix_llm_call_audit_org_id", "llm_call_audit", ["org_id"])
    op.execute("""CREATE FUNCTION protect_llm_audit() RETURNS trigger AS $$ BEGIN
        RAISE EXCEPTION 'LLM audit is append-only'; END; $$ LANGUAGE plpgsql;""")
    op.execute("""CREATE TRIGGER llm_audit_append_only BEFORE UPDATE OR DELETE ON llm_call_audit
        FOR EACH ROW EXECUTE FUNCTION protect_llm_audit();""")


def downgrade():
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT FROM llm_call_audit) THEN
        RAISE EXCEPTION 'retain LLM audit; downgrade refused'; END IF; END $$;""")
    op.drop_table("llm_call_audit")
    op.execute("DROP FUNCTION protect_llm_audit()")
    op.drop_column("organizations", "llm_redaction_terms")
