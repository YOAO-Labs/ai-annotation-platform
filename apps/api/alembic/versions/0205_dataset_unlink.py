"""Coordinate dataset unlink through owner capabilities and immutable receipts.

Revision ID: bo0205
Revises: bo0204
"""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0205"
down_revision = "bo0204"
branch_labels = depends_on = None


def upgrade() -> None:
    artifact = Path(__file__).resolve().parents[1] / "sql/0205_dataset_unlink.sql"
    for statement in artifact.read_text().split("-- aap-statement-break"):
        if statement.strip():
            op.execute(sa.text(statement.strip()))


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION aap_workspace_v1.unlink_dataset(uuid, uuid, uuid, uuid, text, jsonb) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_workspace_v1.preview_dataset_unlink(uuid, uuid, uuid) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_workspace_v1._dataset_unlink_failure(text, text) RESTRICT"
    )
    op.drop_table("dataset_unlink_operations")
