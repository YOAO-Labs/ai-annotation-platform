"""Authorize terminal drain under the existing unload control fence."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0402"
down_revision = "bo0401"
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
    _apply("0402_plugin_terminal_drain_route.sql")


def downgrade() -> None:
    _apply("0402_plugin_terminal_drain_route_down.sql")
