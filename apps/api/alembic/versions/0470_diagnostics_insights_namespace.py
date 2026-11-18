"""Move Diagnostics and Insights owner capabilities to their current namespaces."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0470"
down_revision = "bo0469"
branch_labels = depends_on = None

_SQL = Path(__file__).resolve().parents[1] / "sql"


def upgrade() -> None:
    source = (_SQL / "0470_diagnostics_insights_namespace.sql").read_text(
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
        "Beta Diagnostics and Insights namespace cutover is forward-only"
    )
