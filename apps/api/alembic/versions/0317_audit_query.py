"""Route super-admin Audit list and export through the fixed owner query."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0317"
down_revision = "0316"
branch_labels = depends_on = None

_SQL_PATH = Path(__file__).resolve().parents[1] / "sql" / "0317_audit_query.sql"


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            op.execute(sa.text(statement.strip().replace(":", r"\:")))


def downgrade() -> None:
    op.execute("DROP FUNCTION aap_audit_v1.query_events(pg_catalog.jsonb) RESTRICT")
