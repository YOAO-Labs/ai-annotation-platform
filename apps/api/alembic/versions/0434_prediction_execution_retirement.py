"""Fence prediction execution and retain exact fixture scope retirement."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0434"
down_revision = "bo0433"
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
    _install_sql("0434_prediction_execution_routing.sql")
    _install_sql("0434_prediction_output_ownership.sql")


def downgrade() -> None:
    raise RuntimeError("Cannot discard prediction execution and retirement evidence")
