"""Record native pyramid receipts using the saved output storage reference."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0462"
down_revision = "bo0461"
branch_labels = depends_on = None

_SQL = Path(__file__).resolve().parents[1] / "sql"


def upgrade() -> None:
    source = (_SQL / "0462_fixture_native_pyramid_receipts.sql").read_text(
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
    raise NotImplementedError("Beta fixture receipt correction is forward-only")
