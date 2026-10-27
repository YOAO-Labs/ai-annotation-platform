"""Add fixed Identity and Workspace operations for the parent development seed."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0302"
down_revision = "bo0301"
branch_labels = depends_on = None

_SQL = Path(__file__).resolve().parents[1] / "sql"


def upgrade() -> None:
    for filename in (
        "0302_identity_seed_persona.sql",
        "0302_workspace_seed_members.sql",
    ):
        for statement in (_SQL / filename).read_text().split("-- aap-statement-break"):
            if statement.strip():
                op.execute(sa.text(statement.strip().replace(":", r"\:")))


def downgrade() -> None:
    op.execute("DROP FUNCTION aap_workspace_v1.write_seed_members(jsonb) RESTRICT")
    op.execute(
        "DROP FUNCTION aap_identity_v1.create_seed_persona_account(jsonb) RESTRICT"
    )
