"""Make caller plugin fences compatible with independent routing admission."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0393"
down_revision = "bo0392"
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
    _apply("0393_plugin_registry_admission_fence.sql")


def downgrade() -> None:
    _apply("0393_plugin_registry_admission_fence_down.sql")
