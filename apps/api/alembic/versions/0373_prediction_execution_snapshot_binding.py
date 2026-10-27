"""Bind immutable prediction snapshots through the Jobs owner."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0373"
down_revision = "bo0372"
branch_labels = depends_on = None

_SQL = (
    Path(__file__).resolve().parents[1]
    / "sql"
    / "0373_prediction_execution_snapshot_binding.sql"
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
        "DROP FUNCTION aap_jobs_v1.bind_prediction_execution_snapshot(uuid,uuid,uuid) RESTRICT"
    )
