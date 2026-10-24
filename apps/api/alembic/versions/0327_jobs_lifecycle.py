"""Install Jobs lifecycle capabilities while retaining borrowed ORM units of work."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0327"
down_revision = "0326"
branch_labels = depends_on = None

_SQL_PATH = Path(__file__).resolve().parents[1] / "sql" / "0327_jobs_lifecycle.sql"
_FUNCTIONS = (
    "aap_jobs_v1.apply_lifecycle(uuid,jsonb)",
    "aap_jobs_v1._jobs_lifecycle_integer(jsonb)",
    "aap_jobs_v1._jobs_lifecycle_is_truthy(jsonb)",
    "aap_jobs_v1._jobs_lifecycle_has_keys(jsonb,text[])",
    "aap_jobs_v1.delete_terminal_jobs_before(timestamptz,text[])",
    "aap_jobs_v1.delete_batch_predict_jobs(uuid)",
    "aap_jobs_v1.cancel_batch_predict_jobs(uuid,timestamptz)",
    "aap_jobs_v1.mark_dispatch_failed(uuid,timestamptz,text)",
    "aap_jobs_v1.insert_video_summary(uuid,text,uuid,uuid,jsonb,text)",
    "aap_jobs_v1.jobs_by_execution_id(text)",
    "aap_jobs_v1.read_job(uuid)",
)


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            op.execute(sa.text(statement.strip().replace(":", r"\:")))


def downgrade() -> None:
    for signature in _FUNCTIONS:
        op.execute(f"DROP FUNCTION {signature} RESTRICT")
    op.execute("DROP VIEW aap_jobs_v1.async_job_creation RESTRICT")
