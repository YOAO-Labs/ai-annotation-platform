"""Retire ordinary storage observation ACLs in favor of fixed owner capabilities."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0486"
down_revision = "bo0485"
branch_labels = depends_on = None


def upgrade() -> None:
    path = Path(__file__).resolve().parents[1] / "sql/0486_storage_fault_owner.sql"
    for statement in path.read_text(encoding="utf-8").split("-- aap-statement-break"):
        op.execute(
            sa.text(statement.replace(":", r"\:")).execution_options(no_parameters=True)
        )


def downgrade() -> None:
    raise NotImplementedError("Storage observation ownership cutover is forward-only")
