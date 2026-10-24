"""Keep shared action idempotency locking and lookup behind Jobs ownership."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0332"
down_revision = "0331"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1] / "sql" / "0332_jobs_action_idempotency.sql"
)


def upgrade() -> None:
    for statement in _SQL_PATH.read_text().split("-- aap-statement-break"):
        if statement.strip():
            op.execute(sa.text(statement.strip().replace(":", r"\:")))


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION aap_jobs_v1.find_action_idempotent_jobs(text,uuid,uuid,text) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_jobs_v1.lock_action_idempotency(text,uuid,uuid,text) RESTRICT"
    )
