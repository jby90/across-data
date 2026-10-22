"""allow egazette strategy on source_sites

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-22 15:33:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_sites_strategy", "source_sites", type_="check")
    op.create_check_constraint(
        "ck_sites_strategy",
        "source_sites",
        "strategy IN ('DIRECT','INDEX_PAGE','EGAZETTE')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_sites_strategy", "source_sites", type_="check")
    op.create_check_constraint(
        "ck_sites_strategy",
        "source_sites",
        "strategy IN ('DIRECT','INDEX_PAGE')",
    )
