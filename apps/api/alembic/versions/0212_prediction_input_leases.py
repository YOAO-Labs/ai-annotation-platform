"""Retain prediction input cleanup independently from business job lifetime."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0212"
down_revision = "0211"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "prediction_input_leases",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_kind", sa.String(32), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("bucket", sa.String(255), nullable=False),
        sa.Column("storage_identity", sa.String(64), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("uncertain_write", sa.Boolean(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retain_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_cleanup_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_cleanup_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(64)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "state IN ('active', 'cleanup_pending')",
            name="ck_prediction_input_lease_state",
        ),
    )
    op.create_index(
        "ix_prediction_input_cleanup",
        "prediction_input_leases",
        ["next_cleanup_at", "expires_at"],
    )


def downgrade():
    op.execute("LOCK TABLE prediction_input_leases IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM prediction_input_leases)")
    ):
        raise RuntimeError(
            "Cannot discard outstanding prediction input recovery records"
        )
    op.drop_table("prediction_input_leases")
