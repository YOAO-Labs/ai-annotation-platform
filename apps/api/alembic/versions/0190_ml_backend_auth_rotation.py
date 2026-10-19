"""Allow managed backend authentication rotation without changing GPU membership.

Revision ID: 0190
Revises: 0189
"""

from alembic import op
import sqlalchemy as sa


revision = "0190"
down_revision = "0189"
branch_labels = depends_on = None


_ROTATE_AUTH_FUNCTION = """
CREATE FUNCTION rotate_ml_backend_authentication()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.auth_revision IS DISTINCT FROM OLD.auth_revision THEN
        RAISE EXCEPTION 'backend authentication revision is database managed'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'ck_ml_backend_auth_revision_managed';
    END IF;
    IF OLD.auth_method IS DISTINCT FROM NEW.auth_method
       OR OLD.auth_token IS DISTINCT FROM NEW.auth_token THEN
        -- The earlier resource/global barrier trigger keeps lock ordering intact.
        -- Proof commits lock this membership while rechecking their registry
        -- snapshot; hold the same row lock without changing its epoch or state.
        IF OLD.gpu_resource_id IS NOT NULL THEN
            PERFORM 1 FROM gpu_backend_memberships
            WHERE backend_registry_id = OLD.id
              AND gpu_resource_id = OLD.gpu_resource_id
              AND state IN ('pending', 'active')
            FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'current GPU membership is missing'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_gpu_backend_membership_current_missing';
            END IF;
        END IF;
        NEW.auth_revision := OLD.auth_revision + 1;
        NEW.state := 'disconnected';
        NEW.health_meta := NULL;
        NEW.last_checked_at := NULL;
        NEW.error_message := NULL;
    END IF;
    RETURN NEW;
END;
$$;
"""


def upgrade() -> None:
    op.add_column(
        "ml_backend_registry",
        sa.Column("auth_revision", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "ck_ml_backend_auth_revision_nonnegative",
        "ml_backend_registry",
        "auth_revision >= 0",
    )
    op.execute(_ROTATE_AUTH_FUNCTION)
    op.execute(
        """
        CREATE TRIGGER trg_rotate_ml_backend_authentication
        BEFORE UPDATE ON ml_backend_registry
        FOR EACH ROW EXECUTE FUNCTION rotate_ml_backend_authentication()
        """
    )
    op.execute("DROP TRIGGER trg_sync_gpu_backend_membership ON ml_backend_registry")
    op.execute(
        """
        CREATE TRIGGER trg_sync_gpu_backend_membership
        AFTER INSERT OR DELETE ON ml_backend_registry
        FOR EACH ROW EXECUTE FUNCTION sync_gpu_backend_membership()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_sync_gpu_backend_membership_update
        AFTER UPDATE OF gpu_resource_id, vram_budget_mb, eviction_priority,
                        extra_params, url ON ml_backend_registry
        FOR EACH ROW
        WHEN (OLD.gpu_resource_id IS DISTINCT FROM NEW.gpu_resource_id
           OR OLD.vram_budget_mb IS DISTINCT FROM NEW.vram_budget_mb
           OR OLD.eviction_priority IS DISTINCT FROM NEW.eviction_priority
           OR OLD.extra_params IS DISTINCT FROM NEW.extra_params
           OR OLD.url IS DISTINCT FROM NEW.url)
        EXECUTE FUNCTION sync_gpu_backend_membership()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER trg_sync_gpu_backend_membership_update ON ml_backend_registry"
    )
    op.execute("DROP TRIGGER trg_sync_gpu_backend_membership ON ml_backend_registry")
    op.execute(
        """
        CREATE TRIGGER trg_sync_gpu_backend_membership
        AFTER INSERT OR UPDATE OF gpu_resource_id, vram_budget_mb,
            eviction_priority, extra_params, url, auth_method, auth_token
        OR DELETE ON ml_backend_registry
        FOR EACH ROW EXECUTE FUNCTION sync_gpu_backend_membership()
        """
    )
    op.execute(
        "DROP TRIGGER trg_rotate_ml_backend_authentication ON ml_backend_registry"
    )
    op.execute("DROP FUNCTION rotate_ml_backend_authentication()")
    op.drop_constraint(
        "ck_ml_backend_auth_revision_nonnegative", "ml_backend_registry", type_="check"
    )
    op.drop_column("ml_backend_registry", "auth_revision")
