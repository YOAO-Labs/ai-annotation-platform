"""Add native read-only Raster Mask GC preview inspection."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0234"
down_revision = "bo0233"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_ARTIFACT = "0234_mask_gc_preview.sql"
_STATEMENT_SEPARATOR = "-- aap-statement-break"


def _statements() -> list[str]:
    return [
        statement.strip()
        for statement in (_SQL_DIR / _ARTIFACT)
        .read_text(encoding="utf-8")
        .split(_STATEMENT_SEPARATOR)
        if statement.strip()
    ]


def _execute(statement: str) -> None:
    op.execute(sa.text(statement.replace(":", r"\:")))


def upgrade() -> None:
    for statement in _statements():
        _execute(statement)


def downgrade() -> None:
    _execute(
        "DROP FUNCTION IF EXISTS aap_labeling_v1.inspect_mask_gc_page(jsonb) RESTRICT"
    )
