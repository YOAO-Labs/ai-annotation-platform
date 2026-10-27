"""Add native transactional ownership for Identity self-account commands."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0238"
down_revision = "bo0237"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_ARTIFACT = "0238_identity_accounts.sql"
_STATEMENT_SEPARATOR = "-- aap-statement-break"
_FUNCTIONS = (
    "aap_identity_accounts_v1.apply_account_command(jsonb)",
    "aap_identity_accounts_v1.prepare_account_command(jsonb)",
    "aap_identity_accounts_v1.mark_inactive_accounts_offline(timestamptz)",
    "aap_identity_accounts_v1.account_policy()",
    "aap_identity_accounts_v1._lock_current_actor(jsonb, text, text)",
    "aap_identity_accounts_v1._account_record(uuid)",
    "aap_identity_accounts_v1._failure(text, smallint, jsonb)",
    "aap_audit_v1.record_identity_account_event(text, jsonb, jsonb, jsonb)",
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
            sa.text("SELECT count(*) FROM public.identity_account_operation_receipts")
        ).scalar_one()
    )
    if receipts:
        raise RuntimeError(
            "Cannot remove Identity account operation receipts while retained state "
            f"exists: receipts={receipts}."
        )

    for signature in _FUNCTIONS:
        _execute(f"DROP FUNCTION IF EXISTS {signature} RESTRICT")
    op.drop_table("identity_account_operation_receipts", schema="public")
    _execute("DROP SCHEMA aap_identity_accounts_v1 RESTRICT")
