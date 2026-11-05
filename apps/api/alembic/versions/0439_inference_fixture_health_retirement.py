"""Retire direct health mutation for the obsolete workbench fixture backend."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0439"
down_revision = "bo0438"
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
    _install_sql("0439_inference_fixture_health_retirement.sql")


def downgrade() -> None:
    _install_sql("0369_inference_fixtures.sql")
