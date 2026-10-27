"""Keep legacy source decisions, require an explicit production approval scope.

Revision ID: 0014
Revises: 0013
"""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "source_sites",
        sa.Column("terms_approval_scope", sa.String(16), nullable=False, server_default="legacy"),
    )


def downgrade():
    op.drop_column("source_sites", "terms_approval_scope")
