"""Move prediction partition DDL behind Labeling's owner."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0357"
down_revision = "bo0356"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1]
    / "sql"
    / "0357_labeling_prediction_partitions.sql"
)


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            op.execute(sa.text(statement.strip().replace(":", r"\:")))


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION aap_labeling_v1.prediction_partition_command(jsonb) RESTRICT"
    )
