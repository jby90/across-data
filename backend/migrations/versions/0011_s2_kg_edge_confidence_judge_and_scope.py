"""s2 kg edge confidence judge and scope

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-23 09:12:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_documents_relevance", "documents", type_="check")
    op.create_check_constraint(
        "ck_documents_relevance",
        "documents",
        "relevance IN ('relevant','irrelevant','out_of_scope','needs_review')",
    )

    op.drop_constraint("ck_kg_nodes_review_status", "kg_nodes", type_="check")
    op.create_check_constraint(
        "ck_kg_nodes_review_status",
        "kg_nodes",
        "review_status IN ('pending_review','approved','rejected','stale','superseded')",
    )

    op.drop_constraint("uq_kg_edges_triple_clause", "kg_edges", type_="unique")
    op.drop_constraint("ck_kg_edges_review_status", "kg_edges", type_="check")
    op.create_check_constraint(
        "ck_kg_edges_review_status",
        "kg_edges",
        "review_status IN ('pending_review','approved','rejected','stale','superseded')",
    )
    op.add_column(
        "kg_edges",
        sa.Column("confidence", sa.Float(), server_default="0.85", nullable=False),
    )
    op.add_column(
        "kg_edges",
        sa.Column(
            "judge_verdict",
            sa.String(length=16),
            server_default="yes",
            nullable=False,
        ),
    )
    op.add_column(
        "kg_edges",
        sa.Column("judge_reason", sa.Text(), nullable=True),
    )
    op.add_column(
        "kg_edges",
        sa.Column(
            "is_structural",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("kg_edges", "is_structural")
    op.drop_column("kg_edges", "judge_reason")
    op.drop_column("kg_edges", "judge_verdict")
    op.drop_column("kg_edges", "confidence")

    op.drop_constraint("ck_kg_edges_review_status", "kg_edges", type_="check")
    op.execute("DELETE FROM kg_edges WHERE review_status = 'superseded'")
    op.create_check_constraint(
        "ck_kg_edges_review_status",
        "kg_edges",
        "review_status IN ('pending_review','approved','rejected','stale')",
    )
    op.create_unique_constraint(
        "uq_kg_edges_triple_clause",
        "kg_edges",
        ["source_node_id", "target_node_id", "relation_type", "evidence_clause_id"],
    )

    op.drop_constraint("ck_kg_nodes_review_status", "kg_nodes", type_="check")
    op.execute("DELETE FROM kg_nodes WHERE review_status = 'superseded'")
    op.create_check_constraint(
        "ck_kg_nodes_review_status",
        "kg_nodes",
        "review_status IN ('pending_review','approved','rejected','stale')",
    )

    op.drop_constraint("ck_documents_relevance", "documents", type_="check")
    op.execute("UPDATE documents SET relevance = 'irrelevant' WHERE relevance = 'out_of_scope'")
    op.create_check_constraint(
        "ck_documents_relevance",
        "documents",
        "relevance IN ('relevant','irrelevant','needs_review')",
    )
