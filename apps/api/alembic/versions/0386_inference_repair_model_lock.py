"""Share-lock a mask-repair backend route through the Inference topology owner."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0386"
down_revision = "bo0385"
branch_labels = depends_on = None

_SQL = (
    Path(__file__).resolve().parents[1] / "sql" / "0386_inference_repair_model_lock.sql"
)


def upgrade() -> None:
    for statement in _SQL.read_text(encoding="utf-8").split("-- aap-statement-break"):
        if statement.strip():
            op.execute(
                sa.text(statement.strip().replace(":", r"\:")).execution_options(
                    no_parameters=True
                )
            )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION aap_inference_topology_v1.lock_repair_model_rows("
        "uuid, uuid, uuid[]) RESTRICT"
    )
