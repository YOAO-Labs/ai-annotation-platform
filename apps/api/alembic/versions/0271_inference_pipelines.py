"""Add the fixed NativeSQL owner for named inference pipelines."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0271"
down_revision = "0270"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1] / "sql" / "0271_inference_pipelines.sql"
)
_FUNCTIONS = (
    "pipeline_step(jsonb,text,jsonb,text[],text[])",
    "pipeline_policy()",
    "_pipeline_row_value(public.project_pipelines)",
    "_step_success(uuid,text,text,uuid,uuid,jsonb)",
    "_step_failure(uuid,text,text,uuid,uuid,integer,jsonb,jsonb,boolean)",
    "_query_is_valid(jsonb)",
    "_command_is_valid(jsonb)",
    "_actor_is_valid(jsonb)",
    "_has_exact_keys(jsonb,text[])",
)
_TABLES = (
    "operation_states",
    "operation_attempts",
    "operation_receipts",
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
    bind = op.get_bind()
    counts = {
        table: int(
            bind.execute(
                sa.text(f"SELECT count(*) FROM aap_inference_pipelines_v1.{table}")
            ).scalar_one()
        )
        for table in _TABLES
    }
    if any(counts.values()):
        raise RuntimeError(
            f"Cannot remove Inference pipeline NativeSQL while private state exists: {counts}."
        )

    for signature in _FUNCTIONS:
        _execute(f"DROP FUNCTION aap_inference_pipelines_v1.{signature} RESTRICT")
    for table in _TABLES:
        op.drop_table(table, schema="aap_inference_pipelines_v1")
    _execute(
        "DROP FUNCTION aap_inference_pipelines_v1._guard_unfinished_operation() RESTRICT"
    )
    _execute(
        "DROP FUNCTION aap_inference_pipelines_v1._cleanup_unfinished_state() RESTRICT"
    )
    _execute("DROP SCHEMA aap_inference_pipelines_v1 RESTRICT")
