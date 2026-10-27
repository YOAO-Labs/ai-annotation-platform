"""Move self-deactivation and due processing into native Identity ownership."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0241"
down_revision = "bo0240"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_ARTIFACTS = (
    "0241_deactivation_notifications.sql",
    "0241_identity_deactivation.sql",
)
_STATEMENT_SEPARATOR = "-- aap-statement-break"
_IDENTITY_FUNCTIONS = (
    "aap_identity_deactivation_v1.process_due_deactivations(jsonb)",
    "aap_identity_deactivation_v1.execute_deactivation_command(jsonb)",
    "aap_identity_deactivation_v1.due_policy()",
    "aap_identity_deactivation_v1.deactivation_policy()",
    "aap_identity_deactivation_v1._reconcile_request(uuid, timestamptz, timestamptz)",
    "aap_identity_deactivation_v1._actor(jsonb, text)",
    "aap_identity_deactivation_v1._account_record(uuid)",
    "aap_identity_deactivation_v1._strip_unicode_space(text)",
    "aap_identity_deactivation_v1._due_failure(text)",
    "aap_identity_deactivation_v1._failure(text, smallint, jsonb)",
    "aap_audit_v1.record_identity_deactivation_event(text, jsonb, jsonb, jsonb)",
    "aap_identity_v1.super_admin_status(uuid)",
    "aap_identity_v1.lock_super_admin_invariant()",
)
_NOTIFICATION_FUNCTIONS = (
    "aap_notifications_v1.finish_identity_delivery(jsonb)",
    "aap_notifications_v1.claim_identity_deliveries(jsonb)",
    "aap_notifications_v1.identity_delivery_policy()",
    "aap_notifications_v1.create_identity_deactivation_notifications(uuid, text, jsonb, uuid[])",
)


def _statements(artifact: str) -> list[str]:
    return [
        statement.strip()
        for statement in (_SQL_DIR / artifact)
        .read_text(encoding="utf-8")
        .split(_STATEMENT_SEPARATOR)
        if statement.strip()
    ]


def _execute(statement: str) -> None:
    op.execute(sa.text(statement.replace(":", r"\:")))


def upgrade() -> None:
    for artifact in _ARTIFACTS:
        for statement in _statements(artifact):
            _execute(statement)


def downgrade() -> None:
    bind = op.get_bind()
    counts = {
        "user_receipts": int(
            bind.execute(
                sa.text(
                    "SELECT count(*) FROM public.identity_deactivation_operation_receipts"
                )
            ).scalar_one()
        ),
        "due_receipts": int(
            bind.execute(
                sa.text(
                    "SELECT count(*) FROM public.identity_deactivation_due_receipts"
                )
            ).scalar_one()
        ),
        "native_requests": int(
            bind.execute(
                sa.text(
                    "SELECT count(*) FROM public.identity_deactivation_requests "
                    "WHERE origin = 'native'"
                )
            ).scalar_one()
        ),
        "delivery_outbox": int(
            bind.execute(
                sa.text("SELECT count(*) FROM public.notification_delivery_outbox")
            ).scalar_one()
        ),
    }
    if any(counts.values()):
        populated = ", ".join(
            f"{name}={count}" for name, count in counts.items() if count
        )
        raise RuntimeError(
            "Cannot remove Identity deactivation while retained results or "
            f"delivery state exists: {populated}."
        )

    for signature in _IDENTITY_FUNCTIONS:
        _execute(f"DROP FUNCTION IF EXISTS {signature} RESTRICT")
    op.drop_table("identity_deactivation_requests", schema="public")
    op.drop_table("identity_deactivation_due_receipts", schema="public")
    op.drop_table("identity_deactivation_operation_receipts", schema="public")
    _execute("DROP SCHEMA aap_identity_deactivation_v1 RESTRICT")

    for signature in _NOTIFICATION_FUNCTIONS:
        _execute(f"DROP FUNCTION IF EXISTS {signature} RESTRICT")
    op.drop_table("notification_delivery_outbox", schema="public")
