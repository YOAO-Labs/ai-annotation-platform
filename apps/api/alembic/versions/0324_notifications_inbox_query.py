"""Move the REST Notifications inbox reads behind fixed owner capabilities."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0324"
down_revision = "bo0323"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1] / "sql" / "0324_notifications_inbox_query.sql"
)


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            op.execute(sa.text(statement.strip().replace(":", r"\:")))


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION aap_notifications_v1.inbox_unread_count(uuid, text) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_notifications_v1.inbox_total_count(uuid, text) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION "
        "aap_notifications_v1.inbox_page(uuid, text, boolean, integer, integer) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_notifications_v1._visible_inbox_rows(uuid, text) RESTRICT"
    )
