"""Add fixed owner reads for ML backend deletion dependencies."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0273"
down_revision = "bo0272"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1]
    / "sql"
    / "0273_inference_backend_dependency_reads.sql"
)
_FUNCTIONS = (
    "aap_jobs_v1.count_running_registry_predictions(uuid)",
    "aap_learning_v1.registry_evaluation_reference_exists(uuid)",
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
