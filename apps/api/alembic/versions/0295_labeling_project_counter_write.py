"""Add a caller-transactional Labeling Project counter writer."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0295"
down_revision = "bo0294"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1]
    / "sql"
    / "0295_labeling_project_counter_write.sql"
)


def _execute(statement: str) -> None:
    op.execute(sa.text(statement.replace(":", r"\:")))


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            _execute(statement.strip())


def downgrade() -> None:
    _execute(
        "DROP FUNCTION "
        "aap_labeling_v1.write_project_counter_projection(uuid, jsonb) RESTRICT"
    )
