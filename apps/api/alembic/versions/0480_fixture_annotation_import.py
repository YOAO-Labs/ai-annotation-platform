"""Prove fixture-owned synchronous annotation imports settled at their owner."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0480"
down_revision = "bo0479"
branch_labels = depends_on = None

_SQL = Path(__file__).resolve().parents[1] / "sql"


def _execute(statement: str) -> None:
    op.execute(
        sa.text(statement.replace(":", r"\:")).execution_options(no_parameters=True)
    )


def upgrade() -> None:
    source = (_SQL / "0480_fixture_annotation_import.sql").read_text(encoding="utf-8")
    for statement in source.split("-- aap-statement-break"):
        _execute(statement)


def downgrade() -> None:
    source = (_SQL / "0436_fixture_jobs_join.sql").read_text(encoding="utf-8")
    original = source.split("-- aap-statement-break", 1)[0]
    _execute(
        original.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1).replace(
            "aap_jobs_v1", "yoao_jobs_v1"
        )
    )
    op.execute("DROP TRIGGER fixture_annotation_import_owner ON public.async_jobs")
    op.execute("DROP FUNCTION yoao_jobs_v1.observe_fixture_annotation_import()")
    op.execute("DROP TABLE yoao_jobs_v1.fixture_annotation_imports")
