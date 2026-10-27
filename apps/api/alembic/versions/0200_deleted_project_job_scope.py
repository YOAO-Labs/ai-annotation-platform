"""Retain project authorization scope after project deletion.

Revision ID: bo0200
Revises: bo0199
"""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0200"
down_revision = "bo0199"
branch_labels = depends_on = None

_ARTIFACT = (
    Path(__file__).resolve().parents[1] / "sql/0200_deleted_project_job_scope.sql"
)
_STATEMENT_SEPARATOR = "-- aap-statement-break"


def upgrade() -> None:
    for statement in _ARTIFACT.read_text(encoding="utf-8").split(_STATEMENT_SEPARATOR):
        if statement.strip():
            op.execute(sa.text(statement.strip()))


def downgrade() -> None:
    retained_scopes = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM public.async_jobs "
                "WHERE retained_project_id IS NOT NULL)"
            )
        )
        .scalar_one()
    )
    if retained_scopes:
        raise RuntimeError(
            "Cannot remove retained async-job project scopes without widening access"
        )
    original = _ARTIFACT.with_name("0193_task_creation_jobs.sql").read_text(
        encoding="utf-8"
    )
    for name in (
        "claim_task_creation_job",
        "finish_task_creation_job",
        "fail_task_creation_job",
    ):
        statement = next(
            part.strip()
            for part in original.split(_STATEMENT_SEPARATOR)
            if part.strip().startswith(f"CREATE FUNCTION aap_jobs_v1.{name}(")
        )
        op.execute(
            sa.text(
                statement.replace("CREATE FUNCTION ", "CREATE OR REPLACE FUNCTION ", 1)
            )
        )
    op.execute("DROP TRIGGER async_jobs_retain_project_scope ON public.async_jobs")
    op.execute("DROP FUNCTION aap_jobs_v1.retain_async_job_project_scope() RESTRICT")
    op.execute("ALTER TABLE public.async_jobs DROP COLUMN retained_project_id")
