"""Add the Identity-owned registration status read capability."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0287"
down_revision = "bo0286"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1]
    / "sql"
    / "0287_identity_registration_status.sql"
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
        "DROP FUNCTION aap_identity_recovery_v1.read_registration_status(jsonb) RESTRICT"
    )
