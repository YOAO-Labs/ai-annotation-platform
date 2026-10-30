"""Store installation-scoped plugin asset decisions separately from lifecycle."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0403"
down_revision = "bo0402"
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
    _apply("0403_plugin_asset_approval.sql")


def downgrade() -> None:
    _apply("0403_plugin_asset_approval_down.sql")
