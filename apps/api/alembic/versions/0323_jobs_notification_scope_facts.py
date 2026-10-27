"""Publish the Jobs-owned notification scope fact relation."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0323"
down_revision = "bo0322"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1]
    / "sql"
    / "0323_jobs_notification_scope_facts.sql"
)


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            op.execute(sa.text(statement.strip().replace(":", r"\:")))


def downgrade() -> None:
    op.execute("DROP VIEW aap_jobs_v1.notification_scope_facts RESTRICT")
