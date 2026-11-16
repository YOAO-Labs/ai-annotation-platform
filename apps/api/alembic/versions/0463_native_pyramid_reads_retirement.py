"""Align native pyramid reads and retirement with saved output ownership."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0463"
down_revision = "bo0462"
branch_labels = depends_on = None

_SQL = Path(__file__).resolve().parents[1] / "sql"


def upgrade() -> None:
    for name in (
        "0463_native_pyramid_reads_retirement.sql",
        "0463_native_pyramid_projection.sql",
        "0463_native_pyramid_retirement.sql",
    ):
        source = (_SQL / name).read_text(encoding="utf-8")
        for statement in source.split("-- aap-statement-break"):
            if statement.strip():
                op.execute(
                    sa.text(statement.strip().replace(":", r"\:")).execution_options(
                        no_parameters=True
                    )
                )


def downgrade() -> None:
    raise NotImplementedError(
        "Beta native pyramid lifecycle correction is forward-only"
    )
