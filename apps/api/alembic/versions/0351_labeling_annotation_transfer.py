"""Move Labeling import, conversion, camera and format-ledger data to its owner."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0351"
down_revision = "0350"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1]
    / "sql"
    / "0351_labeling_annotation_transfer.sql"
)


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            op.execute(sa.text(statement.strip().replace(":", r"\:")))


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION aap_labeling_v1.annotation_transfer_command(jsonb) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_labeling_v1._annotation_transfer_without_suffix(text) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_labeling_v1._annotation_transfer_camera_bbox_valid(jsonb) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_labeling_v1._annotation_transfer_without_track_ids(jsonb) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_labeling_v1._annotation_transfer_exact_fields(jsonb,text[]) RESTRICT"
    )
