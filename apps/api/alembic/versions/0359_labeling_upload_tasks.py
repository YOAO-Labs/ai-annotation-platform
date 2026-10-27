"""Move legacy upload Task writes behind the Labeling owner."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0359"
down_revision = "bo0358"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1] / "sql" / "0359_labeling_upload_tasks.sql"
)


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            op.execute(sa.text(statement.strip().replace(":", r"\:")))


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION aap_labeling_v1.complete_uploading_task(uuid,uuid,uuid,text,text) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_labeling_v1.create_uploading_task(uuid,uuid,text,text,text) RESTRICT"
    )
