"""Coordinate transactional project deletion through module owner capabilities.

Revision ID: bo0199
Revises: bo0198
"""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0199"
down_revision = "bo0198"
branch_labels = depends_on = None


def upgrade() -> None:
    artifact = Path(__file__).resolve().parents[1] / "sql/0199_project_deletion.sql"
    for statement in artifact.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            op.execute(sa.text(statement.strip()))


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION aap_workspace_v1.delete_project(uuid, uuid, uuid, jsonb) RESTRICT"
    )
    op.execute("DROP FUNCTION aap_workspace_v1._project_delete_failure(text) RESTRICT")
    op.execute(
        "DROP FUNCTION aap_audit_v1.record_project_deletion(uuid, uuid, uuid, text, text, bigint, bigint, jsonb) RESTRICT"
    )
    op.execute("DROP SCHEMA aap_audit_v1 RESTRICT")
    op.execute("DROP FUNCTION aap_identity_v1.lock_audit_actor(uuid) RESTRICT")
    op.drop_table("project_deletion_operations")
