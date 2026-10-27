"""Move task edit-lock decisions behind the Labeling owner capability."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0334"
down_revision = "bo0333"
branch_labels = depends_on = None

_SQL_PATH = Path(__file__).resolve().parents[1] / "sql" / "0334_labeling_task_locks.sql"


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            op.execute(sa.text(statement.strip().replace(":", r"\:")))


def downgrade() -> None:
    op.execute("DROP FUNCTION aap_labeling_v1.task_edit_lock_command(jsonb) RESTRICT")
