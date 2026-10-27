"""Retain explicit storage Job progress observations without historical guesses."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0259"
down_revision = "0258"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "async_jobs",
        sa.Column("last_progress_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "async_jobs",
        sa.Column("progress_observation", postgresql.JSONB(), nullable=True),
    )


def downgrade():
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM async_jobs WHERE last_progress_at IS NOT NULL OR progress_observation IS NOT NULL)"
        )
    ):
        raise RuntimeError(
            "Storage progress facts are retained; keep schema for application rollback"
        )
    op.drop_column("async_jobs", "progress_observation")
    op.drop_column("async_jobs", "last_progress_at")
