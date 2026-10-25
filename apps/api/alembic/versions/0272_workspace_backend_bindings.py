"""Add the fixed Native Workspace backend-binding cleanup command."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0272"
down_revision = "0271"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1]
    / "sql"
    / "0272_workspace_backend_bindings.sql"
)
_FUNCTION = "aap_workspace_v1.clear_primary_backend_bindings(uuid)"


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
