"""Publish Learning-owned project deletion retention capabilities.

Revision ID: bo0196
Revises: bo0195
"""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0196"
down_revision = "bo0195"
branch_labels = depends_on = None


def upgrade() -> None:
    artifact = (
        Path(__file__).resolve().parents[1] / "sql/0196_project_deletion_retention.sql"
    )
    for statement in artifact.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            op.execute(sa.text(statement.strip()))


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION aap_learning_v1.delete_project_owned_records(uuid) RESTRICT"
    )
    op.execute("DROP FUNCTION aap_learning_v1.guard_project_deletion(uuid) RESTRICT")
    op.execute("DROP FUNCTION aap_learning_v1.lock_project_deletion(uuid) RESTRICT")
    op.execute("DROP SCHEMA aap_learning_v1 RESTRICT")
