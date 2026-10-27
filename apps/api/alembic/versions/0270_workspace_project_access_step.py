"""Add the fixed Workspace Project access fact step."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0270"
down_revision = "bo0269"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1]
    / "sql"
    / "0270_workspace_project_access_step.sql"
)
_FUNCTION = "aap_workspace_v1.project_access_step(uuid,uuid,text)"


def _execute(statement: str) -> None:
    op.execute(sa.text(statement.replace(":", r"\:")))


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            _execute(statement.strip())


def downgrade() -> None:
    _execute(f"DROP FUNCTION {_FUNCTION} RESTRICT")
