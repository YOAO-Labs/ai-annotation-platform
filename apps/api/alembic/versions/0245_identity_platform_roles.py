"""Move platform-role preview and mutation to native Identity ownership."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0245"
down_revision = "bo0244"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_ARTIFACTS = (
    "0245_identity_platform_roles_support.sql",
    "0245_identity_platform_roles.sql",
)
_STATEMENT_SEPARATOR = "-- aap-statement-break"
_FUNCTIONS = (
    "aap_identity_platform_roles_v1.execute_platform_role_command(jsonb)",
    "aap_identity_platform_roles_v1.platform_role_policy()",
    "aap_identity_platform_roles_v1._preview_snapshot(uuid)",
    "aap_identity_platform_roles_v1._has_exact_keys(jsonb,text[])",
    "aap_identity_platform_roles_v1._failure(text,integer,jsonb)",
    "aap_workspace_v1.identity_platform_role_facts(uuid)",
    "aap_workspace_v1.lock_identity_platform_role_owned_projects(uuid)",
    "aap_labeling_v1.identity_platform_role_facts(uuid,uuid[])",
    "aap_audit_v1.record_identity_platform_role_change(jsonb,uuid,jsonb,text,text,text)",
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
                "SELECT count(*) FROM public.identity_platform_role_operation_receipts"
            )
        ).scalar_one()
    )
    if receipts:
        raise RuntimeError(
            "Cannot remove Identity platform-role ownership while receipts exist: "
            f"receipts={receipts}."
        )

    for signature in _FUNCTIONS:
        _execute(f"DROP FUNCTION IF EXISTS {signature} RESTRICT")
    op.drop_table("identity_platform_role_operation_receipts", schema="public")
    _execute("DROP SCHEMA aap_identity_platform_roles_v1 RESTRICT")
