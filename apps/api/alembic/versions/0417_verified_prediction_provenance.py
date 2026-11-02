"""Match durable results separately from transaction-qualified source evidence."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0417"
down_revision = "bo0416"
branch_labels = depends_on = None


def _apply(filename: str) -> None:
    sql = (Path(__file__).resolve().parents[1] / "sql" / filename).read_text(
        encoding="utf-8"
    )
    op.execute(sa.text(sql.replace(":", r"\:")).execution_options(no_parameters=True))


def upgrade() -> None:
    _apply("0417_verified_prediction_provenance.sql")


def downgrade() -> None:
    _apply("0417_verified_prediction_provenance_down.sql")
