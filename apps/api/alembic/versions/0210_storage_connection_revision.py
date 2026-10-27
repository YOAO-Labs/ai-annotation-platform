"""Fence connection edits independently from location capability evidence."""

from alembic import op
import sqlalchemy as sa

revision = "0210"
down_revision = "0209"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "storage_connections",
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade():
    op.drop_column("storage_connections", "revision")
