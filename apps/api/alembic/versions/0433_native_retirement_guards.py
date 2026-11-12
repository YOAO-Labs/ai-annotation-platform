"""Preserve scalar GPU configuration and require physical video retirement."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0433"
down_revision = "bo0432"
branch_labels = depends_on = None

_SQL = Path(__file__).resolve().parents[1] / "sql"


def upgrade() -> None:
    for name in (
        "0433_gpu_membership_scalar_guard.sql",
        "0433_project_video_retirement.sql",
    ):
        source = (_SQL / name).read_text(encoding="utf-8")
        op.execute(
            sa.text(source.replace(":", r"\:")).execution_options(no_parameters=True)
        )


def downgrade() -> None:
    raise RuntimeError("Cannot restore unsafe native retirement guards")
