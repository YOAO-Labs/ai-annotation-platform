"""Add native Identity recovery commands and a credential-mail outbox."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0239"
down_revision = "bo0238"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_ARTIFACT = "0239_identity_recovery.sql"
_STATEMENT_SEPARATOR = "-- aap-statement-break"
_FUNCTIONS = (
    "aap_identity_recovery_v1.apply_recovery_command(jsonb)",
    "aap_identity_recovery_v1.prepare_recovery_command(jsonb)",
    "aap_identity_recovery_v1.finish_mail(jsonb)",
    "aap_identity_recovery_v1.renew_mail(jsonb)",
    "aap_identity_recovery_v1.claim_mail(jsonb)",
    "aap_identity_recovery_v1.mail_policy()",
    "aap_identity_recovery_v1.recovery_policy()",
    "aap_identity_recovery_v1._token_record(text, uuid)",
    "aap_identity_recovery_v1._actor_record(uuid)",
    "aap_identity_recovery_v1._mail_failure(text)",
    "aap_identity_recovery_v1._failure(text)",
    "aap_audit_v1.record_identity_recovery_event(text, jsonb, jsonb, jsonb)",
    "aap_settings_v1.mail_delivery_policy(jsonb)",
    "aap_settings_v1.identity_recovery_policy(jsonb)",
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
    counts = {
        "password_reset_tokens": int(
            bind.execute(
                sa.text("SELECT count(*) FROM public.password_reset_tokens")
            ).scalar_one()
        ),
        "email_verification_tokens": int(
            bind.execute(
                sa.text("SELECT count(*) FROM public.email_verification_tokens")
            ).scalar_one()
        ),
        "receipts": int(
            bind.execute(
                sa.text(
                    "SELECT count(*) FROM public.identity_recovery_operation_receipts"
                )
            ).scalar_one()
        ),
        "outbox": int(
            bind.execute(
                sa.text("SELECT count(*) FROM public.identity_credential_mail_outbox")
            ).scalar_one()
        ),
    }
    if any(counts.values()):
        populated = ", ".join(
            f"{name}={count}" for name, count in counts.items() if count
        )
        raise RuntimeError(
            "Cannot remove Identity recovery while challenges or retained state exist: "
            f"{populated}."
        )

    for signature in _FUNCTIONS:
        _execute(f"DROP FUNCTION IF EXISTS {signature} RESTRICT")
    op.drop_table("identity_credential_mail_outbox", schema="public")
    op.drop_table("identity_recovery_operation_receipts", schema="public")
    op.drop_constraint(
        "ck_password_reset_tokens_hash_hex", "password_reset_tokens", schema="public"
    )
    op.drop_constraint(
        "ck_email_verification_tokens_hash_hex",
        "email_verification_tokens",
        schema="public",
    )
    op.drop_column("password_reset_tokens", "issued_email", schema="public")
    op.drop_column("email_verification_tokens", "issued_email", schema="public")
    op.alter_column(
        "password_reset_tokens", "token_hash", new_column_name="token", schema="public"
    )
    op.alter_column(
        "email_verification_tokens",
        "token_hash",
        new_column_name="token",
        schema="public",
    )
    _execute(
        "ALTER INDEX public.ix_password_reset_tokens_token_hash "
        "RENAME TO ix_password_reset_tokens_token"
    )
    _execute(
        "ALTER INDEX public.ix_email_verification_tokens_token_hash "
        "RENAME TO ix_email_verification_tokens_token"
    )
    _execute("DROP SCHEMA aap_identity_recovery_v1 RESTRICT")
