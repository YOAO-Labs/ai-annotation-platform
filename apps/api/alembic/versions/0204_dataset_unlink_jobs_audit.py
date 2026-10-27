"""Add Jobs cancellation and fixed dataset unlink audit capabilities.

Revision ID: bo0204
Revises: bo0203
"""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0204"
down_revision = "bo0203"
branch_labels = depends_on = None

_ARTIFACT = (
    Path(__file__).resolve().parents[1] / "sql/0204_dataset_unlink_jobs_audit.sql"
)
_STATEMENT_SEPARATOR = "-- aap-statement-break"


def upgrade() -> None:
    for statement in _ARTIFACT.read_text(encoding="utf-8").split(_STATEMENT_SEPARATOR):
        if statement.strip():
            op.execute(sa.text(statement.strip()))


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION aap_audit_v1.record_dataset_unlink(uuid, uuid, uuid, uuid, bigint, bigint, uuid[], jsonb) RESTRICT"
    )
    op.execute("DROP FUNCTION aap_audit_v1.valid_request_context(jsonb) RESTRICT")
    op.execute(
        "DROP FUNCTION aap_jobs_v1.cancel_dataset_task_creation(uuid, uuid, uuid) RESTRICT"
    )
