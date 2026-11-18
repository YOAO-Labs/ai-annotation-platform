"""Move Workspace membership to its current owned namespace."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0467"
down_revision = "bo0466"
branch_labels = depends_on = None

_SQL = Path(__file__).resolve().parents[1] / "sql"


def upgrade() -> None:
    source = (_SQL / "0467_workspace_membership_namespace.sql").read_text(
        encoding="utf-8"
    )
    for statement in source.split("-- aap-statement-break"):
        if statement.strip():
            op.execute(
                sa.text(statement.strip().replace(":", r"\:")).execution_options(
                    no_parameters=True
                )
            )


def downgrade() -> None:
    raise NotImplementedError(
        "Beta Workspace membership namespace cutover is forward-only"
    )
