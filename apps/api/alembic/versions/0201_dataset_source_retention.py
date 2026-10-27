"""Publish Learning source-retention guards for dataset lifecycle commands.

Revision ID: bo0201
Revises: bo0200
"""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0201"
down_revision = "bo0200"
branch_labels = depends_on = None

_ARTIFACT = (
    Path(__file__).resolve().parents[1] / "sql/0201_dataset_source_retention.sql"
)
_STATEMENT_SEPARATOR = "-- aap-statement-break"


def upgrade() -> None:
    for statement in _ARTIFACT.read_text(encoding="utf-8").split(_STATEMENT_SEPARATOR):
        if statement.strip():
            op.execute(sa.text(statement.strip()))


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION aap_learning_v1.guard_dataset_unlink(uuid, uuid) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_learning_v1.guard_dataset_item_deletion(uuid, uuid) RESTRICT"
    )
