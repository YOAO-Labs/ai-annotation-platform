"""Add the bounded Identity attribution read for the image importer."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0454"
down_revision = "bo0453"
branch_labels = depends_on = None


def upgrade() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "sql"
        / "0454_identity_first_seed_account.sql"
    ).read_text()
    for statement in source.split("-- aap-statement-break"):
        if statement.strip():
            op.execute(sa.text(statement.strip().replace(":", r"\:")))


def downgrade() -> None:
    op.execute("DROP FUNCTION aap_identity_v1.read_first_seed_account() RESTRICT")
