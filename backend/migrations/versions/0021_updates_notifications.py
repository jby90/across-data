"""Update observations, report impacts, reminders, and measured first parse time.

Existing sources, clauses, reports and approval records are unchanged.
"""

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

# Frozen DDL; no dependency on mutable application models at migration time.
STATEMENTS = [
    "\nCREATE TABLE regulatory_discoveries (\n\tid UUID NOT NULL, \n\tsite_id UUID, \n\tcountry_code VARCHAR(8) NOT NULL, \n\tsource_url VARCHAR(2048) NOT NULL, \n\ttitle VARCHAR(500) NOT NULL, \n\tdiscovered_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tlast_seen_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tdocument_id UUID, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_discovery_site_url UNIQUE (site_id, source_url), \n\tFOREIGN KEY(site_id) REFERENCES source_sites (id) ON DELETE SET NULL, \n\tFOREIGN KEY(document_id) REFERENCES documents (id)\n)\n\n",
    "CREATE INDEX ix_discovery_country_time ON regulatory_discoveries (country_code, discovered_at)",
    "CREATE INDEX ix_regulatory_discoveries_document_id ON regulatory_discoveries (document_id)",
    "CREATE INDEX ix_regulatory_discoveries_site_id ON regulatory_discoveries (site_id)",
    "\nCREATE TABLE source_fetch_observations (\n\tid BIGSERIAL NOT NULL, \n\tdocument_id UUID NOT NULL, \n\tversion_id UUID NOT NULL, \n\tprevious_version_id UUID, \n\tobserved_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tcomparison_basis VARCHAR(32) NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(document_id) REFERENCES documents (id), \n\tFOREIGN KEY(version_id) REFERENCES document_versions (id), \n\tFOREIGN KEY(previous_version_id) REFERENCES document_versions (id)\n)\n\n",
    "CREATE INDEX ix_source_fetch_doc_sequence ON source_fetch_observations (document_id, id)",
    "CREATE INDEX ix_source_fetch_observations_document_id ON source_fetch_observations (document_id)",
    "\nCREATE TABLE version_changes (\n\tid UUID NOT NULL, \n\torigin_key VARCHAR(100) NOT NULL, \n\tsource_observation_id BIGINT, \n\tdocument_id UUID NOT NULL, \n\told_version_id UUID NOT NULL, \n\tnew_version_id UUID NOT NULL, \n\tdetected_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tchange_kind VARCHAR(40) NOT NULL, \n\tsummary TEXT NOT NULL, \n\tchanges_json JSONB NOT NULL, \n\tgraph_edge_ids JSONB NOT NULL, \n\tcontent_hashes JSONB NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_version_change_origin UNIQUE (origin_key), \n\tUNIQUE (source_observation_id), \n\tFOREIGN KEY(source_observation_id) REFERENCES source_fetch_observations (id), \n\tFOREIGN KEY(document_id) REFERENCES documents (id), \n\tFOREIGN KEY(old_version_id) REFERENCES document_versions (id), \n\tFOREIGN KEY(new_version_id) REFERENCES document_versions (id)\n)\n\n",
    "CREATE INDEX ix_version_changes_old_version_id ON version_changes (old_version_id)",
    "\nCREATE TABLE update_impacts (\n\tid UUID NOT NULL, \n\torg_id UUID, \n\ttask_id UUID NOT NULL, \n\treport_version_id UUID NOT NULL, \n\tdimension_code VARCHAR(8) NOT NULL, \n\tchange_id UUID NOT NULL, \n\tdetected_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tdue_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tstate VARCHAR(24) NOT NULL, \n\tresolved_report_id UUID, \n\tresolved_at TIMESTAMP WITH TIME ZONE, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_report_change_impact UNIQUE (report_version_id, dimension_code, change_id), \n\tFOREIGN KEY(org_id) REFERENCES organizations (id), \n\tFOREIGN KEY(task_id) REFERENCES analysis_tasks (id), \n\tFOREIGN KEY(report_version_id) REFERENCES report_versions (id), \n\tFOREIGN KEY(change_id) REFERENCES version_changes (id), \n\tFOREIGN KEY(resolved_report_id) REFERENCES report_versions (id)\n)\n\n",
    "CREATE INDEX ix_update_impacts_change_id ON update_impacts (change_id)",
    "CREATE INDEX ix_update_impacts_org_id ON update_impacts (org_id)",
    "CREATE INDEX ix_update_impacts_state_due ON update_impacts (state, due_at)",
    "CREATE INDEX ix_update_impacts_task_id ON update_impacts (task_id)",
    "\nCREATE TABLE user_notifications (\n\tid UUID NOT NULL, \n\torg_id UUID NOT NULL, \n\tuser_id UUID NOT NULL, \n\timpact_id UUID NOT NULL, \n\ttask_id UUID NOT NULL, \n\tchange_id UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tread_at TIMESTAMP WITH TIME ZONE, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_change_recipient UNIQUE (task_id, change_id, user_id), \n\tFOREIGN KEY(org_id) REFERENCES organizations (id), \n\tFOREIGN KEY(user_id) REFERENCES users (id), \n\tFOREIGN KEY(impact_id) REFERENCES update_impacts (id), \n\tFOREIGN KEY(task_id) REFERENCES analysis_tasks (id), \n\tFOREIGN KEY(change_id) REFERENCES version_changes (id)\n)\n\n",
    "CREATE INDEX ix_notifications_user_created ON user_notifications (user_id, created_at)",
    "CREATE INDEX ix_user_notifications_change_id ON user_notifications (change_id)",
    "CREATE INDEX ix_user_notifications_impact_id ON user_notifications (impact_id)",
    "CREATE INDEX ix_user_notifications_org_id ON user_notifications (org_id)",
    "CREATE INDEX ix_user_notifications_task_id ON user_notifications (task_id)",
    "CREATE INDEX ix_user_notifications_user_id ON user_notifications (user_id)",
]


def upgrade():
    op.add_column(
        "document_versions", sa.Column("parsed_at", sa.DateTime(timezone=True), nullable=True)
    )
    for statement in STATEMENTS:
        op.execute(statement)
    op.create_index(
        "ix_report_versions_citation_snapshot",
        "report_versions",
        ["dimensions_json"],
        postgresql_using="gin",
    )
    op.execute("""CREATE FUNCTION protect_source_change_history() RETURNS trigger AS $$ BEGIN
        RAISE EXCEPTION 'source change history is append-only'; END; $$ LANGUAGE plpgsql;""")
    for table in ("source_fetch_observations", "version_changes"):
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION protect_source_change_history()"
        )


def downgrade():
    for table in (
        "regulatory_discoveries",
        "source_fetch_observations",
        "version_changes",
        "update_impacts",
        "user_notifications",
    ):
        op.execute(f"""DO $$ BEGIN IF EXISTS(SELECT FROM {table}) THEN
            RAISE EXCEPTION 'retain update evidence and notification history'; END IF; END $$;""")
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT FROM document_versions WHERE parsed_at IS NOT NULL)
        THEN RAISE EXCEPTION 'retain measured parsing timestamps'; END IF; END $$;""")
    for table in (
        "user_notifications",
        "update_impacts",
        "version_changes",
        "source_fetch_observations",
        "regulatory_discoveries",
    ):
        op.drop_table(table)
    op.execute("DROP FUNCTION protect_source_change_history()")
    op.drop_index("ix_report_versions_citation_snapshot", table_name="report_versions")
    op.drop_column("document_versions", "parsed_at")
