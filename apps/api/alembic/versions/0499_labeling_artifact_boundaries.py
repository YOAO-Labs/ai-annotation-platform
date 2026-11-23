"""Keep Labeling and Workspace artifacts behind their owner capabilities."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0499"
down_revision = "bo0498"
branch_labels = depends_on = None


def upgrade() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "sql"
        / "0499_labeling_artifact_boundaries.sql"
    )
    for statement in source.read_text(encoding="utf-8").split("-- aap-statement-break"):
        op.execute(
            sa.text(statement.replace(":", r"\:")).execution_options(no_parameters=True)
        )


def downgrade() -> None:
    raise NotImplementedError(
        "Labeling and Workspace artifact capabilities are forward-only"
    )
