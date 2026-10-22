"""Register controlled locations without copying connector credentials."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "0195"
down_revision = "0194"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "storage_locations", sa.Column("connection_id", pg.UUID(as_uuid=True))
    )
    op.add_column(
        "storage_locations",
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
    )
    op.create_foreign_key(
        "fk_storage_location_connection",
        "storage_locations",
        "storage_connections",
        ["connection_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_storage_location_connection", "storage_locations", ["connection_id"]
    )
    op.create_table(
        "storage_location_probes",
        sa.Column(
            "location_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("storage_locations.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("node_id", sa.String(255), primary_key=True),
        sa.Column("config_revision", sa.Integer(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("result", pg.JSONB(), nullable=False),
    )
    op.execute("""
        INSERT INTO storage_locations (id, name, kind, config_revision, config, connection_id)
        SELECT md5('aap:storage-connection:' || id::text)::uuid,
               'connector-' || id::text, kind, 1, '{}'::jsonb, id
        FROM storage_connections
    """)


def downgrade():
    used = (
        op.get_bind()
        .execute(
            sa.text("""
        SELECT EXISTS (
            SELECT 1 FROM dataset_asset_locations a JOIN storage_locations l ON l.id = a.location_id
            WHERE l.kind = 'filesystem' OR l.connection_id IS NOT NULL
        ) OR EXISTS (
            SELECT 1 FROM storage_bindings b JOIN storage_locations l ON l.id = b.location_id
            WHERE l.kind = 'filesystem' OR l.connection_id IS NOT NULL
        )
    """)
        )
        .scalar()
    )
    if used:
        raise RuntimeError(
            "Retain referenced storage locations on application rollback"
        )
    op.drop_table("storage_location_probes")
    op.execute(
        "DELETE FROM storage_locations WHERE kind = 'filesystem' OR connection_id IS NOT NULL"
    )
    op.drop_constraint(
        "uq_storage_location_connection", "storage_locations", type_="unique"
    )
    op.drop_constraint(
        "fk_storage_location_connection", "storage_locations", type_="foreignkey"
    )
    op.drop_column("storage_locations", "enabled")
    op.drop_column("storage_locations", "connection_id")
