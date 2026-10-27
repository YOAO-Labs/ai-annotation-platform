"""Add fixed Native Labeling class and attribute usage reads."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0266"
down_revision = "bo0265"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1] / "sql" / "0266_labeling_class_query.sql"
)
_FUNCTIONS = (
    "aap_labeling_class_query_v1.class_query_policy()",
    "aap_labeling_class_query_v1.class_counts(uuid)",
    "aap_labeling_class_query_v1.attribute_rows(uuid)",
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
    _execute("DROP SCHEMA aap_labeling_class_query_v1 RESTRICT")
