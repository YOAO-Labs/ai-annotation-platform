"""Fence connector probes and updates through the credential-owning runtime."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0376"
down_revision = "bo0375"
branch_labels = depends_on = None

_SQL = (
    Path(__file__).resolve().parents[1] / "sql" / "0376_assets_connection_runtime.sql"
)
_FUNCTIONS = (
    "aap_assets_v1._connection_probe_reply(jsonb,text)",
    "aap_assets_v1._connection_authority_proof(uuid,uuid,text[],jsonb)",
    "aap_assets_v1.begin_connection_probe(uuid,uuid,uuid,text[])",
    "aap_assets_v1.finish_connection_probe(uuid,uuid,uuid,uuid,text[],jsonb,boolean)",
    "aap_assets_v1.update_storage_connection_authorized(uuid,uuid,uuid,jsonb,jsonb,text,text,text,jsonb,jsonb,uuid,text[],jsonb)",
)


def upgrade() -> None:
    for statement in _SQL.read_text(encoding="utf-8").split("-- aap-statement-break"):
        if statement.strip():
            op.execute(
                sa.text(statement.strip().replace(":", r"\:")).execution_options(
                    no_parameters=True
                )
            )


def downgrade() -> None:
    op.execute(
        """
        DO $restore_grants$
        DECLARE
            grant_row record;
        BEGIN
            FOR grant_row IN
                SELECT role.rolname, acl.is_grantable
                FROM pg_catalog.pg_proc AS procedure
                CROSS JOIN LATERAL pg_catalog.aclexplode(procedure.proacl) AS acl
                JOIN pg_catalog.pg_roles AS role ON role.oid = acl.grantee
                WHERE procedure.oid = 'aap_assets_v1.update_storage_connection_authorized(uuid,uuid,uuid,jsonb,jsonb,text,text,text,jsonb,jsonb,uuid,text[],jsonb)'::regprocedure
                  AND acl.privilege_type = 'EXECUTE'
                  AND acl.grantee <> procedure.proowner
            LOOP
                EXECUTE pg_catalog.format(
                    'GRANT EXECUTE ON FUNCTION aap_assets_v1.update_storage_connection(uuid,uuid,uuid,jsonb,jsonb,text,text,text,jsonb,jsonb) TO %I%s',
                    grant_row.rolname,
                    CASE WHEN grant_row.is_grantable THEN ' WITH GRANT OPTION' ELSE '' END
                );
            END LOOP;
        END;
        $restore_grants$;
        """
    )
    for signature in reversed(_FUNCTIONS):
        op.execute(f"DROP FUNCTION {signature} RESTRICT")
