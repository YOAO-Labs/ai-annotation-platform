"""Bind reused ordinary image attempts to their real source permits."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0448"
down_revision = "bo0447"
branch_labels = depends_on = None


def upgrade() -> None:
    source = (Path(__file__).resolve().parents[1] / "sql" / "0448_fixture_external_pyramid_reuse.sql").read_text(encoding="utf-8")
    for statement in source.split("-- aap-statement-break"):
        if statement.strip():
            op.execute(sa.text(statement.strip().replace(":", r"\:")).execution_options(no_parameters=True))


def downgrade() -> None:
    raise RuntimeError("Cannot discard immutable ordinary pyramid reuse evidence")
