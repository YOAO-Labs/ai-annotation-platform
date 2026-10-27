"""Route ordinary Audit appends through the same-transaction owner function."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0315"
down_revision = "bo0314"
branch_labels = depends_on = None

_SQL_PATH = Path(__file__).resolve().parents[1] / "sql" / "0315_audit_append.sql"


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            op.execute(sa.text(statement.strip().replace(":", r"\:")))


def downgrade() -> None:
    op.execute("DROP FUNCTION aap_audit_v1.append_events(jsonb) RESTRICT")
