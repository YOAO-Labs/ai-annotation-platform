"""Retain project authorization scope after project deletion.

Revision ID: 0200
Revises: 0199
"""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "0200"
down_revision = "0199"
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
    op.execute("DROP TRIGGER async_jobs_retain_project_scope ON public.async_jobs")
    op.execute("DROP FUNCTION aap_jobs_v1.retain_async_job_project_scope() RESTRICT")
    op.execute("ALTER TABLE public.async_jobs DROP COLUMN retained_project_id")
