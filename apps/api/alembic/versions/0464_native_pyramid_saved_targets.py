"""Saved-output reads, target context and store-free Native retirement claims."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0464"
down_revision = "bo0463"
branch_labels = depends_on = None

_SQL = Path(__file__).resolve().parents[1] / "sql"


def upgrade() -> None:
    for name in (
        "0464_native_pyramid_saved_targets.sql",
        "0464_native_storage_capture.sql",
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
    raise NotImplementedError("Dynamic Native saved-target lifecycle is forward-only")
