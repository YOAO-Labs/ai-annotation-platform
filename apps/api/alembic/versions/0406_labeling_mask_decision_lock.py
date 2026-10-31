"""Lock AI Mask decisions through the Labeling owner during acceptance."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0406"
down_revision = "bo0405"
branch_labels = depends_on = None


_SQL_ROOT = Path(__file__).resolve().parents[1] / "sql"


def _apply(statement: str) -> None:
    op.execute(
        sa.text(statement.replace(":", r"\:")).execution_options(no_parameters=True)
    )


def upgrade() -> None:
    _apply(
        (_SQL_ROOT / "0406_labeling_mask_decision_lock.sql").read_text(encoding="utf-8")
    )


def downgrade() -> None:
    source = (_SQL_ROOT / "0348_labeling_mask_edits.sql").read_text(encoding="utf-8")
    statement = source.split("-- aap-statement-break")[-1].strip()
    _apply(statement.replace("CREATE FUNCTION ", "CREATE OR REPLACE FUNCTION ", 1))
