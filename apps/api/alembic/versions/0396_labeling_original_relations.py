"""Publish the bounded original-relation locking capability."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0396"
down_revision = "bo0395"
branch_labels = depends_on = None


def _apply(name: str) -> None:
    sql = (Path(__file__).resolve().parents[1] / "sql" / name).read_text(
        encoding="utf-8"
    )
    for statement in sql.split("-- aap-statement-break"):
        if statement.strip():
            op.execute(
                sa.text(statement.strip().replace(":", r"\:")).execution_options(
                    no_parameters=True
                )
            )


def upgrade() -> None:
    _apply("0396_labeling_original_relations.sql")


def downgrade() -> None:
    _apply("0396_labeling_original_relations_down.sql")
