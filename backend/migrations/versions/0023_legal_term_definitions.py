"""Source-bound legal terminology, preserving existing glossary and approvals."""

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE legal_term_definitions (
      id UUID PRIMARY KEY, concept_code VARCHAR(128) NOT NULL,
      country_code VARCHAR(2), language VARCHAR(8) NOT NULL,
      source_term VARCHAR(255) NOT NULL, target_zh VARCHAR(255) NOT NULL,
      definition_original TEXT NOT NULL, definition_zh TEXT,
      applies_to_document_id UUID REFERENCES documents(id) ON DELETE RESTRICT,
      source_kind VARCHAR(32) NOT NULL, source_url VARCHAR(2048) NOT NULL,
      source_reference VARCHAR(500) NOT NULL,
      document_id UUID REFERENCES documents(id) ON DELETE RESTRICT,
      version_id UUID REFERENCES document_versions(id) ON DELETE RESTRICT,
      clause_id UUID REFERENCES clauses(id) ON DELETE RESTRICT,
      quote_start INTEGER, quote_end INTEGER,
      source_text_sha256 VARCHAR(64) NOT NULL, fingerprint VARCHAR(64) NOT NULL,
      verification_status VARCHAR(32) NOT NULL,
      registration_origin VARCHAR(24) NOT NULL DEFAULT 'auto',
      created_by UUID REFERENCES users(id) ON DELETE RESTRICT,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      CONSTRAINT uq_legal_term_fingerprint UNIQUE (fingerprint)
    )""")
    for name in ("concept_code", "country_code", "document_id", "clause_id"):
        op.execute(
            f"CREATE INDEX ix_legal_term_definitions_{name} ON legal_term_definitions ({name})"
        )
    op.execute("GRANT SELECT, INSERT ON legal_term_definitions TO cyshuhe_app")


def downgrade():
    raise RuntimeError(
        "Definition provenance must be archived before an explicit destructive rollback"
    )
