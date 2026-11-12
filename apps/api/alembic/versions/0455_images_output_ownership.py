"""Bind native pyramid output work to its saved physical owner."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0455"
down_revision = "bo0454"
branch_labels = depends_on = None


def upgrade() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "sql" / "0455_images_output_ownership.sql"
    ).read_text(encoding="utf-8")
    for statement in source.split("-- aap-statement-break"):
        if statement.strip():
            op.execute(
                sa.text(statement.strip().replace(":", r"\:")).execution_options(
                    no_parameters=True
                )
            )


def downgrade() -> None:
    raise RuntimeError("native pyramid output ownership evidence must be retained")
