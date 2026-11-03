"""Expose bounded native video export reads and identity-only shared locks."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0428"
down_revision = "bo0427"
branch_labels = depends_on = None


def _apply(name: str) -> None:
    sql = (Path(__file__).resolve().parents[1] / "sql" / name).read_text(
        encoding="utf-8"
    )
    for statement in sql.split("-- aap-statement-break"):
        if statement.strip():
            op.execute(
                sa.text(statement.replace(":", r"\:")).execution_options(
                    no_parameters=True
                )
            )


def upgrade() -> None:
    _apply("0428_video_export_core_reads.sql")


def downgrade() -> None:
    _apply("0428_video_export_core_reads_down.sql")
