"""Honor native prediction/export retirement in Dataset fixture fences."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0443"
down_revision = "bo0442"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"


def _execute(name: str) -> None:
    source = (_SQL_DIR / name).read_text(encoding="utf-8")
    for statement in source.split("-- aap-statement-break"):
        if statement.strip():
            op.execute(
                sa.text(statement.strip().replace(":", r"\:")).execution_options(
                    no_parameters=True
                )
            )


def upgrade() -> None:
    _execute("0443_fixture_dataset_retired_jobs.sql")


def downgrade() -> None:
    _execute("0437_fixture_reclaim_job_scope.sql")
    _execute("0441_fixture_presentation_dataset_guard.sql")
