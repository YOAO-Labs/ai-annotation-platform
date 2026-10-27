"""Move the administrator offboarding workflow into native Identity."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0248"
down_revision = "bo0247"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_ARTIFACTS = (
    "0248_identity_offboarding_support.sql",
    "0248_identity_offboarding.sql",
)
_STATEMENT_SEPARATOR = "-- aap-statement-break"
_FUNCTIONS = (
    "aap_identity_offboarding_v1.apply_offboarding_command(jsonb)",
    "aap_identity_offboarding_v1.prepare_offboarding_command(jsonb)",
    "aap_identity_offboarding_v1.offboarding_policy()",
    "aap_identity_offboarding_v1._has_exact_keys(jsonb,text[])",
    "aap_identity_offboarding_v1._failure(text,integer,jsonb)",
    "aap_workspace_v1.lock_identity_offboarding_scope(uuid,uuid[],uuid[])",
    "aap_workspace_v1.identity_offboarding_replay_scope(uuid,uuid[])",
    "aap_workspace_v1.identity_offboarding_facts(uuid)",
    "aap_workspace_v1.transfer_identity_offboarding_owners(uuid,jsonb)",
    "aap_labeling_v1.lock_identity_offboarding_work(uuid,uuid[])",
    "aap_labeling_v1.identity_offboarding_facts(uuid)",
    "aap_labeling_v1.transfer_identity_offboarding_work(uuid,jsonb)",
    "aap_labeling_v1.release_identity_offboarding_task_locks(uuid)",
    "aap_audit_v1.record_identity_offboarding_event(jsonb,uuid,jsonb,jsonb)",
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
                "SELECT count(*) FROM public.identity_offboarding_operation_receipts"
            )
        ).scalar_one()
    )
    if receipts:
        raise RuntimeError(
            "Cannot remove Identity offboarding ownership while receipts exist: "
            f"receipts={receipts}."
        )

    for signature in _FUNCTIONS:
        _execute(f"DROP FUNCTION IF EXISTS {signature} RESTRICT")
    op.drop_table("identity_offboarding_operation_receipts", schema="public")
    _execute("DROP SCHEMA aap_identity_offboarding_v1 RESTRICT")
