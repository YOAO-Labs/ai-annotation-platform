"""Add the immutable Jobs queue-dispatch ledger."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0368"
down_revision = "bo0367"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1] / "sql" / "0368_jobs_dispatch_ledger.sql"
)


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            op.execute(sa.text(statement.strip().replace(":", r"\:")))


def downgrade() -> None:
    op.execute("DROP FUNCTION aap_jobs_v1.dispatch_status(uuid) RESTRICT")
    op.execute("DROP FUNCTION aap_jobs_v1.finish_dispatch(jsonb) RESTRICT")
    op.execute("DROP FUNCTION aap_jobs_v1.claim_dispatches(jsonb) RESTRICT")
    op.execute("DROP FUNCTION aap_jobs_v1.prepare_dispatch(jsonb) RESTRICT")
    op.execute("DROP TABLE aap_jobs_v1.dispatch_intents RESTRICT")
