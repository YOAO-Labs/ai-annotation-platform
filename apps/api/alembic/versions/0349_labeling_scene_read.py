"""Move Labeling scene and SceneTrack reads behind the owner boundary."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0349"
down_revision = "bo0348"
branch_labels = depends_on = None

_SQL_PATH = Path(__file__).resolve().parents[1] / "sql" / "0349_labeling_scene_read.sql"


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            op.execute(sa.text(statement.strip().replace(":", r"\:")))


def downgrade() -> None:
    op.execute("DROP FUNCTION aap_labeling_v1.scene_read_query(jsonb) RESTRICT")
    op.execute(
        "DROP FUNCTION aap_labeling_v1._scene_read_annotation_json(uuid, uuid, uuid, text, text, text, jsonb, text, uuid, text, text, boolean, boolean, boolean, boolean, integer, timestamptz, integer) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_labeling_v1._scene_read_interval_json(uuid, uuid, integer, integer, text, integer) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_labeling_v1._scene_read_track_json(uuid, uuid, uuid, text, text, text, jsonb, jsonb, integer, timestamptz) RESTRICT"
    )
