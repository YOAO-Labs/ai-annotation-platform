"""Add the workspace project-reference transaction capability.

Revision ID: bo0190
Revises: 0267
"""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0190"
down_revision = "0267"
branch_labels = depends_on = None

_ARTIFACT_NAME = "0190_workspace_data_contract.sql"
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
    op.execute("DROP FUNCTION aap_workspace_v1.lock_project_reference(uuid) RESTRICT")
    op.execute("DROP SCHEMA aap_workspace_v1 RESTRICT")
