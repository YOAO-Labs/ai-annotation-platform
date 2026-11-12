"""Retain exact physical ZIP provenance for private fixture retirement."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0442"
down_revision = "bo0441"
branch_labels = depends_on = None


def upgrade() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "sql"
        / "0442_fixture_export_zip_versions.sql"
    ).read_text(encoding="utf-8")
    for statement in source.split("-- aap-statement-break"):
        if statement.strip():
            op.execute(
                sa.text(statement.strip().replace(":", r"\:")).execution_options(
                    no_parameters=True
                )
            )


def downgrade() -> None:
    raise RuntimeError("Cannot discard fixture ZIP version retirement evidence")
