"""Observe fixture-owned synchronous prediction imports without backfilling."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0483"
down_revision = "bo0482"
branch_labels = depends_on = None

_SQL = Path(__file__).resolve().parents[1] / "sql"
_BREAK = "-- aap-statement-break"


def _execute(statement: str) -> None:
    op.execute(
        sa.text(statement.replace(":", r"\:")).execution_options(no_parameters=True)
    )


def upgrade() -> None:
    source = (_SQL / "0483_fixture_prediction_import.sql").read_text(encoding="utf-8")
    for statement in source.split(_BREAK):
        _execute(statement)


def downgrade() -> None:
    source = (_SQL / "0480_fixture_annotation_import.sql").read_text(encoding="utf-8")
    _execute(source.rsplit(_BREAK, 1)[1])
    op.execute("DROP TRIGGER fixture_prediction_import_owner ON public.async_jobs")
    op.execute("DROP FUNCTION yoao_jobs_v1.observe_fixture_prediction_import()")
    op.execute(
        "DELETE FROM yoao_jobs_v1.fixture_annotation_imports WHERE kind = 'predictions_import'"
    )
    op.execute(
        "ALTER TABLE yoao_jobs_v1.fixture_annotation_imports "
        "DROP COLUMN kind, DROP COLUMN running_at, DROP COLUMN result"
    )
