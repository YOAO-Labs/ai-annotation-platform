"""Add Jobs and Audit support for Dataset/DatasetItem deletion.

Revision ID: bo0208
Revises: bo0207
"""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0208"
down_revision = "bo0207"
branch_labels = depends_on = None

_ARTIFACT = Path(__file__).resolve().parents[1] / "sql/0208_dataset_source_support.sql"
_STATEMENT_SEPARATOR = "-- aap-statement-break"


def upgrade() -> None:
    for statement in _ARTIFACT.read_text(encoding="utf-8").split(_STATEMENT_SEPARATOR):
        if statement.strip():
            op.execute(sa.text(statement.strip()))


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION aap_audit_v1.record_dataset_source_deletion(uuid, uuid, uuid, uuid, bigint, bigint, bigint, bigint, bigint, bigint, jsonb) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_jobs_v1.task_creation_execution_active(uuid, uuid) RESTRICT"
    )
    op.execute("DROP FUNCTION aap_jobs_v1.guard_dataset_source_deletion(uuid) RESTRICT")
