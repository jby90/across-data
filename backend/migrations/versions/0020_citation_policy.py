"""Pin policy on new tasks/reports; old reports stay unrecorded and unchanged."""

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade():
    for name in ("analysis_tasks", "report_versions"):
        op.add_column(name, sa.Column("citation_policy", sa.String(40), nullable=True))


def downgrade():
    for name in ("analysis_tasks", "report_versions"):
        op.execute(f"""DO $$ BEGIN IF EXISTS(SELECT FROM {name} WHERE citation_policy IS NOT NULL)
            THEN RAISE EXCEPTION 'retain citation policy history'; END IF; END $$;""")
    for name in ("analysis_tasks", "report_versions"):
        op.drop_column(name, "citation_policy")
