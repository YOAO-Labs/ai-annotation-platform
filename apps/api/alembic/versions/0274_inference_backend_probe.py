"""Add transaction-bound encrypted Backend setup probe subjects."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0274"
down_revision = "bo0273"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1] / "sql" / "0274_inference_backend_probe.sql"
)
_BASE_SQL_PATH = (
    Path(__file__).resolve().parents[1]
    / "sql"
    / "0256_workspace_configuration_support.sql"
)
_WORKSPACE_BACKEND_PREFIX = (
    "CREATE FUNCTION aap_inference_v1.workspace_configuration_backend("
)
_WORKSPACE_BACKEND_STEP = (
    "CREATE OR REPLACE FUNCTION aap_inference_v1.workspace_configuration_backend("
)


def _execute(statement: str) -> None:
    op.execute(sa.text(statement.replace(":", r"\:")))


def _function_statement(path: Path, prefix: str) -> str:
    source = path.read_text(encoding="utf-8")
    if source.count(prefix) != 1:
        raise RuntimeError(f"Expected one frozen function definition: {prefix}")
    start = source.index(prefix)
    terminator = "$function$;"
    end = source.index(terminator, start)
    return source[start : end + len(terminator)].strip()


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            _execute(statement.strip())


def downgrade() -> None:
    original_backend_lookup = _function_statement(
        _BASE_SQL_PATH, _WORKSPACE_BACKEND_PREFIX
    ).replace(_WORKSPACE_BACKEND_PREFIX, _WORKSPACE_BACKEND_STEP, 1)
    _execute(original_backend_lookup)
    _execute(
        "DROP FUNCTION "
        "aap_inference_backend_probe_v1.open_backend_probe_subject(text, uuid) RESTRICT"
    )
    _execute(
        "DROP FUNCTION aap_inference_backend_probe_v1.backend_probe_policy() RESTRICT"
    )
    _execute("DROP TABLE aap_inference_backend_probe_v1.probe_secret RESTRICT")
    _execute("DROP SCHEMA aap_inference_backend_probe_v1 RESTRICT")
