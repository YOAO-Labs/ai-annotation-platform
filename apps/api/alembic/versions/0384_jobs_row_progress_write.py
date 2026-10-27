"""Write merged Job progress facts through the Jobs owner."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0384"
down_revision = "bo0383"
branch_labels = depends_on = None

_SQL = Path(__file__).resolve().parents[1] / "sql" / "0384_jobs_row_progress_write.sql"


def upgrade() -> None:
    for statement in _SQL.read_text(encoding="utf-8").split("-- aap-statement-break"):
        if statement.strip():
            op.execute(
                sa.text(statement.strip().replace(":", r"\:")).execution_options(
                    no_parameters=True
                )
            )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION aap_jobs_v1.record_job_progress("
        "uuid, text, text, timestamp with time zone, jsonb) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_jobs_v1.read_job_progress_for_update("
        "uuid, text, text) RESTRICT"
    )
