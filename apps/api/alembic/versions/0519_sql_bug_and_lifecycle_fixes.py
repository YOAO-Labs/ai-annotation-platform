"""Fix SQL rowtype projections, registry auth rotation, rollout blocking, reclamation regex, pyramid schema-1 retirement, and task-creation lifecycle fencing."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0519"
down_revision = "bo0518"
branch_labels = depends_on = None

_SQL = Path(__file__).resolve().parents[1] / "sql"
_BREAK = "-- aap-statement-break"
_FORWARD = "0519_sql_bug_and_lifecycle_fixes.sql"
_PREVIOUS = "0519_sql_bug_and_lifecycle_fixes.previous.sql"


def _execute(statement: str) -> None:
    op.execute(
        sa.text(statement.replace(":", r"\:")).execution_options(no_parameters=True)
    )


def _statements(filename: str) -> list[str]:
    return [
        statement.strip()
        for statement in (_SQL / filename).read_text(encoding="utf-8").split(_BREAK)
        if statement.strip()
    ]


def upgrade() -> None:
    for statement in _statements(_FORWARD):
        _execute(statement)


def downgrade() -> None:
    for statement in _statements(_PREVIOUS):
        _execute(statement)
