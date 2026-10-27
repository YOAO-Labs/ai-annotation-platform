"""Durable periodic inventories, preflights and explicit review gates."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = "0202"
down_revision = "0201"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "dataset_source_sync_schedules",
        sa.Column(
            "source_id",
            UUID(),
            sa.ForeignKey("dataset_sources.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("interval_minutes", sa.Integer(), nullable=False),
        sa.Column(
            "actor_id",
            UUID(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("api_key_id", UUID()),
        sa.Column("key_proof", sa.String(255)),
        sa.Column("snapshot", JSONB(), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column(
            "scan_id",
            UUID(),
            sa.ForeignKey("dataset_scan_runs.id", ondelete="RESTRICT"),
        ),
        sa.Column(
            "registration_id",
            UUID(),
            sa.ForeignKey("dataset_registration_runs.id", ondelete="RESTRICT"),
        ),
        sa.Column("next_run_at", sa.DateTime(timezone=True)),
        sa.Column(
            "next_check_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_checked_at", sa.DateTime(timezone=True)),
        sa.Column("last_started_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.String(60)),
        sa.Column("dispatch_token", UUID()),
        sa.Column("dispatch_expires_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "interval_minutes BETWEEN 15 AND 10080", name="ck_source_sync_interval"
        ),
        sa.CheckConstraint(
            "state IN ('idle','scanning','preflighting','awaiting_review','registering','blocked')",
            name="ck_source_sync_state",
        ),
    )
    op.create_index(
        "ix_source_sync_due",
        "dataset_source_sync_schedules",
        ["enabled", "next_check_at", "source_id"],
    )


def downgrade():
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM dataset_source_sync_schedules)")
    ):
        raise RuntimeError("Retain periodic source schedules on application rollback")
    op.drop_table("dataset_source_sync_schedules")
