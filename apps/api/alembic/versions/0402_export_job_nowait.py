"""Expose the frozen export worker's exact Jobs NOWAIT fence."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0402"
down_revision = "bo0401"
branch_labels = depends_on = None


def upgrade() -> None:
    sql = (
        Path(__file__).resolve().parents[1] / "sql" / "0402_export_job_nowait.sql"
    ).read_text(encoding="utf-8")
    for statement in sql.split("-- aap-statement-break"):
        if statement.strip():
            op.execute(
                sa.text(statement.replace(":", r"\:")).execution_options(
                    no_parameters=True
                )
            )


def downgrade() -> None:
    op.execute("DROP FUNCTION aap_jobs_v1.read_job_for_update_nowait(uuid) RESTRICT")
