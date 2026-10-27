"""Move administrator account deletion into native Identity."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0247"
down_revision = "bo0246"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_ARTIFACTS = (
    "0247_identity_deletions_support.sql",
    "0247_identity_deletions.sql",
)
_STATEMENT_SEPARATOR = "-- aap-statement-break"
_FUNCTIONS = (
    "aap_identity_deletions_v1.execute_deletion_command(jsonb)",
    "aap_identity_deletions_v1.deletion_policy()",
    "aap_identity_deletions_v1._has_exact_keys(jsonb,text[])",
    "aap_identity_deletions_v1._failure(text,integer,jsonb)",
    "aap_workspace_v1.lock_identity_deletion_scope(uuid,uuid)",
    "aap_workspace_v1.identity_deletion_transfer_covered(uuid,uuid[])",
    "aap_labeling_v1.transfer_identity_deletion_work(uuid,uuid)",
    "aap_labeling_v1.identity_deletion_work(uuid)",
    "aap_audit_v1.record_identity_deletion_event(jsonb,uuid,jsonb,jsonb)",
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
            sa.text("SELECT count(*) FROM public.identity_deletion_operation_receipts")
        ).scalar_one()
    )
    if receipts:
        raise RuntimeError(
            "Cannot remove Identity deletion ownership while receipts exist: "
            f"receipts={receipts}."
        )

    for signature in _FUNCTIONS:
        _execute(f"DROP FUNCTION IF EXISTS {signature} RESTRICT")
    op.drop_table("identity_deletion_operation_receipts", schema="public")
    _execute("DROP SCHEMA aap_identity_deletions_v1 RESTRICT")
