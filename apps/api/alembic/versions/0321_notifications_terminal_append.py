"""Install atomic Notifications async-job terminal appends."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0321"
down_revision = "bo0320"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1]
    / "sql"
    / "0321_notifications_terminal_append.sql"
)


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            op.execute(sa.text(statement.strip().replace(":", r"\:")))


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION "
        "aap_notifications_v1.append_job_terminal_notification(jsonb) RESTRICT"
    )
