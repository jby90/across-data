"""knowledge glossary, translations, review points, and 6-element 12-relation graph

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-22 16:45:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "glossary_terms",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("concept_code", sa.String(length=64), nullable=False),
        sa.Column("country_code", sa.String(length=2), nullable=True),
        sa.Column("language", sa.String(length=8), nullable=False),
        sa.Column("source_term", sa.String(length=255), nullable=False),
        sa.Column("target_zh", sa.String(length=255), nullable=False),
        sa.Column("priority_level", sa.Integer(), server_default="1", nullable=False),
        sa.Column("statutory_definition", sa.Text(), nullable=True),
        sa.Column("source_reference", sa.String(length=255), nullable=True),
        sa.Column(
            "evidence_clause_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("clauses.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("review_status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column(
            "reviewed_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("priority_level IN (1, 2, 3, 4)", name="ck_glossary_priority"),
        sa.CheckConstraint(
            "review_status IN ('pending','approved','rejected')",
            name="ck_glossary_review_status",
        ),
    )
    op.create_index("ix_glossary_terms_concept_code", "glossary_terms", ["concept_code"])
    op.create_index("ix_glossary_terms_country_code", "glossary_terms", ["country_code"])
    op.create_index(
        "ix_glossary_terms_evidence_clause_id", "glossary_terms", ["evidence_clause_id"]
    )

    op.create_table(
        "clause_translations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "clause_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("clauses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("translated_text", sa.Text(), nullable=False),
        sa.Column("engine", sa.String(length=32), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=False),
        sa.Column("prompt_version", sa.String(length=32), server_default="v1", nullable=False),
        sa.Column("glossary_hits", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("unmatched_terms", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("quality_check_passed", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("quality_issues", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "review_status",
            sa.String(length=24),
            server_default="machine_translated",
            nullable=False,
        ),
        sa.Column(
            "reviewer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("reviewer_note", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("clause_id", name="uq_clause_translations_clause"),
        sa.CheckConstraint(
            "review_status IN ('machine_translated','pending_review','approved','rejected','stale')",
            name="ck_translations_review_status",
        ),
    )
    op.create_index("ix_clause_translations_clause_id", "clause_translations", ["clause_id"])

    op.create_table(
        "review_points",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("country_code", sa.String(length=2), nullable=False),
        sa.Column("point_code", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("impact_on_conclusion", sa.Text(), nullable=False),
        sa.Column("provisional_interpretation", sa.Text(), nullable=False),
        sa.Column(
            "clause_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("clauses.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "term_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("glossary_terms.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(length=24), server_default="open", nullable=False),
        sa.Column("lawyer_reply", sa.Text(), nullable=True),
        sa.Column(
            "decided_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("point_code", name="uq_review_points_code"),
        sa.CheckConstraint(
            "status IN ('open','confirmed','needs_discussion')",
            name="ck_review_points_status",
        ),
    )
    op.create_index("ix_review_points_country_code", "review_points", ["country_code"])
    op.create_index("ix_review_points_clause_id", "review_points", ["clause_id"])
    op.create_index("ix_review_points_term_id", "review_points", ["term_id"])

    op.create_table(
        "kg_nodes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("country_code", sa.String(length=2), nullable=False),
        sa.Column("node_type", sa.String(length=24), nullable=False),
        sa.Column("node_key", sa.String(length=128), nullable=False),
        sa.Column("name_zh", sa.String(length=255), nullable=False),
        sa.Column("name_orig", sa.String(length=255), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("attributes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "evidence_clause_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("clauses.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "review_status",
            sa.String(length=24),
            server_default="pending_review",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("country_code", "node_key", name="uq_kg_nodes_country_key"),
        sa.CheckConstraint(
            "node_type IN ('COUNTRY','REGULATOR','INSTRUMENT','DATA_CATEGORY','OBLIGATION','PENALTY')",
            name="ck_kg_nodes_type",
        ),
        sa.CheckConstraint(
            "review_status IN ('pending_review','approved','rejected','stale')",
            name="ck_kg_nodes_review_status",
        ),
    )
    op.create_index("ix_kg_nodes_country_code", "kg_nodes", ["country_code"])
    op.create_index("ix_kg_nodes_node_type", "kg_nodes", ["node_type"])
    op.create_index("ix_kg_nodes_document_id", "kg_nodes", ["document_id"])
    op.create_index("ix_kg_nodes_evidence_clause_id", "kg_nodes", ["evidence_clause_id"])

    op.create_table(
        "kg_edges",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("country_code", sa.String(length=2), nullable=False),
        sa.Column(
            "source_node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("kg_nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "target_node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("kg_nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("relation_type", sa.String(length=32), nullable=False),
        sa.Column(
            "evidence_clause_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("clauses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("evidence_quote", sa.Text(), nullable=False),
        sa.Column("extractor_model", sa.String(length=128), nullable=False),
        sa.Column(
            "review_status",
            sa.String(length=24),
            server_default="pending_review",
            nullable=False,
        ),
        sa.Column(
            "reviewed_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "source_node_id",
            "target_node_id",
            "relation_type",
            "evidence_clause_id",
            name="uq_kg_edges_triple_clause",
        ),
        sa.CheckConstraint(
            "relation_type IN ("
            "'PUBLISHES','AMENDS','REPEALS','GOVERNS','PROHIBITS','REQUIRES',"
            "'EXEMPTS','SUPERVISES','PENALIZES','APPLIES_TO_SCENARIO',"
            "'RESTRICTS_CROSS_BORDER','REQUIRES_ADMISSION'"
            ")",
            name="ck_kg_edges_relation",
        ),
        sa.CheckConstraint(
            "review_status IN ('pending_review','approved','rejected','stale')",
            name="ck_kg_edges_review_status",
        ),
    )
    op.create_index("ix_kg_edges_country_code", "kg_edges", ["country_code"])
    op.create_index("ix_kg_edges_source_node_id", "kg_edges", ["source_node_id"])
    op.create_index("ix_kg_edges_target_node_id", "kg_edges", ["target_node_id"])
    op.create_index("ix_kg_edges_relation_type", "kg_edges", ["relation_type"])
    op.create_index("ix_kg_edges_evidence_clause_id", "kg_edges", ["evidence_clause_id"])


def downgrade() -> None:
    op.drop_table("kg_edges")
    op.drop_table("kg_nodes")
    op.drop_table("review_points")
    op.drop_table("clause_translations")
    op.drop_table("glossary_terms")
