"""Add caller-transactional Labeling Project counter deltas."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0299"
down_revision = "bo0298"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1]
    / "sql"
    / "0299_labeling_project_counter_deltas.sql"
)
_FUNCTION_SIGNATURES = (
    "aap_labeling_v1.record_project_review_approval(uuid)",
    "aap_labeling_v1.decrement_project_completed_count(uuid)",
    "aap_labeling_v1.decrement_project_review_count(uuid)",
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
    for signature in _FUNCTION_SIGNATURES:
        _execute(f"DROP FUNCTION {signature} RESTRICT")
