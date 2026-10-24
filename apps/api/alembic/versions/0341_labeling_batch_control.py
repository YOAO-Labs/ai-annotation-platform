"""Move Batch and inherited Task control writes behind Labeling's owner."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0341"
down_revision = "0340"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1] / "sql" / "0341_labeling_batch_control.sql"
)


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            op.execute(sa.text(statement.strip().replace(":", r"\:")))


def downgrade() -> None:
    op.execute("DROP FUNCTION aap_labeling_v1.batch_control_command(jsonb) RESTRICT")
