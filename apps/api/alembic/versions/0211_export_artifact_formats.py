"""Preserve the complete target list of multi-format export artifacts."""

from alembic import op
import sqlalchemy as sa

revision = "0211"
down_revision = "0210"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column(
        "export_artifacts",
        "format",
        existing_type=sa.String(20),
        type_=sa.Text(),
        existing_nullable=False,
    )


def downgrade():
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM export_artifacts WHERE char_length(format) > 20)"
        )
    ):
        raise RuntimeError(
            "Cannot narrow export formats while multi-target artifacts exist"
        )
    op.alter_column(
        "export_artifacts",
        "format",
        existing_type=sa.Text(),
        type_=sa.String(20),
        existing_nullable=False,
    )
