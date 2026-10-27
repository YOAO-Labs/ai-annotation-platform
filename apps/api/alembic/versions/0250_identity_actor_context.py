"""Add a readonly, admission-bound actor context function."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0250"
down_revision = "bo0249"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_ARTIFACT = "0250_identity_actor_context.sql"
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
        "DROP FUNCTION IF EXISTS aap_identity_v1.read_actor_context(uuid,text) RESTRICT"
    )
