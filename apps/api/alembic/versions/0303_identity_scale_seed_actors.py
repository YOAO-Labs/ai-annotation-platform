"""Add fixed Identity actor selectors for the scale seed."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0303"
down_revision = "bo0302"
branch_labels = depends_on = None

_SQL = Path(__file__).resolve().parents[1] / "sql"


def upgrade() -> None:
    for statement in (
        (_SQL / "0303_identity_scale_seed_actors.sql")
        .read_text()
        .split("-- aap-statement-break")
    ):
        if statement.strip():
            op.execute(sa.text(statement.strip().replace(":", r"\:")))


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION aap_identity_v1.read_scale_seed_annotator(uuid[]) RESTRICT"
    )
    op.execute("DROP FUNCTION aap_identity_v1.read_scale_seed_super_admin() RESTRICT")
