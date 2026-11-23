"""Reserve, read and retire saved avatar receipts through the Identity owner."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0501"
down_revision = "bo0500"
branch_labels = depends_on = None

_SQL = Path(__file__).resolve().parents[1] / "sql" / "0501_identity_avatar_uploads.sql"


def upgrade() -> None:
    for statement in _SQL.read_text(encoding="utf-8").split("-- aap-statement-break"):
        if statement.strip():
            op.execute(
                sa.text(statement.strip().replace(":", r"\:")).execution_options(
                    no_parameters=True
                )
            )


def downgrade() -> None:
    raise NotImplementedError("Identity avatar receipt permissions are forward-only")
