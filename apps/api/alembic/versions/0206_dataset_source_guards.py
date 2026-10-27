"""Add Learning and Labeling source-deletion guards.

Revision ID: bo0206
Revises: bo0205
"""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0206"
down_revision = "bo0205"
branch_labels = depends_on = None

_ARTIFACT = Path(__file__).resolve().parents[1] / "sql/0206_dataset_source_guards.sql"
_STATEMENT_SEPARATOR = "-- aap-statement-break"


def upgrade() -> None:
    for statement in _ARTIFACT.read_text(encoding="utf-8").split(_STATEMENT_SEPARATOR):
        if statement.strip():
            op.execute(sa.text(statement.strip()))


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION aap_labeling_v1.detach_deleted_dataset_attribution(uuid) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_labeling_v1.guard_dataset_source_deletion(uuid, uuid) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_learning_v1.guard_dataset_source_deletion(uuid, uuid) RESTRICT"
    )
