"""Publish the Audit daily aggregate and refresh capability."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0322"
down_revision = "0321"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1] / "sql" / "0322_audit_daily_projection.sql"
)


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            op.execute(sa.text(statement.strip().replace(":", r"\:")))


def downgrade() -> None:
    op.execute("DROP FUNCTION aap_audit_v1.refresh_audit_bi_mv() RESTRICT")
    op.execute("DROP VIEW aap_audit_v1.audit_daily_facts RESTRICT")
