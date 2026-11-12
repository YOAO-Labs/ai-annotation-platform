"""Preserve approved video candidates through status and restore verification."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0450"
down_revision = "bo0449"
branch_labels = depends_on = None


def upgrade() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "sql"
        / "0450_video_candidate_status_restore.sql"
    ).read_text(encoding="utf-8")
    for statement in source.split("-- aap-statement-break"):
        if statement.strip():
            op.execute(
                sa.text(statement.strip().replace(":", r"\:")).execution_options(
                    no_parameters=True
                )
            )


def downgrade() -> None:
    raise RuntimeError("Cannot discard approved candidate status and restore support")
