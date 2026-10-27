"""Add caller-transaction Identity account row locks."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0251"
down_revision = "bo0250"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_ARTIFACT = "0251_identity_account_locks.sql"
_STATEMENT_SEPARATOR = "-- aap-statement-break"
_FUNCTIONS = (
    "aap_identity_v1.lock_accounts_key_share(uuid[],boolean)",
    "aap_identity_v1.lock_accounts_update(uuid[],boolean)",
    "aap_identity_v1.lock_accounts_share(uuid[],boolean)",
)


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
    for signature in _FUNCTIONS:
        _execute(f"DROP FUNCTION IF EXISTS {signature} RESTRICT")
