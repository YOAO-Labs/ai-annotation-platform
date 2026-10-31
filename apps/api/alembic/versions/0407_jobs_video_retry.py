"""Expose narrowly scoped video retry observer lookups from Jobs."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0407"
down_revision = "bo0406"
branch_labels = depends_on = None


def upgrade() -> None:
    source = Path(__file__).resolve().parents[1] / "sql" / "0407_jobs_video_retry.sql"
    for statement in source.read_text(encoding="utf-8").split("-- aap-statement-break"):
        if statement.strip():
            op.execute(
                sa.text(statement.strip().replace(":", r"\:")).execution_options(
                    no_parameters=True
                )
            )


def downgrade() -> None:
    op.execute("DROP FUNCTION aap_jobs_v1.lock_video_retry_jobs(uuid)")
    op.execute("DROP FUNCTION aap_jobs_v1.find_video_retry_job(uuid,uuid,text)")
