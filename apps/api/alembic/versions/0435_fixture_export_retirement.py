"""Bind fixture exports to short lifetimes and durable execution retirement."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0435"
down_revision = "bo0434"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"


def _install_sql(name: str) -> None:
    source = (_SQL_DIR / name).read_text(encoding="utf-8")
    for statement in source.split("-- aap-statement-break"):
        if statement.strip():
            op.execute(
                sa.text(statement.strip().replace(":", r"\:")).execution_options(
                    no_parameters=True
                )
            )


def upgrade() -> None:
    _install_sql("0435_fixture_export_retention.sql")
    _install_sql("0435_fixture_export_zip_retirement.sql")
    _install_sql("0435_fixture_export_retired.sql")
    _install_sql("0435_fixture_export_execution.sql")
    _install_sql("0435_fixture_native_export.sql")
    _install_sql("0435_fixture_export_owner.sql")


def downgrade() -> None:
    _install_sql("0435_fixture_export_owner_down.sql")
    _install_sql("0435_fixture_native_export_down.sql")
    _install_sql("0435_fixture_export_execution_down.sql")
    _install_sql("0435_fixture_export_retention_down.sql")
