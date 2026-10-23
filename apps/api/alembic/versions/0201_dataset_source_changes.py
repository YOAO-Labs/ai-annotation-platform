"""Retain preflight difference evidence without rebinding historical samples."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = "0201"
down_revision = "0200"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "dataset_registration_runs",
        sa.Column(
            "comparison_complete", sa.Boolean(), nullable=False, server_default="false"
        ),
    )
    op.create_table(
        "dataset_source_changes",
        sa.Column(
            "run_id",
            UUID(),
            sa.ForeignKey("dataset_registration_runs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("path_digest", sa.String(64), primary_key=True),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column(
            "baseline_run_id",
            UUID(),
            sa.ForeignKey("dataset_scan_runs.id", ondelete="RESTRICT"),
        ),
        sa.Column("before_item_id", UUID()),
        sa.Column("before_sha256", sa.String(64)),
        sa.Column("before_observation", JSONB()),
        sa.Column("before_anchor", JSONB()),
        sa.Column("candidate_sha256", sa.String(64)),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("reason", sa.String(60)),
        sa.CheckConstraint(
            "kind IN ('pending','added','unchanged','changed','not_seen','excluded','unreadable')",
            name="ck_dataset_source_change_kind",
        ),
    )
    op.create_index(
        "ix_dataset_source_change_page",
        "dataset_source_changes",
        ["run_id", "kind", "path_digest"],
    )


def downgrade():
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM dataset_source_changes)")
    ):
        raise RuntimeError("Retain source comparison evidence on application rollback")
    op.drop_table("dataset_source_changes")
    op.drop_column("dataset_registration_runs", "comparison_complete")
