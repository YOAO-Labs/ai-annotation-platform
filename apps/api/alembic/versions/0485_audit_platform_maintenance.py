"""Borrow the Audit owner's exact partition locks, stream and retirement."""

from importlib.resources import files

import sqlalchemy as sa
from alembic import op

revision = "bo0485"
down_revision = "bo0484"
branch_labels = depends_on = None


_FUNCTIONS = (
    "_assert_platform_partition(date,bigint,bigint)",
    "platform_maintenance(text,date,bigint,bigint,text,uuid,timestamptz)",
    "archive_output_has_ready(integer,integer)",
    "archive_output_ready_month(integer,integer)",
    "archive_output_pending(uuid)",
    "archive_output_ready_list()",
    "archive_output_history()",
    "archive_output_save_candidate(uuid,uuid,date,bigint,bigint,bigint,bigint,text,bigint,text,jsonb,timestamptz)",
)


_OWNER_SETUP = f"""
DO $owner$
DECLARE
    audit_owner name;
    capability text;
    had_create boolean;
BEGIN
    SELECT pg_catalog.pg_get_userbyid(relowner) INTO STRICT audit_owner
    FROM pg_catalog.pg_class WHERE oid = 'public.audit_logs'::regclass;
    IF audit_owner = current_user THEN
        RETURN;
    END IF;
    EXECUTE format('GRANT SELECT, INSERT, UPDATE (state,published_at)
        ON public.audit_archive_outputs TO %I', audit_owner);
    had_create := pg_catalog.has_schema_privilege(audit_owner, 'yoao_audit_v1', 'CREATE');
    IF NOT had_create THEN
        EXECUTE format('GRANT CREATE ON SCHEMA yoao_audit_v1 TO %I', audit_owner);
    END IF;
    FOREACH capability IN ARRAY ARRAY[{", ".join(f"'{name}'" for name in _FUNCTIONS)}] LOOP
        EXECUTE format('ALTER FUNCTION yoao_audit_v1.%s OWNER TO %I', capability, audit_owner);
    END LOOP;
    IF NOT had_create THEN
        EXECUTE format('REVOKE CREATE ON SCHEMA yoao_audit_v1 FROM %I', audit_owner);
    END IF;
END;
$owner$;
"""


def _execute(statement: str) -> None:
    op.execute(
        sa.text(statement.replace(":", r"\:")).execution_options(no_parameters=True)
    )


def upgrade() -> None:
    resource = files("yoao_audit.sql").joinpath("0485_audit_platform_maintenance.sql")
    for statement in resource.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        _execute(statement)
    _execute(_OWNER_SETUP)


def downgrade() -> None:
    _execute("""
        DO $owner$
        DECLARE
            audit_owner name;
        BEGIN
            SELECT pg_catalog.pg_get_userbyid(fn.proowner) INTO STRICT audit_owner
            FROM pg_catalog.pg_proc AS fn
            WHERE fn.oid = 'yoao_audit_v1.platform_maintenance(text,date,bigint,bigint,text,uuid,timestamptz)'::regprocedure;
            IF audit_owner <> pg_catalog.pg_get_userbyid((
                SELECT relowner FROM pg_catalog.pg_class
                WHERE oid = 'public.audit_archive_outputs'::regclass
            )) THEN
                EXECUTE format('REVOKE SELECT, INSERT, UPDATE (state,published_at)
                    ON public.audit_archive_outputs FROM %I', audit_owner);
            END IF;
        END;
        $owner$;
    """)
    for signature in reversed(_FUNCTIONS):
        op.execute(f"DROP FUNCTION yoao_audit_v1.{signature}")
