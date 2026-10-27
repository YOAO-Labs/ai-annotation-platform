"""Add caller-transaction Workspace ProjectMember lock capabilities."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0292"
down_revision = "bo0291"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1] / "sql" / "0292_workspace_member_locks.sql"
)
_FUNCTIONS = (
    "aap_workspace_v1.project_member_rows(uuid, uuid[], boolean)",
    "aap_workspace_v1.lock_project_member_reference(uuid, uuid, boolean)",
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
    for signature in _FUNCTIONS:
        _execute(f"DROP FUNCTION {signature} RESTRICT")
