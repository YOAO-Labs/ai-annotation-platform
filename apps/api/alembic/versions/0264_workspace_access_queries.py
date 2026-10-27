"""Add the fixed Native Project access-row read to the existing query RPC."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0264"
down_revision = "bo0263"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1] / "sql" / "0264_workspace_access_queries.sql"
)
_PREVIOUS_SQL_PATH = (
    Path(__file__).resolve().parents[1]
    / "sql"
    / "0262_workspace_member_authority_temporals.sql"
)
_STEP_CREATE = "CREATE FUNCTION aap_workspace_project_query_v1.project_query_step("


def _execute(statement: str) -> None:
    op.execute(sa.text(statement.replace(":", r"\:")))


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            _execute(statement.strip())


def downgrade() -> None:
    statements = [
        statement.strip()
        for statement in _PREVIOUS_SQL_PATH.read_text(encoding="utf-8").split(
            "-- aap-statement-break"
        )
        if statement.strip().startswith(_STEP_CREATE)
    ]
    if len(statements) != 1:
        raise RuntimeError("Frozen Project query wrapper definition is missing")
    _execute(statements[0].replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1))
