"""Move the filtering fixture terminal projections behind Jobs ownership."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0331"
down_revision = "0330"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1] / "sql" / "0331_jobs_filter_fixtures.sql"
)


def upgrade() -> None:
    for statement in _SQL_PATH.read_text().split("-- aap-statement-break"):
        if statement.strip():
            op.execute(sa.text(statement.strip().replace(":", r"\:")))


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION aap_jobs_v1.set_filter_fixture_job_outcomes(uuid,uuid) RESTRICT"
    )
