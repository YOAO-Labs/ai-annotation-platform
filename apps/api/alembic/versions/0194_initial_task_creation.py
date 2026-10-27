"""Persist accepted initial task-creation plans and chunk receipts.

Revision ID: bo0194
Revises: bo0193
"""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0194"
down_revision = "bo0193"
branch_labels = depends_on = None

_ARTIFACT_NAME = "0194_initial_task_creation.sql"
_STATEMENT_SEPARATOR = "-- aap-statement-break"


def _artifact_statements() -> list[str]:
    artifact = Path(__file__).resolve().parents[1] / "sql" / _ARTIFACT_NAME
    return [
        statement.strip()
        for statement in artifact.read_text(encoding="utf-8").split(
            _STATEMENT_SEPARATOR
        )
        if statement.strip()
    ]


def upgrade() -> None:
    for statement in _artifact_statements():
        op.execute(sa.text(statement))


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION aap_labeling_v1.advance_dataset_task_creation(uuid, integer) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_labeling_v1.read_dataset_task_creation(uuid) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_labeling_v1.prepare_dataset_task_creation(uuid, jsonb) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_labeling_v1.accept_dataset_task_creation(uuid, uuid, uuid, uuid, uuid, uuid, text, integer) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_assets_v1.lock_task_creation_items(uuid, uuid[]) RESTRICT"
    )
    op.drop_table("task_creation_chunks")
    op.drop_table("task_creation_operations")
