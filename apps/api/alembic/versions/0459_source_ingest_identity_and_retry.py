"""Require verified scan identity and immutable no-write evidence for retry."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0459"
down_revision = "bo0458"
branch_labels = depends_on = None

_SQL = Path(__file__).resolve().parents[1] / "sql"


def upgrade() -> None:
    source = (_SQL / "0459_source_ingest_identity_and_retry.sql").read_text(
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
    raise NotImplementedError("Beta source identity and retry evidence is forward-only")
