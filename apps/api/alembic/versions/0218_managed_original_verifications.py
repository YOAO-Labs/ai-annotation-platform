"""Preserve bounded managed-original observations and unresolved physical responsibility."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0218"
down_revision = "0217"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "managed_original_verifications",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "retirement_id",
            UUID(as_uuid=True),
            sa.ForeignKey("managed_original_retirements.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "async_job_id",
            UUID(as_uuid=True),
            sa.ForeignKey("async_jobs.id", ondelete="SET NULL"),
            unique=True,
        ),
        sa.Column("requested_by", UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", sa.String(255), nullable=False, unique=True),
        sa.Column("authority", JSONB, nullable=False),
        sa.Column("anchor", JSONB, nullable=False),
        sa.Column("configuration", JSONB, nullable=False),
        sa.Column("input_fingerprint", sa.String(64), nullable=False),
        sa.Column("expected_sha256", sa.String(64), nullable=False),
        sa.Column("expected_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("maintenance_revision", sa.Integer(), nullable=False),
        sa.Column("operation_id", UUID(as_uuid=True), unique=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("outcome", sa.String(20)),
        sa.Column("error_code", sa.String(80)),
        sa.Column("observed_sha256", sa.String(64)),
        sa.Column("observed_size_bytes", sa.BigInteger()),
        sa.Column("observation", JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('pending','running','completed','failed','cancelled')",
            name="ck_managed_verification_status",
        ),
        sa.CheckConstraint(
            "expected_sha256 ~ '^[0-9a-f]{64}$' AND expected_size_bytes >= 0 AND maintenance_revision >= 1",
            name="ck_managed_verification_expected",
        ),
        sa.CheckConstraint(
            "input_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_managed_verification_fingerprint",
        ),
        sa.CheckConstraint(
            "observed_sha256 IS NULL OR observed_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_managed_verification_observed_sha",
        ),
        sa.CheckConstraint(
            "observed_size_bytes IS NULL OR observed_size_bytes >= 0",
            name="ck_managed_verification_observed_size",
        ),
        sa.CheckConstraint(
            "error_code IS NULL OR error_code ~ '^[a-z][a-z0-9_]{0,79}$'",
            name="ck_managed_verification_error",
        ),
        sa.CheckConstraint(
            "(status='pending' AND started_at IS NULL AND operation_id IS NULL AND completed_at IS NULL AND outcome IS NULL) OR (status='running' AND started_at IS NOT NULL AND completed_at IS NULL AND outcome IS NULL) OR (status='completed' AND started_at IS NOT NULL AND operation_id IS NOT NULL AND completed_at IS NOT NULL AND outcome IN ('matched','missing','changed','unverifiable')) OR (status='failed' AND completed_at IS NOT NULL AND outcome='failed' AND error_code IS NOT NULL) OR (status='cancelled' AND completed_at IS NOT NULL AND outcome='cancelled' AND cancel_requested_at IS NOT NULL)",
            name="ck_managed_verification_lifecycle",
        ),
        sa.CheckConstraint(
            "(status IN ('pending','running') AND outcome IS NULL AND error_code IS NULL AND observed_sha256 IS NULL AND observed_size_bytes IS NULL AND observation='{}'::jsonb) OR (status NOT IN ('pending','running') AND outcome IS NOT NULL)",
            name="ck_managed_verification_terminal_facts",
        ),
        sa.CheckConstraint(
            "outcome IS DISTINCT FROM 'matched' OR (observed_sha256 IS NOT NULL AND observed_size_bytes IS NOT NULL AND observed_sha256=expected_sha256 AND observed_size_bytes=expected_size_bytes AND error_code IS NULL)",
            name="ck_managed_verification_match",
        ),
    )
    op.create_index(
        "ix_managed_verification_history",
        "managed_original_verifications",
        ["retirement_id", "requested_at", "id"],
    )
    op.create_index(
        "uq_managed_verification_active",
        "managed_original_verifications",
        ["retirement_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending','running')"),
    )
    op.execute("""
        CREATE FUNCTION guard_managed_original_verification_history() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'Managed original verification history is immutable'
                    USING ERRCODE = '23514', CONSTRAINT = 'ck_managed_verification_immutable';
            END IF;
            IF ROW(NEW.id, NEW.retirement_id, NEW.requested_by, NEW.task_id,
                   NEW.authority, NEW.anchor, NEW.configuration, NEW.input_fingerprint,
                   NEW.expected_sha256, NEW.expected_size_bytes, NEW.maintenance_revision,
                   NEW.requested_at)
               IS DISTINCT FROM
               ROW(OLD.id, OLD.retirement_id, OLD.requested_by, OLD.task_id,
                   OLD.authority, OLD.anchor, OLD.configuration, OLD.input_fingerprint,
                   OLD.expected_sha256, OLD.expected_size_bytes, OLD.maintenance_revision,
                   OLD.requested_at)
               OR (NEW.async_job_id IS DISTINCT FROM OLD.async_job_id AND NEW.async_job_id IS NOT NULL)
               OR (OLD.started_at IS NOT NULL AND NEW.started_at IS DISTINCT FROM OLD.started_at)
               OR (OLD.operation_id IS NOT NULL AND NEW.operation_id IS DISTINCT FROM OLD.operation_id)
               OR (OLD.cancel_requested_at IS NOT NULL AND NEW.cancel_requested_at IS DISTINCT FROM OLD.cancel_requested_at)
               OR (OLD.status NOT IN ('pending','running') AND
                   (to_jsonb(NEW) - 'async_job_id') IS DISTINCT FROM (to_jsonb(OLD) - 'async_job_id'))
               OR (OLD.status='running' AND NEW.status='pending') THEN
                RAISE EXCEPTION 'Managed original verification history is immutable'
                    USING ERRCODE = '23514', CONSTRAINT = 'ck_managed_verification_immutable';
            END IF;
            RETURN NEW;
        END
        $$
    """)
    op.execute("""
        CREATE TRIGGER trg_managed_original_verification_history
        BEFORE UPDATE OR DELETE ON managed_original_verifications
        FOR EACH ROW EXECUTE FUNCTION guard_managed_original_verification_history()
    """)


def downgrade():
    op.execute("LOCK TABLE managed_original_verifications IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM managed_original_verifications)")
    ):
        raise RuntimeError(
            "Managed original verification facts prevent destructive downgrade"
        )
    op.drop_table("managed_original_verifications")
    op.execute("DROP FUNCTION guard_managed_original_verification_history()")
