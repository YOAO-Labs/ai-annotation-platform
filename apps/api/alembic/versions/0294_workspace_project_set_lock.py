"""Add an ordered Workspace Project reference set lock capability."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0294"
down_revision = "bo0293"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1] / "sql" / "0294_workspace_project_set_lock.sql"
)
_FUNCTION_SIGNATURE = "aap_workspace_v1.lock_project_references_for_update(uuid[])"


def _execute(statement: str) -> None:
    op.execute(sa.text(statement.replace(":", r"\:")))


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            _execute(statement.strip())


def downgrade() -> None:
    _execute(f"DROP FUNCTION {_FUNCTION_SIGNATURE} RESTRICT")
