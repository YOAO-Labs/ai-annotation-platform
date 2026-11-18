"""Cut the task edit-lock advisory domain over to its current key prefix."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0466"
down_revision = "bo0465"
branch_labels = depends_on = None

_SQL = Path(__file__).resolve().parents[1] / "sql"


def upgrade() -> None:
    source = (_SQL / "0466_task_edit_lock_domain.sql").read_text(encoding="utf-8")
    for statement in source.split("-- aap-statement-break"):
        if statement.strip():
            op.execute(sa.text(statement.strip().replace(":", r"\:")))


def downgrade() -> None:
    raise NotImplementedError("Task edit-lock advisory domain cutover is forward-only")
