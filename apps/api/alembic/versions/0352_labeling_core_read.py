"""Publish Labeling's core-row read relations and exact row-lock operations."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0352"
down_revision = "0351"
branch_labels = depends_on = None

_SQL_PATH = Path(__file__).resolve().parents[1] / "sql" / "0352_labeling_core_read.sql"


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            op.execute(sa.text(statement.strip().replace(":", r"\:")))


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION aap_labeling_v1.lock_annotation_rows_for_core_read(uuid[],uuid,uuid,text,boolean) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_labeling_v1.lock_batch_rows_for_core_read(uuid[],uuid,text,boolean) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_labeling_v1.lock_task_for_core_read(uuid,text,boolean) RESTRICT"
    )
    for relation in (
        "task_dataset_item_link_rows",
        "task_lock_rows",
        "failed_prediction_rows",
        "prediction_meta_rows",
        "prediction_rows",
        "annotation_rows",
        "batch_rows",
        "task_rows",
    ):
        op.execute(f"DROP VIEW aap_labeling_v1.{relation} RESTRICT")
