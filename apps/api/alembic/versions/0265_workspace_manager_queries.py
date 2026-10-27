"""Add the fixed Native Project manager authorization read operation."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0265"
down_revision = "bo0264"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1] / "sql" / "0265_workspace_manager_queries.sql"
)
_PREVIOUS_SQL_PATH = (
    Path(__file__).resolve().parents[1] / "sql" / "0264_workspace_access_queries.sql"
)
_FUNCTION_SQL_START = (
    "CREATE OR REPLACE FUNCTION aap_workspace_project_query_v1.project_query_step("
)


def _execute(statement: str) -> None:
    op.execute(sa.text(statement.replace(":", r"\:")))


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            _execute(statement.strip())


def downgrade() -> None:
    previous_sql = _PREVIOUS_SQL_PATH.read_text(encoding="utf-8")
    if previous_sql.count(_FUNCTION_SQL_START) != 1:
        raise RuntimeError("Frozen Project query function definition is missing")
    statement = previous_sql[previous_sql.index(_FUNCTION_SQL_START) :].strip()
    if not statement.endswith("$function$;"):
        raise RuntimeError("Frozen Project query function definition is incomplete")
    _execute(statement)
