"""Move the complete invitation lifecycle into native Identity ownership."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0242"
down_revision = "bo0241"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_ARTIFACTS = (
    "0242_identity_invitations_support.sql",
    "0242_invitation_mail.sql",
    "0242_identity_invitations.sql",
)
_STATEMENT_SEPARATOR = "-- aap-statement-break"

_FUNCTIONS = (
    "aap_identity_invitations_v1.apply_invitation_command(jsonb)",
    "aap_identity_invitations_v1.prepare_invitation_command(jsonb)",
    "aap_identity_invitations_v1.invitation_policy()",
    "aap_identity_invitations_v1.invitation_quota_reached(uuid, timestamptz, integer)",
    "aap_identity_invitations_v1.lock_invitation_mail_scope(uuid, text)",
    "aap_identity_invitations_v1._failure(text, text, text)",
    "aap_identity_invitations_v1._strip_unicode_space(text)",
    "aap_identity_invitations_v1._account_record(uuid)",
    "aap_identity_invitations_v1._invitation_record(uuid, text)",
    "aap_identity_invitations_v1._project_map(uuid[])",
    "aap_identity_invitations_v1._policy(jsonb)",
    "aap_identity_invitations_v1._lock_users(uuid[])",
    "aap_identity_invitations_v1._actor(jsonb, text, boolean, boolean)",
    "aap_identity_invitations_v1._empty_result()",
    "aap_identity_invitations_v1._incompatible_role_detail(text, text)",
    "aap_identity_invitations_v1._args_valid(text, jsonb)",
    "aap_identity_invitations_v1._read_result(text, uuid, jsonb)",
    "aap_identity_invitations_v1._native_actor(uuid)",
    "aap_identity_invitations_v1._project_record(uuid)",
    "aap_identity_invitations_v1._batch_snapshot(uuid, jsonb)",
    "aap_identity_invitations_v1._batch_effects(boolean, uuid, text, text, jsonb, jsonb, jsonb, jsonb, jsonb)",
    "aap_identity_invitations_v1._run_invitation_preview(uuid, text, text, jsonb, jsonb, jsonb, jsonb)",
    "aap_workspace_v1.invitation_projects(uuid[])",
    "aap_workspace_v1.lock_invitation_project(uuid, uuid)",
    "aap_workspace_v1.read_invitation_membership(uuid, uuid)",
    "aap_workspace_v1.grant_invitation_membership(uuid, uuid, text, uuid, uuid)",
    "aap_labeling_v1.invitation_work_summary(uuid, uuid, text)",
    "aap_settings_v1.invitation_policy(jsonb)",
    "aap_audit_v1.record_identity_invitation_event(text, jsonb, jsonb, jsonb, jsonb)",
)
_MAIL_V2_FUNCTIONS = (
    "aap_identity_mail_v2.finish_mail(jsonb)",
    "aap_identity_mail_v2.renew_mail(jsonb)",
    "aap_identity_mail_v2.claim_mail(jsonb)",
    "aap_identity_mail_v2.mail_policy()",
)
_MAIL_V1_FUNCTIONS = (
    "aap_identity_recovery_v1.finish_mail(jsonb)",
    "aap_identity_recovery_v1.renew_mail(jsonb)",
    "aap_identity_recovery_v1.claim_mail(jsonb)",
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


def _restore_frozen_recovery_functions() -> None:
    """Restore v1 mail RPCs without editing their frozen 0239 source artifact."""
    frozen = _statements("0239_identity_recovery.sql")
    for signature in _MAIL_V1_FUNCTIONS:
        name = signature.split("(", maxsplit=1)[0]
        create_statement = next(
            statement
            for statement in frozen
            if statement.startswith(f"CREATE FUNCTION {name}(")
        )
        revoke_statement = next(
            statement
            for statement in frozen
            if statement.startswith(f"REVOKE ALL ON FUNCTION {signature}")
        )
        _execute(
            create_statement.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)
        )
        _execute(revoke_statement)


def downgrade() -> None:
    bind = op.get_bind()
    receipts = int(
        bind.execute(
            sa.text(
                "SELECT count(*) FROM public.identity_invitation_operation_receipts"
            )
        ).scalar_one()
    )
    invitation_mail = int(
        bind.execute(
            sa.text(
                "SELECT count(*) FROM public.identity_credential_mail_outbox "
                "WHERE purpose = 'invitation'"
            )
        ).scalar_one()
    )
    if receipts or invitation_mail:
        raise RuntimeError(
            "Cannot remove Identity invitation ownership while receipts or "
            f"durable mail state exists: receipts={receipts}, mail={invitation_mail}."
        )

    for signature in _MAIL_V2_FUNCTIONS:
        _execute(f"DROP FUNCTION IF EXISTS {signature} RESTRICT")
    _execute("DROP SCHEMA aap_identity_mail_v2 RESTRICT")
    for signature in _FUNCTIONS:
        _execute(f"DROP FUNCTION IF EXISTS {signature} RESTRICT")
    op.drop_table("identity_invitation_operation_receipts", schema="public")
    _execute("DROP SCHEMA aap_identity_invitations_v1 RESTRICT")

    # The Mail artifact adds a purpose and nullable invitation fields to the
    # frozen credential-mail queue. Restore its original recovery-only shape.
    _execute(
        "ALTER TABLE public.identity_credential_mail_outbox "
        "DROP CONSTRAINT ck_identity_credential_mail_purpose_v2, "
        "DROP CONSTRAINT ck_identity_credential_mail_purpose_shape_v2, "
        "DROP CONSTRAINT ck_identity_credential_mail_invitation_context_v2, "
        "DROP CONSTRAINT fk_identity_credential_mail_invitation"
    )
    _execute("DROP INDEX IF EXISTS public.ix_identity_credential_mail_invitation")
    _execute(
        "ALTER TABLE public.identity_credential_mail_outbox "
        "DROP COLUMN invitation_context, DROP COLUMN invitation_id"
    )
    _execute(
        "ALTER TABLE public.identity_credential_mail_outbox "
        "ALTER COLUMN challenge_id SET NOT NULL"
    )
    _execute(
        "ALTER TABLE public.identity_credential_mail_outbox "
        "ADD CONSTRAINT identity_credential_mail_outbox_purpose_check "
        "CHECK (purpose IN ('password_reset','email_verification'))"
    )
    _restore_frozen_recovery_functions()
