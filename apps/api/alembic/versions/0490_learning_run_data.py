"""Install fixed Learning capabilities for training execution and previews."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0490"
down_revision = "bo0489"
branch_labels = depends_on = None

_SQL = Path(__file__).resolve().parents[1] / "sql" / "0490_learning_run_data.sql"
_BREAK = "-- aap-statement-break"


def _execute(statement: str) -> None:
    op.execute(
        sa.text(statement.replace(":", r"\:")).execution_options(no_parameters=True)
    )


def upgrade() -> None:
    for statement in _SQL.read_text(encoding="utf-8").split(_BREAK):
        _execute(statement)


def downgrade() -> None:
    functions, views = _SQL.read_text(encoding="utf-8").split(_BREAK)[-2:]
    function_names = functions.strip().removeprefix("REVOKE ALL ON FUNCTION")
    view_names = views.strip().removeprefix("REVOKE ALL ON ")
    _execute("DROP FUNCTION " + function_names.removesuffix("\nFROM PUBLIC;"))
    _execute("DROP VIEW " + view_names.removesuffix(" FROM PUBLIC;"))
