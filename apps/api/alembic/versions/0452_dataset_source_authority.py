"""Publish the caller-transactional Dataset source authority projection."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0452"
down_revision = "bo0451"
branch_labels = depends_on = None


def upgrade() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "sql"
        / "0452_dataset_source_authority.sql"
    ).read_text(encoding="utf-8")
    for statement in source.split("-- aap-statement-break"):
        if statement.strip():
            op.execute(
                sa.text(statement.strip().replace(":", r"\:")).execution_options(
                    no_parameters=True
                )
            )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION aap_assets_v1.read_dataset_source_authority(uuid, boolean)"
    )
