"""Keep saved views and the daily performance cache behind Insights capabilities."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0487"
down_revision = "bo0486"
branch_labels = depends_on = None

_SQL = Path(__file__).resolve().parents[1] / "sql"
_BREAK = "-- aap-statement-break"


def upgrade() -> None:
    for artifact in (
        "0487_insights_perf_refresh.sql",
        "0487_insights_task_views.sql",
        "0487_insights_dataset_views.sql",
        "0487_insights_storage_owner.sql",
    ):
        for statement in (_SQL / artifact).read_text(encoding="utf-8").split(_BREAK):
            op.execute(
                sa.text(statement.replace(":", r"\:")).execution_options(
                    no_parameters=True
                )
            )


def downgrade() -> None:
    raise RuntimeError("Insights private storage cutover is forward-only")
