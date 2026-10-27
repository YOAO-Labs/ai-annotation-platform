"""Add Labeling-owned dataset unlink capabilities.

Revision ID: bo0202
Revises: bo0201
"""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0202"
down_revision = "bo0201"
branch_labels = depends_on = None

_ARTIFACT_NAME = "0202_dataset_unlink_labeling.sql"
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
    for signature in (
        "aap_labeling_v1.delete_dataset_tasks(uuid, uuid)",
        "aap_labeling_v1.preview_dataset_unlink(uuid, uuid)",
        "aap_labeling_v1.guard_dataset_unlink(uuid, uuid)",
        "aap_labeling_v1.lock_dataset_unlink_scope(uuid, uuid)",
        "aap_labeling_v1.dataset_task_references(uuid, uuid)",
        "aap_labeling_v1._dataset_unlink_task_snapshot(uuid, uuid)",
    ):
        op.execute(f"DROP FUNCTION {signature} RESTRICT")
