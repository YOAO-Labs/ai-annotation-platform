"""Private bounded reports for fixed configured-source inventory tasks."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0255"
down_revision = "0254"
branch_labels = None
depends_on = None


CHECKS = (
    (
        "status IN ('pending','running','completed','failed','cancelled')",
        "ck_migration_inventory_status",
    ),
    (
        "input_fingerprint ~ '^[0-9a-f]{64}$' AND maintenance_revision >= 1",
        "ck_migration_inventory_input",
    ),
    (
        "error_code IS NULL OR error_code ~ '^[a-z][a-z0-9_]{0,79}$'",
        "ck_migration_inventory_error",
    ),
    (
        "(status='pending' AND started_at IS NULL AND completed_at IS NULL AND operation_id IS NULL) OR (status='running' AND started_at IS NOT NULL AND completed_at IS NULL) OR (status IN ('completed','failed','cancelled') AND completed_at IS NOT NULL)",
        "ck_migration_inventory_lifecycle",
    ),
    (
        "(report IS NULL AND report_sha256 IS NULL AND report_size_bytes IS NULL AND complete IS NULL AND counts IS NULL) OR (report IS NOT NULL AND status IN ('completed','failed') AND complete IS NOT NULL AND counts IS NOT NULL AND report_sha256 ~ '^[0-9a-f]{64}$' AND report_size_bytes=octet_length(report) AND report_size_bytes BETWEEN 1 AND 33554432)",
        "ck_migration_inventory_report",
    ),
    (
        "(status='completed' AND complete IS TRUE AND error_code IS NULL) OR (status='failed' AND error_code IS NOT NULL) OR (status='cancelled' AND cancel_requested_at IS NOT NULL AND report IS NULL) OR status IN ('pending','running')",
        "ck_migration_inventory_outcome",
    ),
)


def upgrade():
    op.create_table(
        "storage_migration_inventories",
        sa.Column(
            "async_job_id",
            sa.UUID(),
            sa.ForeignKey("async_jobs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("requested_by", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.String(64), nullable=False, unique=True),
        sa.Column("source_location_id", sa.UUID(), nullable=False),
        *(
            sa.Column(name, postgresql.JSONB(), nullable=False)
            for name in ("source", "limits", "handling", "authority")
        ),
        sa.Column("input_fingerprint", sa.String(64), nullable=False),
        sa.Column("maintenance_revision", sa.Integer(), nullable=False),
        sa.Column("operation_id", sa.UUID(), unique=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        *(
            sa.Column(name, sa.DateTime(timezone=True))
            for name in ("started_at", "completed_at", "cancel_requested_at")
        ),
        sa.Column("error_code", sa.String(80)),
        sa.Column("complete", sa.Boolean()),
        sa.Column("counts", postgresql.JSONB()),
        sa.Column("report", sa.LargeBinary()),
        sa.Column("report_sha256", sa.String(64)),
        sa.Column("report_size_bytes", sa.Integer()),
        *(sa.CheckConstraint(sql, name=name) for sql, name in CHECKS),
    )
    op.create_index(
        "ix_migration_inventory_history",
        "storage_migration_inventories",
        ["created_at", "async_job_id"],
    )
    op.create_index(
        "uq_migration_inventory_active_source",
        "storage_migration_inventories",
        ["source_location_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending','running')"),
    )


def downgrade():
    op.drop_table("storage_migration_inventories")
