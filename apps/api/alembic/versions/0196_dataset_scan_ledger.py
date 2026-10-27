"""Persist resumable read-only directory inventories."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "0196"
down_revision = "0195"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "dataset_sources",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "dataset_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("datasets.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "location_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("storage_locations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("config", pg.JSONB(), nullable=False),
        sa.Column("config_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("root_identity", pg.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_dataset_sources_dataset_id", "dataset_sources", ["dataset_id"])
    op.create_index(
        "ix_dataset_sources_location_id", "dataset_sources", ["location_id"]
    )
    op.create_table(
        "dataset_scan_runs",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "source_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("dataset_sources.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "async_job_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("async_jobs.id", ondelete="SET NULL"),
            unique=True,
        ),
        sa.Column("snapshot", pg.JSONB(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("phase", sa.String(30), nullable=False, server_default="enumerating"),
        sa.Column("complete", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_token", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(60), nullable=True),
        sa.Column("counts", pg.JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending','running','completed','failed','cancelled')",
            name="ck_dataset_scan_status",
        ),
    )
    op.create_index(
        "ix_dataset_scan_runs_source_id", "dataset_scan_runs", ["source_id"]
    )
    op.create_index(
        "uq_dataset_source_active_scan",
        "dataset_scan_runs",
        ["source_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending','running')"),
    )
    op.create_table(
        "dataset_scan_entries",
        sa.Column(
            "run_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("dataset_scan_runs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("path_digest", sa.String(64), primary_key=True),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("observed", pg.JSONB(), nullable=False),
        sa.Column("directory_digest", sa.String(64), nullable=True),
        sa.Column(
            "validated_generation", sa.Integer(), nullable=False, server_default="0"
        ),
    )
    op.create_index(
        "ix_dataset_scan_work",
        "dataset_scan_entries",
        ["run_id", "kind", "state", "validated_generation"],
    )


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM dataset_sources)")):
        raise RuntimeError(
            "Retain dataset source and scan history on application rollback"
        )
    op.drop_table("dataset_scan_entries")
    op.drop_table("dataset_scan_runs")
    op.drop_table("dataset_sources")
