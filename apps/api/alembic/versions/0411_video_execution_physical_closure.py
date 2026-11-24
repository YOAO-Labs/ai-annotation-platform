"""Fence release of positively settled video executions."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0411"
down_revision = "bo0410"
branch_labels = depends_on = None

_SQL = Path(__file__).resolve().parents[1] / "sql"


def _execute(statement: str) -> None:
    op.execute(sa.text(statement.replace(":", r"\:")))


def upgrade() -> None:
    for statement in (
        (_SQL / "0411_video_execution_physical_closure.sql")
        .read_text(encoding="utf-8")
        .split("-- aap-statement-break")
    ):
        if statement.strip():
            _execute(statement.strip())


def downgrade() -> None:
    statements = (
        (_SQL / "0409_video_execution_settlement.sql")
        .read_text(encoding="utf-8")
        .split("-- aap-statement-break")
    )
    definition = next(
        statement
        for statement in statements
        if statement.strip().startswith(
            "CREATE OR REPLACE FUNCTION aap_jobs_v1.video_execution_step("
        )
    )
    _execute(definition.strip())
