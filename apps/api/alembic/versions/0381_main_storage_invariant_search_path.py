"""Pin main storage invariant triggers to fixed relations and a fixed search_path."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0381"
down_revision = "bo0380"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_FORWARD = "0381_main_storage_invariant_search_path.sql"
_PREVIOUS = "0381_main_storage_invariant_search_path.previous.sql"


def _execute(statement: str) -> None:
    op.execute(
        sa.text(statement.replace(":", r"\:")).execution_options(no_parameters=True)
    )


def _statements(filename: str) -> list[str]:
    return [
        statement.strip()
        for statement in (_SQL_DIR / filename)
        .read_text(encoding="utf-8")
        .split("-- aap-statement-break")
        if statement.strip()
    ]


def upgrade() -> None:
    # CREATE OR REPLACE keeps each guard's OID, owner and trigger bindings; only
    # the fixed relation names, composite-type names, search_path and the
    # documented cross-owner security mode change.
    for statement in _statements(_FORWARD):
        _execute(statement)


def downgrade() -> None:
    # Restore the frozen pre-0381 definitions (unqualified names, caller
    # search_path, invoker security). CREATE OR REPLACE preserves each guard's
    # existing owner and ACL, so PUBLIC EXECUTE is never re-granted here and an
    # already-revoked deployment stays revoked. Trigger execution does not check
    # consumer EXECUTE, so fail-closed ACLs cannot disable the guards.
    for statement in _statements(_PREVIOUS):
        _execute(statement)
