"""Close remaining production Labeling compatibility reads and lock adapters."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0361"
down_revision = "bo0360"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1]
    / "sql"
    / "0361_labeling_compatibility_reads.sql"
)


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            op.execute(sa.text(statement.strip().replace(":", r"\:")))


def downgrade() -> None:
    op.execute("DROP FUNCTION aap_labeling_v1.next_batch_display_id() RESTRICT")
    op.execute(
        "DROP FUNCTION aap_labeling_v1.lock_active_learning_feedback_annotation_rows(uuid[]) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_labeling_v1.lock_active_learning_feedback_task_rows(uuid,uuid[]) RESTRICT"
    )
    op.execute("DROP VIEW aap_assets_v1.labeling_dataset_item_facts RESTRICT")
    op.execute("DROP VIEW aap_labeling_v1.annotation_draft_rows RESTRICT")
