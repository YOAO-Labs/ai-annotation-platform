"""Add native transactional ownership for identity group commands."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0240"
down_revision = "bo0239"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_ARTIFACT = "0240_identity_groups.sql"
_STATEMENT_SEPARATOR = "-- aap-statement-break"
_FUNCTIONS = (
    "aap_identity_groups_v1.execute_group_command(jsonb)",
    "aap_identity_groups_v1.group_policy()",
    "aap_identity_groups_v1.resolve_invitation_group(uuid, text)",
    "aap_identity_groups_v1._group_actor(jsonb, text)",
    "aap_identity_groups_v1._group_scope(uuid, uuid[], boolean)",
    "aap_identity_groups_v1._group_record(uuid)",
    "aap_identity_groups_v1._account_record(uuid)",
    "aap_identity_groups_v1._strip_unicode_space(text)",
    "aap_identity_groups_v1._failure(text, smallint, jsonb)",
    "aap_workspace_v1.lock_group_assignment_scope(uuid, uuid[], boolean)",
    "aap_audit_v1.record_identity_group_event(text, jsonb, jsonb, jsonb)",
)


def _statements() -> list[str]:
    return [
        statement.strip()
        for statement in (_SQL_DIR / _ARTIFACT)
        .read_text(encoding="utf-8")
        .split(_STATEMENT_SEPARATOR)
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
            sa.text("SELECT count(*) FROM public.identity_group_operation_receipts")
        ).scalar_one()
    )
    if receipts:
        raise RuntimeError(
            "Cannot remove Identity group operation receipts while retained state "
            f"exists: receipts={receipts}."
        )

    for signature in _FUNCTIONS:
        _execute(f"DROP FUNCTION IF EXISTS {signature} RESTRICT")
    op.drop_table("identity_group_operation_receipts", schema="public")
    _execute("DROP SCHEMA aap_identity_groups_v1 RESTRICT")
