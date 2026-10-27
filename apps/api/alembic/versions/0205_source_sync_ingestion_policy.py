"""Explicit automatic ingestion policy and IANA display timezone."""

from alembic import op
import sqlalchemy as sa

revision = "0205"
down_revision = "0204"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "dataset_source_sync_schedules",
        sa.Column(
            "new_file_policy", sa.String(30), nullable=False, server_default="manual"
        ),
    )
    op.add_column(
        "dataset_source_sync_schedules",
        sa.Column("timezone", sa.String(64), nullable=False, server_default="UTC"),
    )
    op.create_check_constraint(
        "ck_source_sync_new_file_policy",
        "dataset_source_sync_schedules",
        "new_file_policy IN ('manual','pure_additions')",
    )


def downgrade():
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM dataset_source_sync_schedules WHERE enabled AND new_file_policy != 'manual')"
        )
    ):
        raise RuntimeError("Pause automatic ingestion schedules before rollback")
    op.drop_constraint(
        "ck_source_sync_new_file_policy", "dataset_source_sync_schedules", type_="check"
    )
    op.drop_column("dataset_source_sync_schedules", "timezone")
    op.drop_column("dataset_source_sync_schedules", "new_file_policy")
