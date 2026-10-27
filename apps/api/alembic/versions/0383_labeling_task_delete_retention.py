"""Retire Main feedback rows inside the frozen Labeling task-delete scope."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0383"
down_revision = "bo0382"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_ARTIFACT = "0383_labeling_task_delete_retention.sql"
_FROZEN = "0355_labeling_task_maintenance.sql"
_FUNCTION = "aap_labeling_v1.task_maintenance_command"


def _execute(statement: str) -> None:
    op.execute(
        sa.text(statement.replace(":", r"\:")).execution_options(no_parameters=True)
    )


def _function_definition(filename: str) -> str:
    source = (_SQL_DIR / filename).read_text(encoding="utf-8")
    prefix = f"CREATE OR REPLACE FUNCTION {_FUNCTION}("
    if source.count(prefix) != 1:
        raise RuntimeError(f"Expected one frozen definition of {_FUNCTION}")
    start = source.index(prefix)
    end = source.index("$function$;", start) + len("$function$;")
    return source[start:end]


def upgrade() -> None:
    for statement in (
        (_SQL_DIR / _ARTIFACT)
        .read_text(encoding="utf-8")
        .split("-- aap-statement-break")
    ):
        if statement.strip():
            _execute(statement.strip())


def downgrade() -> None:
    # CREATE OR REPLACE keeps the Labeling owner and its granted ACL.
    _execute(_function_definition(_FROZEN))
