"""Add the narrow Labeling-owned project class rename effect."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0267"
down_revision = "bo0266"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1] / "sql" / "0267_labeling_class_rename.sql"
)
_FUNCTION = "aap_labeling_v1.rename_project_annotation_class(uuid,text,text)"


def _execute(statement: str) -> None:
    op.execute(sa.text(statement.replace(":", r"\:")))


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            _execute(statement.strip())


def downgrade() -> None:
    _execute(f"DROP FUNCTION IF EXISTS {_FUNCTION} RESTRICT")
