"""Separate Identity presence from account authority without changing its fences."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0423"
down_revision = "bo0422"
branch_labels = depends_on = None


def _apply(name: str) -> None:
    sql = (Path(__file__).resolve().parents[1] / "sql" / name).read_text(
        encoding="utf-8"
    )
    for statement in sql.split("-- aap-statement-break"):
        if statement.strip():
            op.execute(
                sa.text(statement.replace(":", r"\:")).execution_options(
                    no_parameters=True
                )
            )


def upgrade() -> None:
    _apply("0423_identity_presence_actor.sql")
    _apply("0423_identity_presence.sql")


def downgrade() -> None:
    _apply("0423_identity_presence_down.sql")
    _apply("0423_identity_presence_actor_down.sql")
