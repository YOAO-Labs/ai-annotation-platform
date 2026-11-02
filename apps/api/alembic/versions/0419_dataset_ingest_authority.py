"""Fence synchronous Dataset ingest publication with admitted authority."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0419"
down_revision = "bo0418"
branch_labels = depends_on = None


def _apply(filename: str) -> None:
    sql = (Path(__file__).resolve().parents[1] / "sql" / filename).read_text(
        encoding="utf-8"
    )
    for statement in sql.split("-- aap-statement-break"):
        if statement.strip():
            op.execute(
                sa.text(statement.replace(":", r"\:")).execution_options(
                    no_parameters=True
                )
            )


def upgrade() -> None:
    _apply("0419_dataset_ingest_authority.sql")


def downgrade() -> None:
    _apply("0419_dataset_ingest_authority_down.sql")
