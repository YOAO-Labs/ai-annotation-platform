"""Hide retained Job notifications after their authoritative project is deleted."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0475"
down_revision = "bo0474"
branch_labels = depends_on = None


def upgrade() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "sql"
        / "0475_inbox_retained_job_scope.sql"
    ).read_text(encoding="utf-8")
    op.execute(
        sa.text(source.replace(":", r"\:")).execution_options(no_parameters=True)
    )


def downgrade() -> None:
    raise NotImplementedError("Beta inbox scope correction is forward-only")
