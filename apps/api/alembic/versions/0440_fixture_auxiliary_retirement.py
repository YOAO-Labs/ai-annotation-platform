"""Retire qualified fixture auxiliary owners without discarding their evidence."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0440"
down_revision = "bo0439"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"


def upgrade() -> None:
    source = (_SQL_DIR / "0440_fixture_auxiliary_retirement.sql").read_text(
        encoding="utf-8"
    )
    for statement in source.split("-- aap-statement-break"):
        if statement.strip():
            op.execute(
                sa.text(statement.strip().replace(":", r"\:")).execution_options(
                    no_parameters=True
                )
            )


def downgrade() -> None:
    raise RuntimeError("Cannot discard fixture auxiliary retirement authority")
