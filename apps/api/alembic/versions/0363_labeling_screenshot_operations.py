"""Move screenshot-managed Labeling operational writes behind its owner."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0363"
down_revision = "bo0362"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1]
    / "sql"
    / "0363_labeling_screenshot_operations.sql"
)


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            op.execute(sa.text(statement.strip().replace(":", r"\:")))


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION aap_labeling_v1.screenshot_operations_command(jsonb) RESTRICT"
    )
