"""Move active routing admission headers without rewriting frozen receipts."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0469"
down_revision = "bo0468"
branch_labels = depends_on = None


def upgrade() -> None:
    path = Path(__file__).resolve().parents[1] / "sql/0469_routing_admission_header.sql"
    for statement in path.read_text(encoding="utf-8").split("-- aap-statement-break"):
        op.execute(
            sa.text(statement.replace(":", r"\:")).execution_options(no_parameters=True)
        )


def downgrade() -> None:
    raise NotImplementedError("Routing wire cutover is forward-only")
