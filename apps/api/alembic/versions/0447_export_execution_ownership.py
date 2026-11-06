"""Bind Jobs publication and Export execution to independent owners."""

from importlib.resources import files
from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0447"
down_revision = "bo0446"
branch_labels = depends_on = None


def _execute_source(source: str) -> None:
    for statement in source.split("-- aap-statement-break"):
        if statement.strip():
            op.execute(
                sa.text(statement.strip().replace(":", r"\:")).execution_options(
                    no_parameters=True
                )
            )


def _execute(name: str) -> None:
    _execute_source(
        (Path(__file__).resolve().parents[1] / "sql" / name).read_text(
            encoding="utf-8"
        )
    )


def upgrade() -> None:
    _execute_source(
        files("yoao_jobs")
        .joinpath("sql/0447_jobs_publication_execution.sql")
        .read_text(encoding="utf-8")
    )
    _execute_source(
        files("yoao_jobs")
        .joinpath("sql/0447_jobs_fixture_media_publication.sql")
        .read_text(encoding="utf-8")
    )
    _execute("0447_export_execution.sql")
    _execute("0447_export_fixture_rebind.sql")
    op.execute("DROP FUNCTION public.fixture_export_execution_admitted(uuid)")
    op.execute("DROP FUNCTION public.begin_fixture_export_execution(uuid,uuid,uuid)")
    op.execute("DROP FUNCTION public.finish_fixture_export_execution(uuid,uuid)")


def downgrade() -> None:
    raise RuntimeError("Cannot discard saved export execution or publication evidence")
