"""Move ProjectTemplate reads and projections behind the Workspace owner."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0259"
down_revision = "bo0258"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1] / "sql" / "0259_workspace_template_queries.sql"
)
_FUNCTIONS = (
    "aap_workspace_template_query_v1.template_query_step(jsonb,text,jsonb,text,text[],text)",
    "aap_workspace_template_query_v1.template_query_policy()",
    "aap_workspace_template_query_v1._template_row_json(public.project_templates)",
    "aap_workspace_template_query_v1._has_exact_keys(jsonb,text[])",
)


def _execute(statement):
    op.execute(sa.text(statement.replace(":", r"\:")))


def upgrade():
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            _execute(statement.strip())


def downgrade():
    for signature in _FUNCTIONS:
        _execute(f"DROP FUNCTION IF EXISTS {signature} RESTRICT")
    _execute("DROP SCHEMA aap_workspace_template_query_v1 RESTRICT")
