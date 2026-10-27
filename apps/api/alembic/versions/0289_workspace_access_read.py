"""Add a role-free unlocked Project row phase to Workspace access reads."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0289"
down_revision = "bo0288"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1] / "sql" / "0289_workspace_access_read.sql"
)
_BASE_PROJECT_ROW_SQL_PATH = (
    Path(__file__).resolve().parents[1] / "sql" / "0268_workspace_class_rename.sql"
)
_BASE_PROJECT_ACCESS_SQL_PATH = (
    Path(__file__).resolve().parents[1]
    / "sql"
    / "0270_workspace_project_access_step.sql"
)
_PROJECT_ROW_PREFIX = (
    "CREATE OR REPLACE FUNCTION "
    "aap_workspace_configuration_v1._class_rename_project_row("
)
_PROJECT_ACCESS_PREFIX = (
    "CREATE OR REPLACE FUNCTION aap_workspace_v1.project_access_step("
)
_PROJECT_ROW_SIGNATURE = (
    "aap_workspace_configuration_v1._class_rename_project_row(uuid, text)"
)
_PROJECT_ACCESS_SIGNATURE = "aap_workspace_v1.project_access_step(uuid, uuid, text)"


def _execute(statement: str) -> None:
    op.execute(sa.text(statement.replace(":", r"\:")))


def _function_statement(path: Path, prefix: str) -> str:
    source = path.read_text(encoding="utf-8")
    if source.count(prefix) != 1:
        raise RuntimeError(f"Expected one frozen function definition: {prefix}")
    start = source.index(prefix)
    terminator = "$function$;"
    end = source.index(terminator, start)
    return source[start : end + len(terminator)].strip()


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            _execute(statement.strip())


def downgrade() -> None:
    _execute(_function_statement(_BASE_PROJECT_ROW_SQL_PATH, _PROJECT_ROW_PREFIX))
    _execute(f"REVOKE ALL ON FUNCTION {_PROJECT_ROW_SIGNATURE} FROM PUBLIC")
    _execute(_function_statement(_BASE_PROJECT_ACCESS_SQL_PATH, _PROJECT_ACCESS_PREFIX))
    _execute(f"REVOKE ALL ON FUNCTION {_PROJECT_ACCESS_SIGNATURE} FROM PUBLIC")
