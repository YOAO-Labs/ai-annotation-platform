"""Install bounded PostgreSQL storage reference and usage capabilities."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0497"
down_revision = "bo0496"
branch_labels = depends_on = None


def upgrade() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "sql"
        / "0497_storage_inventory_capabilities.sql"
    )
    for statement in source.read_text(encoding="utf-8").split("-- aap-statement-break"):
        op.execute(
            sa.text(statement.replace(":", r"\:")).execution_options(no_parameters=True)
        )


def downgrade() -> None:
    raise NotImplementedError("Storage inventory capabilities are forward-only")
