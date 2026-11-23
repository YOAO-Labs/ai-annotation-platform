"""Install transaction-local Learning selection, policy, study and recipe capabilities."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0493"
down_revision = "bo0492"
branch_labels = depends_on = None

_SOURCE = Path(__file__).resolve().parents[1] / "sql" / "0493_learning_selection.sql"


def _statements():
    return [
        statement.strip()
        for statement in _SOURCE.read_text(encoding="utf-8").split(
            "-- aap-statement-break"
        )
        if statement.strip()
    ]


def upgrade() -> None:
    for statement in _statements():
        op.execute(
            sa.text(statement.replace(":", r"\:")).execution_options(no_parameters=True)
        )


def downgrade() -> None:
    for statement in reversed(_statements()):
        if statement.startswith("REVOKE ALL ON TABLE yoao_learning_v1."):
            op.execute(
                f"DROP VIEW {statement.removeprefix('REVOKE ALL ON TABLE ').removesuffix(' FROM PUBLIC;')}"
            )
        if statement.startswith("REVOKE ALL ON FUNCTION "):
            op.execute(
                f"DROP FUNCTION {statement.removeprefix('REVOKE ALL ON FUNCTION ').removesuffix('FROM PUBLIC;').strip()}"
            )
