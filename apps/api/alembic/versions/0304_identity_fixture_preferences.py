"""Add the guarded Identity fixture preference replacement capability."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0304"
down_revision = "bo0303"
branch_labels = depends_on = None

_SQL = Path(__file__).resolve().parents[1] / "sql"


def upgrade() -> None:
    for statement in (
        (_SQL / "0304_identity_fixture_preferences.sql")
        .read_text()
        .split("-- aap-statement-break")
    ):
        if statement.strip():
            op.execute(sa.text(statement.strip().replace(":", r"\:")))


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION aap_identity_v1.replace_fixture_preferences(uuid,jsonb) RESTRICT"
    )
