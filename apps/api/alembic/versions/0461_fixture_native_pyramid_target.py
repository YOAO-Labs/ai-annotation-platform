"""Admit only the exact live native pyramid claim at its fixture target."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0461"
down_revision = "bo0460"
branch_labels = depends_on = None

_SQL = Path(__file__).resolve().parents[1] / "sql"


def upgrade() -> None:
    source = (_SQL / "0461_fixture_native_pyramid_target.sql").read_text(encoding="utf-8")
    for statement in source.split("-- aap-statement-break"):
        if statement.strip():
            op.execute(
                sa.text(statement.strip().replace(":", r"\:")).execution_options(
                    no_parameters=True
                )
            )


def downgrade() -> None:
    raise NotImplementedError("Beta fixture target admission is forward-only")
