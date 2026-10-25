"""Move notification preference reads and writes behind Notifications ownership."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0311"
down_revision = "0310"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1] / "sql" / "0311_notifications_preferences.sql"
)


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            op.execute(sa.text(statement.strip().replace(":", r"\:")))


def downgrade() -> None:
    op.execute("DROP FUNCTION aap_notifications_v1.preference_command(jsonb) RESTRICT")
    op.execute(
        "DROP FUNCTION aap_notifications_v1._preference_jsonb_truthy(jsonb) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_notifications_v1._preference_object_keys_valid("
        "jsonb, text[], integer, integer) RESTRICT"
    )
    op.execute("DROP FUNCTION aap_notifications_v1._preference_policy() RESTRICT")
