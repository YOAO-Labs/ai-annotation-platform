"""Move the three read-only Workspace query owners to their current namespace."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0471"
down_revision = "bo0470"
branch_labels = depends_on = None

_SQL = Path(__file__).resolve().parents[1] / "sql"


def upgrade() -> None:
    source = (_SQL / "0471_workspace_query_namespace.sql").read_text(encoding="utf-8")
    for statement in source.split("-- aap-statement-break"):
        if statement.strip():
            op.execute(
                sa.text(statement.strip().replace(":", r"\:")).execution_options(
                    no_parameters=True
                )
            )


def downgrade() -> None:
    raise NotImplementedError("Beta Workspace query namespace cutover is forward-only")
