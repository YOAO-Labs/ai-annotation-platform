"""Add caller-transaction Workspace import and quality-configuration capabilities."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0288"
down_revision = "bo0287"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1]
    / "sql"
    / "0288_workspace_project_maintenance.sql"
)
_FUNCTIONS = (
    "aap_workspace_v1.write_point_cloud_quality_config(uuid,jsonb)",
    "aap_workspace_v1.create_import_project(uuid,text,text,uuid,jsonb)",
    "aap_workspace_v1.read_import_project(text)",
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
