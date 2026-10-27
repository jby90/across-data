"""Tenant ownership metadata and forced row-level security; existing payloads unchanged."""

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

CHILDREN = ("analysis_step_traces", "analysis_messages", "report_versions")
TASK_BOUND = (*CHILDREN, "analysis_events", "update_impacts", "user_notifications")
TENANT_TABLES = ("analysis_tasks", *TASK_BOUND, "llm_call_audit")
ALL_TABLES = (*TENANT_TABLES, "organizations", "users", "knowledge_audit_records")
SCOPE = "current_setting('app.scope', true)"
ORG = "NULLIF(current_setting('app.org_id', true),'')::uuid"
USER = "NULLIF(current_setting('app.user_id', true),'')::uuid"
EMAIL = "NULLIF(current_setting('app.auth_email', true),'')"
ADMIN = f"{SCOPE} IN ('platform','reviewer','system')"
PUBLIC_AUDIT = "'documents','document_versions','clauses','clause_translations','kg_nodes','kg_edges','kg_country','glossary_terms','source_sites'"


def policy(table, expression, write_expression=None):
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    if write_expression is None:
        op.execute(
            f"CREATE POLICY tenant_access ON {table} USING ({expression}) WITH CHECK ({expression})"
        )
    else:
        op.execute(f"CREATE POLICY tenant_read ON {table} FOR SELECT USING ({expression})")
        op.execute(
            f"CREATE POLICY tenant_insert ON {table} FOR INSERT WITH CHECK ({write_expression})"
        )
        op.execute(
            f"CREATE POLICY tenant_update ON {table} FOR UPDATE USING ({write_expression}) WITH CHECK ({write_expression})"
        )
        op.execute(f"CREATE POLICY tenant_delete ON {table} FOR DELETE USING ({write_expression})")


def upgrade():
    # A group role has no login or embedded password. Deployment provisions a separate
    # non-owner login and grants membership through its secret-controlled bootstrap.
    op.execute("""DO $$ BEGIN
        IF NOT EXISTS(SELECT FROM pg_roles WHERE rolname='cyshuhe_app') THEN
            CREATE ROLE cyshuhe_app NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION;
        ELSIF EXISTS(SELECT FROM pg_roles WHERE rolname='cyshuhe_app' AND
            (rolsuper OR rolbypassrls OR rolcreaterole OR rolcreatedb OR rolreplication)) THEN
            RAISE EXCEPTION 'cyshuhe_app must be an ordinary application group';
        END IF;
    END $$;""")
    for table in (*CHILDREN, "knowledge_audit_records"):
        op.add_column(table, sa.Column("org_id", sa.UUID(), nullable=True))
        op.create_foreign_key(f"fk_{table}_org", table, "organizations", ["org_id"], ["id"])
        op.create_index(f"ix_{table}_org_id", table, ["org_id"])
    for table in CHILDREN:
        op.execute(
            f"UPDATE {table} c SET org_id=t.org_id FROM analysis_tasks t WHERE c.task_id=t.id"
        )
    # DDL/row locks protect this migration transaction. Only a NEW metadata column
    # is backfilled; the immutable audit payload/hash/approvals are never rewritten.
    op.execute("ALTER TABLE knowledge_audit_records DISABLE TRIGGER knowledge_audit_immutable")
    op.execute("""UPDATE knowledge_audit_records a SET org_id=o.id FROM organizations o
        WHERE a.target_type='organization_redaction' AND a.target_id=o.id""")
    op.execute("ALTER TABLE knowledge_audit_records ENABLE TRIGGER knowledge_audit_immutable")
    op.execute("""CREATE FUNCTION enforce_task_tenant() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
        DECLARE parent_org uuid; parent_task uuid; related_change uuid;
        BEGIN
            SELECT org_id INTO parent_org FROM public.analysis_tasks WHERE id=NEW.task_id;
            IF NOT FOUND THEN RAISE EXCEPTION 'analysis task unavailable'; END IF;
            IF NEW.org_id IS NOT NULL AND NEW.org_id IS DISTINCT FROM parent_org THEN
                RAISE EXCEPTION 'child organization does not match task'; END IF;
            NEW.org_id=parent_org;
            IF TG_TABLE_NAME='update_impacts' THEN
                SELECT task_id INTO parent_task FROM public.report_versions WHERE id=NEW.report_version_id;
                IF NOT FOUND OR parent_task IS DISTINCT FROM NEW.task_id THEN
                    RAISE EXCEPTION 'report does not match impact task'; END IF;
            ELSIF TG_TABLE_NAME='user_notifications' THEN
                SELECT task_id,change_id INTO parent_task,related_change FROM public.update_impacts WHERE id=NEW.impact_id;
                IF NOT FOUND OR parent_task IS DISTINCT FROM NEW.task_id OR related_change IS DISTINCT FROM NEW.change_id THEN
                    RAISE EXCEPTION 'notification does not match impact'; END IF;
                IF NOT EXISTS(SELECT FROM public.users WHERE id=NEW.user_id AND org_id=parent_org) THEN
                    RAISE EXCEPTION 'notification recipient does not match task organization'; END IF;
            END IF;
            RETURN NEW;
        END $$;""")
    for table in TASK_BOUND:
        op.execute(
            f"CREATE TRIGGER tenant_parent_guard BEFORE INSERT OR UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION enforce_task_tenant()"
        )
    op.execute("""CREATE FUNCTION protect_task_tenant() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            IF NEW.org_id IS DISTINCT FROM OLD.org_id THEN RAISE EXCEPTION 'task ownership is immutable'; END IF;
            RETURN NEW;
        END $$;""")
    op.execute(
        "CREATE TRIGGER task_tenant_immutable BEFORE UPDATE ON analysis_tasks FOR EACH ROW EXECUTE FUNCTION protect_task_tenant()"
    )
    op.execute(f"""CREATE FUNCTION assign_audit_tenant() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            IF NEW.target_type='organization_redaction' THEN NEW.org_id=NEW.target_id;
            ELSIF NEW.target_type IN ({PUBLIC_AUDIT}) THEN NEW.org_id=NULL;
            ELSIF NEW.org_id IS NULL THEN NEW.org_id={ORG}; END IF;
            RETURN NEW;
        END $$;""")
    op.execute(
        "CREATE TRIGGER audit_tenant_guard BEFORE INSERT ON knowledge_audit_records FOR EACH ROW EXECUTE FUNCTION assign_audit_tenant()"
    )
    for table in TENANT_TABLES:
        policy(table, f"{ADMIN} OR ({SCOPE}='tenant' AND org_id={ORG})")
    policy(
        "users",
        f"{ADMIN} OR ({SCOPE}='tenant' AND org_id={ORG}) OR ({SCOPE}='identity' AND id={USER}) OR ({SCOPE}='auth' AND email={EMAIL})",
        f"{ADMIN} OR ({SCOPE}='tenant' AND org_id={ORG})",
    )
    policy(
        "organizations",
        f"{ADMIN} OR ({SCOPE}='tenant' AND id={ORG}) OR ({SCOPE}='identity' AND EXISTS(SELECT FROM users u WHERE u.org_id=organizations.id AND u.id={USER})) OR ({SCOPE}='auth' AND EXISTS(SELECT FROM users u WHERE u.org_id=organizations.id AND u.email={EMAIL}))",
        f"{ADMIN} OR ({SCOPE}='tenant' AND id={ORG})",
    )
    policy(
        "knowledge_audit_records",
        f"{ADMIN} OR ({SCOPE}='tenant' AND org_id={ORG}) OR (org_id IS NULL AND target_type IN ({PUBLIC_AUDIT}))",
        f"{ADMIN} OR ({SCOPE}='tenant' AND org_id={ORG})",
    )
    op.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    op.execute("GRANT USAGE ON SCHEMA public TO cyshuhe_app")
    op.execute("GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO cyshuhe_app")
    op.execute("GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO cyshuhe_app")
    op.execute("REVOKE ALL ON alembic_version FROM cyshuhe_app")
    op.execute(
        "REVOKE UPDATE,DELETE ON llm_call_audit,knowledge_audit_records,source_fetch_observations,version_changes FROM cyshuhe_app"
    )


def downgrade():
    for table in ALL_TABLES:
        op.execute(f"""DO $$ BEGIN IF EXISTS(SELECT FROM {table}) THEN
            RAISE EXCEPTION 'retain tenant isolation metadata; rollback code without removing RLS'; END IF; END $$;""")
    for table in ALL_TABLES:
        for name in (
            "tenant_access",
            "tenant_read",
            "tenant_insert",
            "tenant_update",
            "tenant_delete",
        ):
            op.execute(f"DROP POLICY IF EXISTS {name} ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
    for table in TASK_BOUND:
        op.execute(f"DROP TRIGGER tenant_parent_guard ON {table}")
    op.execute("DROP TRIGGER task_tenant_immutable ON analysis_tasks")
    op.execute("DROP TRIGGER audit_tenant_guard ON knowledge_audit_records")
    for name in ("enforce_task_tenant", "protect_task_tenant", "assign_audit_tenant"):
        op.execute(f"DROP FUNCTION {name}()")
    for table in (*CHILDREN, "knowledge_audit_records"):
        op.drop_index(f"ix_{table}_org_id", table_name=table)
        op.drop_constraint(f"fk_{table}_org", table, type_="foreignkey")
        op.drop_column(table, "org_id")
