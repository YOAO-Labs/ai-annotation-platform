"""Retain maintenance generations and unresolved physical access receipts."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0215"
down_revision = "0214"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "storage_maintenance_gates",
        sa.Column("location_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("state", sa.String(20), nullable=False, server_default="active"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("changed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "changed_by",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.CheckConstraint(
            "state IN ('active', 'draining', 'stopped')",
            name="ck_storage_maintenance_state",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_storage_maintenance_revision"),
    )
    op.create_table(
        "storage_operations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "location_id",
            UUID(as_uuid=True),
            sa.ForeignKey("storage_maintenance_gates.location_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("purpose", sa.String(64), nullable=False),
        sa.Column("node_id", sa.String(128), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("revision >= 1", name="ck_storage_operation_revision"),
    )
    op.create_index(
        "ix_storage_operations_location_started",
        "storage_operations",
        ["location_id", "started_at", "id"],
    )
    op.execute(
        sa.text(
            "INSERT INTO storage_maintenance_gates (location_id) SELECT id FROM storage_locations WHERE kind = 'filesystem' OR connection_id IS NOT NULL"
        )
    )


def downgrade():
    changed = op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM storage_operations) OR EXISTS "
            "(SELECT 1 FROM storage_maintenance_gates WHERE state <> 'active' "
            "OR revision <> 1 OR changed_at IS NOT NULL OR changed_by IS NOT NULL)"
        )
    )
    if changed:
        raise RuntimeError(
            "Storage maintenance history or unresolved operations prevent downgrade"
        )
    op.drop_index(
        "ix_storage_operations_location_started", table_name="storage_operations"
    )
    op.drop_table("storage_operations")
    op.drop_table("storage_maintenance_gates")
