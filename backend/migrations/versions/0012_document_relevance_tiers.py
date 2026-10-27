"""document relevance tiers (relevant / related / out_of_scope) and decision source

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-23 13:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_documents_relevance", "documents", type_="check")
    op.create_check_constraint(
        "ck_documents_relevance",
        "documents",
        "relevance IN ('relevant','related','irrelevant','out_of_scope','needs_review')",
    )
    op.add_column(
        "documents",
        sa.Column(
            "relevance_source",
            sa.String(length=16),
            server_default="rule",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("documents", "relevance_source")
    op.drop_constraint("ck_documents_relevance", "documents", type_="check")
    op.execute("UPDATE documents SET relevance = 'relevant' WHERE relevance = 'related'")
    op.create_check_constraint(
        "ck_documents_relevance",
        "documents",
        "relevance IN ('relevant','irrelevant','out_of_scope','needs_review')",
    )
