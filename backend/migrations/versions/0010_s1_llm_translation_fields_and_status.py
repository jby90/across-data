"""s1 llm translation fields and status

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-22 23:55:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "clause_translations",
        sa.Column("tokens_in", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "clause_translations",
        sa.Column("tokens_out", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "clause_translations",
        sa.Column("duration_ms", sa.Integer(), server_default="0", nullable=False),
    )
    op.drop_constraint(
        "uq_clause_translations_clause",
        "clause_translations",
        type_="unique",
    )
    op.drop_constraint(
        "ck_translations_review_status",
        "clause_translations",
        type_="check",
    )
    op.create_check_constraint(
        "ck_translations_review_status",
        "clause_translations",
        "review_status IN ("
        "'machine_translated','translation_failed','not_translated',"
        "'pending_review','approved','rejected','stale','superseded'"
        ")",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_translations_review_status",
        "clause_translations",
        type_="check",
    )
    op.execute(
        """
        DELETE FROM clause_translations
        WHERE review_status IN ('translation_failed', 'not_translated', 'superseded')
        """
    )
    op.create_check_constraint(
        "ck_translations_review_status",
        "clause_translations",
        "review_status IN ('machine_translated','pending_review','approved','rejected','stale')",
    )
    op.create_unique_constraint(
        "uq_clause_translations_clause",
        "clause_translations",
        ["clause_id"],
    )
    op.drop_column("clause_translations", "duration_ms")
    op.drop_column("clause_translations", "tokens_out")
    op.drop_column("clause_translations", "tokens_in")
