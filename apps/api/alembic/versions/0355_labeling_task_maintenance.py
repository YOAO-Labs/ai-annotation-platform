"""Move source-less Task maintenance to the Labeling owner."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0355"
down_revision = "0354"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1] / "sql" / "0355_labeling_task_maintenance.sql"
)


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            op.execute(sa.text(statement.strip().replace(":", r"\:")))


def downgrade() -> None:
    op.execute("DROP FUNCTION aap_labeling_v1.task_maintenance_command(jsonb) RESTRICT")
