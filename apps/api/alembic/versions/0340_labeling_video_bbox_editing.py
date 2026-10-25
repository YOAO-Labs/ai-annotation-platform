"""Move compact video-bbox conversion and composition writes behind Labeling."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0340"
down_revision = "0339"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1] / "sql" / "0340_labeling_video_bbox_editing.sql"
)


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            op.execute(sa.text(statement.strip().replace(":", r"\:")))


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION aap_labeling_v1.video_bbox_editing_command(jsonb) RESTRICT"
    )
