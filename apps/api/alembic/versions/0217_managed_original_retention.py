"""Keep explicit managed-original retention decisions and declared hold history."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0217"
down_revision = "0216"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ix_managed_retirement_locator_hash",
        "managed_original_retirements",
        [sa.text("md5(bucket)"), sa.text("md5(object_key)")],
    )
    op.create_table(
        "managed_original_retention_holds",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "retirement_id",
            UUID(as_uuid=True),
            sa.ForeignKey("managed_original_retirements.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("reference_code", sa.String(100), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("created_by", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("release_reason", sa.String(500)),
        sa.Column("released_by", UUID(as_uuid=True)),
        sa.Column("released_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "kind IN ('backup', 'migration')", name="ck_managed_hold_kind"
        ),
        sa.CheckConstraint(
            "reference_code ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$'",
            name="ck_managed_hold_reference",
        ),
        sa.CheckConstraint("reason ~ '[^[:space:]]'", name="ck_managed_hold_reason"),
        sa.CheckConstraint(
            "(revision = 1 AND released_at IS NULL AND released_by IS NULL AND release_reason IS NULL) "
            "OR (revision = 2 AND released_at IS NOT NULL AND released_by IS NOT NULL "
            "AND release_reason IS NOT NULL AND release_reason ~ '[^[:space:]]')",
            name="ck_managed_hold_release",
        ),
    )
    op.create_index(
        "ix_managed_hold_anchor",
        "managed_original_retention_holds",
        ["retirement_id", "created_at", "id"],
    )
    op.create_index(
        "uq_managed_hold_active_reference",
        "managed_original_retention_holds",
        ["retirement_id", "kind", "reference_code"],
        unique=True,
        postgresql_where=sa.text("released_at IS NULL"),
    )
    op.execute("""
        CREATE FUNCTION guard_managed_original_hold_history() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF ROW(NEW.id, NEW.retirement_id, NEW.kind, NEW.reference_code, NEW.reason,
                   NEW.created_by, NEW.created_at)
               IS DISTINCT FROM
               ROW(OLD.id, OLD.retirement_id, OLD.kind, OLD.reference_code, OLD.reason,
                   OLD.created_by, OLD.created_at)
               OR (NEW IS DISTINCT FROM OLD AND NOT (OLD.revision = 1 AND NEW.revision = 2)) THEN
                RAISE EXCEPTION 'Managed original hold history is immutable'
                    USING ERRCODE = '23514', CONSTRAINT = 'ck_managed_hold_immutable';
            END IF;
            RETURN NEW;
        END
        $$
    """)
    op.execute("""
        CREATE TRIGGER trg_managed_original_hold_history
        BEFORE UPDATE ON managed_original_retention_holds
        FOR EACH ROW EXECUTE FUNCTION guard_managed_original_hold_history()
    """)


def downgrade():
    op.execute(
        "LOCK TABLE managed_original_retirements, managed_original_retention_holds IN ACCESS EXCLUSIVE MODE"
    )
    if op.get_bind().scalar(
        sa.text("""
        SELECT EXISTS (SELECT 1 FROM managed_original_retention_holds)
            OR EXISTS (SELECT 1 FROM managed_original_retirements
                WHERE policy_revision <> 1 OR retain_until IS NOT NULL)
    """)
    ):
        raise RuntimeError(
            "Managed original retention facts prevent destructive downgrade"
        )
    op.drop_table("managed_original_retention_holds")
    op.execute("DROP FUNCTION guard_managed_original_hold_history()")
    op.drop_index(
        "ix_managed_retirement_locator_hash", table_name="managed_original_retirements"
    )
