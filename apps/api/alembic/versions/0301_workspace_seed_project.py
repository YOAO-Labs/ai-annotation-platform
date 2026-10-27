"""Add fixed caller-transaction Workspace Project seed capabilities."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0301"
down_revision = "bo0300"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1] / "sql" / "0301_workspace_seed_project.sql"
)
_FUNCTIONS = (
    "aap_workspace_v1.write_nuscenes_seed_defaults(uuid,text,jsonb)",
    "aap_workspace_v1.create_point_cloud_seed_project(uuid,text,text,uuid,jsonb)",
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
