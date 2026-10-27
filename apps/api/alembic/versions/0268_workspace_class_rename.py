"""Add stateless Native actions for project class renames."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0268"
down_revision = "bo0267"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1] / "sql" / "0268_workspace_class_rename.sql"
)
_BASE_SQL_PATH = (
    Path(__file__).resolve().parents[1] / "sql" / "0257_workspace_configuration.sql"
)
_STEP_PREFIX = "CREATE FUNCTION aap_workspace_configuration_v1.configuration_step("
_PROJECT_ROW = "aap_workspace_configuration_v1._class_rename_project_row(uuid,text)"
_MEMBER_ROWS = "aap_workspace_configuration_v1._class_rename_member_rows(uuid,uuid)"


def _execute(statement: str) -> None:
    op.execute(sa.text(statement.replace(":", r"\:")))


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            _execute(statement.strip())


def downgrade() -> None:
    base_statements = _BASE_SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    )
    step_statements = [
        statement.strip()
        for statement in base_statements
        if statement.lstrip().startswith(_STEP_PREFIX)
    ]
    if len(step_statements) != 1:
        raise RuntimeError(
            "Expected exactly one frozen Workspace configuration_step body."
        )

    original_step = step_statements[0].replace(
        _STEP_PREFIX,
        "CREATE OR REPLACE FUNCTION aap_workspace_configuration_v1.configuration_step(",
        1,
    )
    _execute(original_step)
    _execute(f"DROP FUNCTION {_PROJECT_ROW} RESTRICT")
    _execute(f"DROP FUNCTION {_MEMBER_ROWS} RESTRICT")
