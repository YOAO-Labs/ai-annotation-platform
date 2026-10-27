"""Move direct project-owner transfer behind the Workspace owner."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0254"
down_revision = "bo0253"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1] / "sql" / "0254_workspace_owner_transfer.sql"
)
_STATEMENT_SEPARATOR = "-- aap-statement-break"
_FUNCTIONS = (
    "aap_workspace_owner_transfer_v1.transfer_project_owner(jsonb)",
    "aap_workspace_owner_transfer_v1.owner_transfer_policy()",
    "aap_workspace_owner_transfer_v1._failure(integer,jsonb,boolean)",
)


def _statements() -> list[str]:
    return [
        statement.strip()
        for statement in _SQL_PATH.read_text(encoding="utf-8").split(
            _STATEMENT_SEPARATOR
        )
        if statement.strip()
    ]


def _execute(statement: str) -> None:
    op.execute(sa.text(statement.replace(":", r"\:")))


def upgrade() -> None:
    for statement in _statements():
        _execute(statement)


def downgrade() -> None:
    bind = op.get_bind()
    receipts = int(
        bind.execute(
            sa.text(
                "SELECT count(*) "
                "FROM public.workspace_owner_transfer_operation_receipts"
            )
        ).scalar_one()
    )
    if receipts:
        raise RuntimeError(
            "Cannot remove Workspace owner transfer while receipts exist: "
            f"receipts={receipts}."
        )

    for signature in _FUNCTIONS:
        _execute(f"DROP FUNCTION IF EXISTS {signature} RESTRICT")
    op.drop_table("workspace_owner_transfer_operation_receipts", schema="public")
    _execute("DROP SCHEMA aap_workspace_owner_transfer_v1 RESTRICT")
