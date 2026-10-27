"""Avoid fence DELETE privilege checks when a GPU fence is absent."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op


revision = "bo0306"
down_revision = "bo0305"
branch_labels = None
depends_on = None

_SQL = Path(__file__).resolve().parents[1] / "sql"


def _execute(filename: str) -> None:
    statement = (_SQL / filename).read_text(encoding="utf-8")
    op.execute(sa.text(statement.replace(":", r"\:")))


def upgrade() -> None:
    _execute("0306_gpu_registry_delete_noop.sql")


def downgrade() -> None:
    _execute("0306_gpu_registry_delete_noop_rollback.sql")
