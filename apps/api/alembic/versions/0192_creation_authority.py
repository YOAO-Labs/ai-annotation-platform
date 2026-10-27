"""Add versioned identity, workspace, and dataset-link authority capabilities.

Revision ID: bo0192
Revises: bo0191
"""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0192"
down_revision = "bo0191"
branch_labels = depends_on = None

_ARTIFACT_NAME = "0192_creation_authority.sql"
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
        "DROP FUNCTION aap_assets_v1.associate_project(uuid, uuid, uuid) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_workspace_v1.lock_project_manager(uuid, uuid) RESTRICT"
    )
    op.execute("DROP FUNCTION aap_identity_v1.lock_active_actor(uuid) RESTRICT")
    op.execute("DROP SCHEMA aap_identity_v1 RESTRICT")
