"""Preserve the project update timestamp on native task append.

Revision ID: bo0195
Revises: bo0194
"""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0195"
down_revision = "bo0194"
branch_labels = depends_on = None


def upgrade() -> None:
    artifact = (
        Path(__file__).resolve().parents[1] / "sql/0195_task_append_timestamp.sql"
    )
    op.execute(sa.text(artifact.read_text(encoding="utf-8")))


def downgrade() -> None:
    artifact = (
        Path(__file__).resolve().parents[1]
        / "sql/0191_dataset_task_append_contract.sql"
    )
    original = next(
        statement.strip()
        for statement in artifact.read_text(encoding="utf-8").split(
            "-- aap-statement-break"
        )
        if statement.strip().startswith(
            "CREATE FUNCTION aap_labeling_v1.append_dataset_items("
        )
    )
    op.execute(
        sa.text(original.replace("CREATE FUNCTION ", "CREATE OR REPLACE FUNCTION ", 1))
    )
