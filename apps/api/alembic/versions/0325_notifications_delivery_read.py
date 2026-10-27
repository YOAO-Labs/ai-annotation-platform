"""Install Notifications delivery read and scope capabilities."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0325"
down_revision = "bo0324"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1] / "sql" / "0325_notifications_delivery_read.sql"
)


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            op.execute(sa.text(statement.strip().replace(":", r"\:")))


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION aap_notifications_v1.delivery_super_pairs(jsonb) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_notifications_v1.delivery_owner_pairs(jsonb) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_notifications_v1.delivery_reviewer_pairs(jsonb) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_notifications_v1.delivery_member_pairs(jsonb) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_notifications_v1.delivery_job_scopes(uuid[]) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION "
        "aap_notifications_v1.find_terminal_notification(uuid, text, uuid) RESTRICT"
    )
    op.execute("DROP FUNCTION aap_notifications_v1.read_notification(uuid) RESTRICT")
