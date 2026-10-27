"""Move candidate and secondary-inference Annotation writes to their owner."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0345"
down_revision = "bo0344"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1]
    / "sql"
    / "0345_labeling_annotation_prediction.sql"
)


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            op.execute(sa.text(statement.strip().replace(":", r"\:")))


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION aap_labeling_v1.annotation_prediction_command(jsonb) RESTRICT"
    )
