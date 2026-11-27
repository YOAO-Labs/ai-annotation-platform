"""Add scoped Jobs list functions for platform storage maintenance jobs."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0517"
down_revision = "bo0516"
branch_labels = depends_on = None

_SQL = Path(__file__).resolve().parents[1] / "sql"


def _execute(statement: str) -> None:
    op.execute(
        sa.text(statement.replace(":", r"\:")).execution_options(no_parameters=True)
    )


def upgrade() -> None:
    source = (_SQL / "0517_jobs_list_platform_storage.sql").read_text(encoding="utf-8")
    for statement in source.split("-- aap-statement-break"):
        if statement.strip():
            _execute(statement)


def downgrade() -> None:
    for signature in (
        "yoao_jobs_v1.async_job_list_total_scoped("
        "uuid, text, text[], text[], uuid, text, boolean)",
        "yoao_jobs_v1.async_job_list_page_scoped("
        "uuid, text, text[], text[], uuid, text, integer, integer, boolean)",
        "yoao_jobs_v1._async_job_list_rows_scoped("
        "uuid, text, text[], text[], uuid, text, boolean)",
    ):
        _execute(f"DROP FUNCTION IF EXISTS {signature}")
