"""Retain dataset contents while versioning reversible archive transitions."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0214"
down_revision = "0213"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("datasets", sa.Column("archived_at", sa.DateTime(timezone=True)))
    op.add_column("datasets", sa.Column("archived_by", UUID(as_uuid=True)))
    op.add_column(
        "datasets",
        sa.Column(
            "lifecycle_revision", sa.Integer(), nullable=False, server_default="1"
        ),
    )
    op.create_foreign_key(
        "fk_datasets_archived_by",
        "datasets",
        "users",
        ["archived_by"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_datasets_lifecycle_revision", "datasets", "lifecycle_revision >= 1"
    )
    op.create_index("ix_datasets_archived_at", "datasets", ["archived_at"])


def downgrade():
    changed = op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM datasets "
            "WHERE archived_at IS NOT NULL OR lifecycle_revision <> 1)"
        )
    )
    if changed:
        raise RuntimeError("Dataset lifecycle history prevents destructive downgrade")
    op.drop_index("ix_datasets_archived_at", table_name="datasets")
    op.drop_constraint("ck_datasets_lifecycle_revision", "datasets", type_="check")
    op.drop_constraint("fk_datasets_archived_by", "datasets", type_="foreignkey")
    op.drop_column("datasets", "lifecycle_revision")
    op.drop_column("datasets", "archived_by")
    op.drop_column("datasets", "archived_at")
