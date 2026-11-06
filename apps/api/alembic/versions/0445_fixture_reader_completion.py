"""Wake the private fixture owner after exact Assets reader settlement."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0445"
down_revision = "bo0444"
branch_labels = depends_on = None


def upgrade() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "sql"
        / "0445_assets_fixture_reader_completion.sql"
    ).read_text(encoding="utf-8")
    for statement in source.split("-- aap-statement-break"):
        if statement.strip():
            op.execute(
                sa.text(statement.strip().replace(":", r"\:")).execution_options(
                    no_parameters=True
                )
            )


def downgrade() -> None:
    raise RuntimeError("Cannot discard fixture reader completion coordination")
