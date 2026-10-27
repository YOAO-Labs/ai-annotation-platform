"""Add fixed NativeSQL reads for Workspace Project queries."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0261"
down_revision = "bo0260"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1] / "sql" / "0261_workspace_project_queries.sql"
)
_FUNCTIONS = (
    "aap_workspace_project_query_v1.project_query_step(jsonb, text, jsonb, text, text, text[], text[], uuid, text, timestamptz, timestamptz, bigint, integer)",
    "aap_workspace_project_query_v1.project_query_policy()",
    "aap_workspace_project_query_v1.project_search_scope(uuid, text)",
    "aap_workspace_project_query_v1.project_search_member_ids(uuid, text)",
    "aap_workspace_project_query_v1._project_row_json(jsonb, integer, integer, integer, integer, text)",
    "aap_workspace_project_query_v1._has_exact_keys(jsonb, text[])",
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
    _execute("DROP SCHEMA IF EXISTS aap_workspace_project_query_v1 RESTRICT")
