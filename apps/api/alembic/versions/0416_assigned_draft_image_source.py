"""Restore assigned, editable draft image source access for Workbench AI."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0416"
down_revision = "bo0415"
branch_labels = depends_on = None


def _apply(filename: str) -> None:
    sql = (Path(__file__).resolve().parents[1] / "sql" / filename).read_text(
        encoding="utf-8"
    )
    op.execute(sa.text(sql.replace(":", r"\:")).execution_options(no_parameters=True))


def upgrade() -> None:
    _apply("0416_assigned_draft_image_source.sql")


def downgrade() -> None:
    _apply("0416_assigned_draft_image_source_down.sql")
