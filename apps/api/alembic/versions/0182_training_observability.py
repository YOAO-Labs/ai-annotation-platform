"""Bounded independent training observations and structured failures.

Revision ID: 0182
Revises: 0181
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "0182"
down_revision = "0181"
branch_labels = depends_on = None


def upgrade():
    op.add_column(
        "training_attempts",
        sa.Column(
            "observation_state",
            pg.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.alter_column("training_attempts", "observation_state", server_default=None)
    op.add_column("training_attempts", sa.Column("failure", pg.JSONB()))
    for table, index in (
        ("training_telemetry_samples", "ix_training_telemetry_retention"),
        ("training_log_chunks", "ix_training_log_retention"),
    ):
        op.create_table(
            table,
            sa.Column(
                "attempt_id",
                pg.UUID(as_uuid=True),
                sa.ForeignKey("training_attempts.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("sequence", sa.Integer(), primary_key=True),
            sa.Column("stream_id", pg.UUID(as_uuid=True), nullable=False),
            sa.Column("producer_boot_id", pg.UUID(as_uuid=True), nullable=False),
            sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column(
                "received_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column("content_digest", sa.String(64), nullable=False),
            sa.Column("payload", pg.JSONB(), nullable=False),
            sa.CheckConstraint("sequence > 0", name=f"ck_{table}_sequence"),
        )
        op.create_index(index, table, ["received_at"])


def downgrade():
    op.drop_table("training_log_chunks")
    op.drop_table("training_telemetry_samples")
    op.drop_column("training_attempts", "failure")
    op.drop_column("training_attempts", "observation_state")
