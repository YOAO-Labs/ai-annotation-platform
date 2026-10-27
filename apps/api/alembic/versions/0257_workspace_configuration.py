"""Move project and template configuration writes behind the Workspace owner."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0257"
down_revision = "bo0256"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1] / "sql" / "0257_workspace_configuration.sql"
)
_FUNCTIONS = (
    "configuration_step(jsonb,text,jsonb,text[],text[])",
    "configuration_policy()",
    "_step_success(jsonb,text,jsonb)",
    "_step_failure(jsonb,text,integer,jsonb,jsonb,boolean)",
    "_configuration_template_visible(jsonb,jsonb)",
    "_configuration_template_snapshot(uuid,boolean)",
    "_configuration_project_access(jsonb,jsonb,boolean)",
    "_configuration_project_snapshot(uuid,text)",
    "_configuration_actor(uuid,boolean,text)",
    "_has_exact_keys(jsonb,text[])",
    "_guard_unfinished_attempt()",
    "_cleanup_unwritten_state()",
)
_TABLES = (
    "workspace_configuration_operation_states",
    "workspace_configuration_operation_attempts",
    "workspace_configuration_operation_receipts",
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
    bind = op.get_bind()
    counts = {
        table: int(
            bind.execute(sa.text(f"SELECT count(*) FROM public.{table}")).scalar_one()
        )
        for table in _TABLES
    }
    if any(counts.values()):
        raise RuntimeError(
            f"Cannot remove Workspace configuration while private state exists: {counts}."
        )
    _execute(
        "DROP FUNCTION aap_workspace_configuration_v1.configuration_step(jsonb,text,jsonb,text[],text[]) RESTRICT"
    )
    for table in _TABLES:
        op.drop_table(table, schema="public")
    for signature in _FUNCTIONS[1:]:
        _execute(f"DROP FUNCTION aap_workspace_configuration_v1.{signature} RESTRICT")
    _execute("DROP SCHEMA aap_workspace_configuration_v1 RESTRICT")
