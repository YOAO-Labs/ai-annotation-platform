"""Remove implicit PUBLIC execution from Mask editing capabilities."""

from alembic import op

revision = "bo0408"
down_revision = "bo0407"
branch_labels = depends_on = None


def upgrade() -> None:
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "aap_labeling_v1.mask_edit_command(jsonb), "
        "aap_labeling_v1.mask_revision_command(jsonb), "
        "aap_labeling_v1.ai_mask_accept_command(jsonb) FROM PUBLIC"
    )
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM pg_catalog.pg_proc AS function
                CROSS JOIN LATERAL pg_catalog.aclexplode(
                    COALESCE(function.proacl, pg_catalog.acldefault('f', function.proowner))
                ) AS acl
                WHERE function.oid IN (
                    'aap_labeling_v1.mask_edit_command(jsonb)'::pg_catalog.regprocedure,
                    'aap_labeling_v1.mask_revision_command(jsonb)'::pg_catalog.regprocedure,
                    'aap_labeling_v1.ai_mask_accept_command(jsonb)'::pg_catalog.regprocedure
                ) AND acl.grantee = 0 AND acl.privilege_type = 'EXECUTE'
            ) THEN
                RAISE EXCEPTION 'Migration owner could not revoke PUBLIC Mask execution';
            END IF;
        END $$
    """)


def downgrade() -> None:
    raise RuntimeError("Cannot restore PUBLIC execution on Mask editing capabilities")
