"""Move administrator password resets to native Identity ownership."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0244"
down_revision = "bo0243"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_ARTIFACTS = (
    "0244_identity_admin_passwords_support.sql",
    "0244_identity_admin_passwords.sql",
)
_STATEMENT_SEPARATOR = "-- aap-statement-break"
_FUNCTIONS = (
    "aap_identity_admin_passwords_v1.execute_admin_password_command(jsonb)",
    "aap_identity_admin_passwords_v1.admin_password_policy()",
    "aap_identity_admin_passwords_v1._has_exact_keys(jsonb,text[])",
    "aap_identity_admin_passwords_v1._failure(text,integer,text)",
    "aap_workspace_v1.admin_password_target_in_owned_project(uuid,uuid)",
    "aap_audit_v1.record_identity_admin_password_reset(jsonb,uuid,jsonb,jsonb)",
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
    receipts = int(
        bind.execute(
            sa.text(
                "SELECT count(*) FROM public.identity_admin_password_reset_receipts"
            )
        ).scalar_one()
    )
    if receipts:
        raise RuntimeError(
            "Cannot remove Identity administrator password ownership while receipts "
            f"exist: receipts={receipts}."
        )

    for signature in _FUNCTIONS:
        _execute(f"DROP FUNCTION IF EXISTS {signature} RESTRICT")
    op.drop_table("identity_admin_password_reset_receipts", schema="public")
    _execute("DROP SCHEMA aap_identity_admin_passwords_v1 RESTRICT")
