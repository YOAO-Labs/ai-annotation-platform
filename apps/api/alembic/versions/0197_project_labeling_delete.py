"""Add the labeling-owned project deletion data contract.

Revision ID: bo0197
Revises: bo0196
"""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0197"
down_revision = "bo0196"
branch_labels = depends_on = None

_ARTIFACT_NAME = "0197_project_labeling_delete.sql"
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
        "DROP FUNCTION aap_labeling_v1.delete_project_owned_records(uuid) RESTRICT"
    )
    op.execute("DROP FUNCTION aap_labeling_v1.project_task_references(uuid) RESTRICT")
