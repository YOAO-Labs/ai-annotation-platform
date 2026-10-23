"""Personal dataset queries with no persisted membership or access grants."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0206"
down_revision = "0205"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "dataset_saved_views",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "dataset_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("datasets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "owner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("query", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "query_schema_version", sa.Integer(), nullable=False, server_default="1"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "dataset_id", "owner_id", "name", name="uq_dataset_saved_views_owner_name"
        ),
    )
    op.create_index(
        "ix_dataset_saved_views_owner_id", "dataset_saved_views", ["owner_id"]
    )


def downgrade():
    op.drop_table("dataset_saved_views")
