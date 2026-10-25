"""Keep guarded fixture Jobs deletion behind its owner capability."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0330"
down_revision = "0329"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1] / "sql" / "0330_jobs_fixture_cleanup.sql"
)


def upgrade() -> None:
    for statement in _SQL_PATH.read_text().split("-- aap-statement-break"):
        if statement.strip():
            op.execute(sa.text(statement.strip().replace(":", r"\:")))


def downgrade() -> None:
    op.execute("DROP FUNCTION aap_jobs_v1.delete_fixture_project_jobs(uuid[]) RESTRICT")
