"""Avoid waiting on Workspace bindings while registry topology locks are held."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0275"
down_revision = "bo0274"
branch_labels = depends_on = None
_SQL = Path(__file__).resolve().parents[1] / "sql"


def _apply(name: str, *, replace: bool = False, function: str | None = None) -> None:
    source = (_SQL / name).read_text(encoding="utf-8")
    for statement in source.split("-- aap-statement-break"):
        if function and not statement.strip().startswith(
            f"CREATE FUNCTION {function}("
        ):
            continue
        if replace:
            statement = statement.replace(
                "CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1
            )
        if statement.strip():
            op.execute(sa.text(statement.strip().replace(":", r"\:")))


def upgrade() -> None:
    _apply("0275_workspace_binding_lock_guard.sql")


def downgrade() -> None:
    _apply("0272_workspace_backend_bindings.sql", replace=True)
    _apply(
        "0255_workspace_configuration_dependencies.sql",
        replace=True,
        function="aap_inference_v1.enable_workspace_project_backend",
    )
