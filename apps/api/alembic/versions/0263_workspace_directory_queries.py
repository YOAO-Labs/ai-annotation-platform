"""Add Native SQL reads for Workspace project directory responses."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0263"
down_revision = "bo0262"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1] / "sql" / "0263_workspace_directory_queries.sql"
)
_FUNCTIONS = (
    "aap_workspace_directory_query_v1.directory_query_step(jsonb, text, jsonb)",
    "aap_workspace_directory_query_v1.directory_query_policy()",
    "aap_workspace_directory_query_v1._has_exact_keys(jsonb, text[])",
    "aap_identity_v1.workspace_directory_superadmins()",
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
        _execute(f"DROP FUNCTION IF EXISTS {signature} RESTRICT")
    _execute("DROP SCHEMA IF EXISTS aap_workspace_directory_query_v1 RESTRICT")
