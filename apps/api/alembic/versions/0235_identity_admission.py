"""Add durable Identity admission and revocation capabilities."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0235"
down_revision = "bo0234"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_ARTIFACT = "0235_identity_admission.sql"
_STATEMENT_SEPARATOR = "-- aap-statement-break"
_FUNCTIONS = (
    "aap_identity_v1.purge_expired_revocations(integer)",
    "aap_identity_v1.lock_current_principal(uuid, text, uuid, bigint, timestamptz, text[])",
    "aap_identity_v1.retire_account_credentials(uuid)",
    "aap_identity_v1.revoke_jti(uuid, timestamptz)",
    "aap_identity_v1.advance_credential_generation(uuid)",
    "aap_identity_v1.get_credential_generation(uuid)",
    "aap_identity_v1.confirm_api_key_principal(uuid, text)",
    "aap_identity_v1.read_api_key_candidates(text, uuid, integer)",
    "aap_identity_v1.read_jwt_principal(uuid, uuid, bigint, timestamptz, boolean)",
    "aap_identity_v1.is_jti_revoked(uuid)",
    "aap_identity_v1.authentication_policy()",
    "aap_identity_v1._admission_failure(text)",
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
    users = int(bind.execute(sa.text("SELECT count(*) FROM public.users")).scalar_one())
    revoked = int(
        bind.execute(
            sa.text("SELECT count(*) FROM public.identity_revoked_jtis")
        ).scalar_one()
    )
    if users or revoked:
        raise RuntimeError(
            "Cannot remove credential generation or durable revocation authority "
            f"while retained state exists: users={users}, revoked_jtis={revoked}."
        )

    for signature in _FUNCTIONS:
        _execute(f"DROP FUNCTION IF EXISTS {signature} RESTRICT")
    _execute("DROP TABLE public.identity_revoked_jtis RESTRICT")
    _execute("ALTER TABLE public.users DROP COLUMN credential_generation RESTRICT")
