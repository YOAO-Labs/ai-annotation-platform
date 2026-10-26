"""Add fixed rendering preparation for the manual filtering fixture."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0367"
down_revision = "0366"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1]
    / "sql"
    / "0367_labeling_filtering_rendering.sql"
)


    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            op.execute(sa.text(statement.strip().replace(":", r"\:")))


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION aap_labeling_v1.prepare_filtering_rendering(jsonb) RESTRICT"
    )
