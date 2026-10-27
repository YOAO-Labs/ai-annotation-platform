"""Add the Jobs-owned task creation command contract.

Revision ID: bo0193
Revises: bo0192
"""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0193"
down_revision = "bo0192"
branch_labels = depends_on = None

_ARTIFACT_NAME = "0193_task_creation_jobs.sql"
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
        "DROP FUNCTION aap_jobs_v1.cancel_task_creation_job(uuid, uuid) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_jobs_v1.fail_task_creation_job(uuid, uuid, text) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_jobs_v1.finish_task_creation_job(uuid, uuid) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_jobs_v1.update_task_creation_progress(uuid, uuid, bigint, bigint, bigint) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_jobs_v1.lock_task_creation_job(uuid, uuid, uuid, uuid, uuid, uuid) RESTRICT"
    )
    op.execute("DROP FUNCTION aap_jobs_v1.claim_task_creation_job(uuid, text) RESTRICT")
    op.execute(
        "DROP FUNCTION aap_jobs_v1.find_task_creation_job(uuid, uuid, uuid, uuid) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_jobs_v1.create_task_creation_job(uuid, uuid, uuid, uuid, uuid) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_jobs_v1._task_creation_job_payload_valid(uuid, jsonb) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_jobs_v1._task_creation_job_failure(text, text) RESTRICT"
    )
    op.execute("DROP SCHEMA aap_jobs_v1 RESTRICT")
