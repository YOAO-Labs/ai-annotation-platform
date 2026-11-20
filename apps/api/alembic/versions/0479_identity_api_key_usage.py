"""Keep API key activity off the authority row used by workers."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0479"
down_revision = "bo0478"
branch_labels = depends_on = None


def upgrade() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "sql" / "0479_identity_api_key_usage.sql"
    )
    for statement in source.read_text(encoding="utf-8").split("-- aap-statement-break"):
        op.execute(
            sa.text(statement.replace(":", r"\:")).execution_options(no_parameters=True)
        )


def downgrade() -> None:
    raise NotImplementedError("API key activity separation is forward-only")
