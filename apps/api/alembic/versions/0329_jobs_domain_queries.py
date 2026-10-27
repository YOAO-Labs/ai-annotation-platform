"""Move fixed cross-frame and preannotation queries into Jobs."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0329"
down_revision = "bo0328"
branch_labels = depends_on = None

_SQL_PATH = Path(__file__).resolve().parents[1] / "sql" / "0329_jobs_domain_queries.sql"


def upgrade() -> None:
    for statement in _SQL_PATH.read_text().split("-- aap-statement-break"):
        if statement.strip():
            op.execute(sa.text(statement.strip().replace(":", r"\:")))


def downgrade() -> None:
    for signature in (
        "latest_batch_prediction_times(uuid[])",
        "cross_frame_job_count(uuid,text)",
        "cross_frame_job_page(uuid,text,integer)",
        "active_cross_frame_job(uuid,text)",
        "lock_cross_frame_singleflight(uuid,text)",
    ):
        op.execute(f"DROP FUNCTION aap_jobs_v1.{signature} RESTRICT")
