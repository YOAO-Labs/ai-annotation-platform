"""Move point-cloud and video-track quality data behind Labeling."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0354"
down_revision = "0353"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1] / "sql" / "0354_labeling_spatial_quality.sql"
)


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            op.execute(sa.text(statement.strip().replace(":", r"\:")))


def downgrade() -> None:
    op.execute("DROP FUNCTION aap_labeling_v1.spatial_quality_command(jsonb) RESTRICT")
