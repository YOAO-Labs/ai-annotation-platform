"""Move administrator directory reads to native Identity ownership."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0243"
down_revision = "bo0242"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_ARTIFACTS = (
    "0243_identity_directory_support.sql",
    "0243_identity_directory.sql",
)
_STATEMENT_SEPARATOR = "-- aap-statement-break"
_FUNCTIONS = (
    "aap_identity_directory_v1.execute_directory_command(jsonb)",
    "aap_identity_directory_v1.directory_policy()",
    "aap_identity_directory_v1._scope_user_ids(uuid,text,text,jsonb,boolean)",
    "aap_identity_directory_v1._directory_user(uuid)",
    "aap_identity_directory_v1._valid_payload(text,jsonb)",
    "aap_identity_directory_v1._strip_unicode_space(text)",
    "aap_identity_directory_v1._has_exact_keys(jsonb,text[])",
    "aap_identity_directory_v1._failure(text,integer,text)",
    "aap_audit_v1.record_identity_directory_export(jsonb,jsonb,jsonb)",
    "aap_workspace_v1.identity_directory_lifecycle_blocked(uuid,uuid[])",
    "aap_labeling_v1.identity_directory_project_refs(uuid[])",
    "aap_workspace_v1.identity_directory_project_member_ids(uuid)",
    "aap_workspace_v1.identity_directory_owned_member_ids(uuid)",
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
            sa.text("SELECT count(*) FROM public.identity_directory_export_receipts")
        ).scalar_one()
    )
    if receipts:
        raise RuntimeError(
            "Cannot remove Identity directory ownership while export receipts exist: "
            f"receipts={receipts}."
        )

    for signature in _FUNCTIONS:
        _execute(f"DROP FUNCTION IF EXISTS {signature} RESTRICT")
    op.drop_table("identity_directory_export_receipts", schema="public")
    _execute("DROP SCHEMA aap_identity_directory_v1 RESTRICT")
