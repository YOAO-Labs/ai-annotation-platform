"""Add a fixed caller-transaction Workspace seed-member assignment pin."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0308"
down_revision = "bo0307"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1]
    / "sql"
    / "0308_workspace_seed_member_assignment.sql"
)
_FUNCTION = "aap_workspace_v1.pin_seed_member_assignment(uuid, timestamptz)"


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            op.execute(sa.text(statement.strip().replace(":", r"\:")))


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {_FUNCTION} RESTRICT")
