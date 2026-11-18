"""Require the old center to settle all relay work before changing Redis keys."""

from alembic import op

revision = "bo0468"
down_revision = "bo0467"
branch_labels = depends_on = None


def upgrade() -> None:
    # Old ingress must already be stopped; a blocked writer is not a drained one.
    op.execute("LOCK TABLE public.site_agent_commands IN EXCLUSIVE MODE NOWAIT")
    op.execute("""
        DO $cutover$
        BEGIN
            IF EXISTS (
                SELECT FROM pg_catalog.pg_stat_activity AS s
                WHERE s.datname = current_database() AND s.pid <> pg_backend_pid()
                  AND s.backend_type = 'client backend' AND s.usesysid IS NOT NULL
                  AND pg_catalog.has_table_privilege(
                      s.usesysid, 'public.site_agent_commands', 'INSERT,UPDATE,DELETE'
                  )
            ) THEN
                RAISE EXCEPTION USING ERRCODE = '55006',
                    MESSAGE = 'Drain site-agent command writers before relay cutover';
            END IF;
            IF EXISTS (
                SELECT FROM public.site_agent_commands
                WHERE state NOT IN ('applied', 'rejected') OR NOT relay_released
                   OR (lease_session_id IS NOT NULL AND closed IS NOT TRUE)
            ) THEN
                RAISE EXCEPTION USING ERRCODE = '55006',
                    MESSAGE = 'Settle site-agent commands and release old relays before cutover';
            END IF;
        END;
        $cutover$
    """)


def downgrade() -> None:
    raise NotImplementedError("Site-agent relay key cutover is forward-only")
