"""Give Jobs ownership of transactional row-lock reads."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0328"
down_revision = "0327"
branch_labels = depends_on = None

_SQL_PATH = Path(__file__).resolve().parents[1] / "sql" / "0328_jobs_row_locks.sql"


def upgrade() -> None:
    for statement in _SQL_PATH.read_text().split("-- aap-statement-break"):
        if statement.strip():
            op.execute(sa.text(statement.strip().replace(":", r"\:")))


def downgrade() -> None:
    op.execute("DROP FUNCTION aap_jobs_v1.read_job_for_share_nowait(uuid) RESTRICT")
    op.execute("DROP FUNCTION aap_jobs_v1.read_job_for_update(uuid) RESTRICT")
