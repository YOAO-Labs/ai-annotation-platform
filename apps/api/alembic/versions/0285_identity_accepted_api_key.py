"""Add the Identity-owned accepted API key authority check."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0285"
down_revision = "bo0284"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1] / "sql" / "0285_identity_accepted_api_key.sql"
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
    _execute(
        "DROP FUNCTION aap_identity_v1.check_accepted_api_key(uuid,uuid,text[]) RESTRICT"
    )
