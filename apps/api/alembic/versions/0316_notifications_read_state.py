"""Move notification read-state writes behind the Notifications owner."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0316"
down_revision = "bo0315"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1] / "sql" / "0316_notifications_read_state.sql"
)


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            op.execute(sa.text(statement.strip().replace(":", r"\:")))


def downgrade() -> None:
    op.execute("DROP FUNCTION aap_notifications_v1.read_state_command(jsonb) RESTRICT")
