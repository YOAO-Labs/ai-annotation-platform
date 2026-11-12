"""Freeze a born prediction Job's exact Dataset set before cleanup."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0456"
down_revision = "bo0455"
branch_labels = depends_on = None


def upgrade() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "sql"
        / "0456_prediction_dataset_scope.sql"
    ).read_text(encoding="utf-8")
    for statement in source.split("-- aap-statement-break"):
        if statement.strip():
            op.execute(
                sa.text(statement.strip().replace(":", r"\:")).execution_options(
                    no_parameters=True
                )
            )


def downgrade() -> None:
    raise RuntimeError("prediction Dataset scope bindings must be retained")
