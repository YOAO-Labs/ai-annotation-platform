"""Route NotificationService writes through the Notifications owner capability."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0314"
down_revision = "bo0313"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1] / "sql" / "0314_notifications_append.sql"
)


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            op.execute(sa.text(statement.strip().replace(":", r"\:")))


def downgrade() -> None:
    op.execute("DROP FUNCTION aap_notifications_v1.append_notification(jsonb) RESTRICT")
