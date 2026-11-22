"""Bind real fixture video requests to their Assets output and Jobs delivery."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0481"
down_revision = "bo0480"
branch_labels = depends_on = None


def upgrade() -> None:
    sql = (
        Path(__file__).resolve().parents[1] / "sql" / "0481_fixture_video_requests.sql"
    )
    for statement in sql.read_text(encoding="utf-8").split("-- aap-statement-break"):
        op.execute(
            sa.text(statement.replace(":", r"\:")).execution_options(no_parameters=True)
        )


def downgrade() -> None:
    raise RuntimeError("Cannot discard fixture video request ownership evidence")
