"""Move Workspace project membership writes behind the Workspace owner."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0253"
down_revision = "bo0252"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_ARTIFACTS = (
    "0253_workspace_membership_labeling.sql",
    "0253_workspace_membership_support.sql",
    "0253_workspace_membership.sql",
)
_STATEMENT_SEPARATOR = "-- aap-statement-break"
_FUNCTIONS = (
    "aap_workspace_membership_v1.apply_membership_command(jsonb)",
    "aap_workspace_membership_v1.prepare_membership_command(jsonb)",
    "aap_workspace_membership_v1.membership_policy()",
    "aap_workspace_membership_v1._request_is_valid(jsonb)",
    "aap_workspace_membership_v1._has_exact_keys(jsonb,text[])",
    "aap_workspace_membership_v1._membership_record(uuid,uuid)",
    "aap_workspace_membership_v1._failure(integer,jsonb,jsonb,boolean)",
    "aap_labeling_v1.release_membership_task_locks(uuid,uuid)",
    "aap_labeling_v1.handoff_membership_reviewer(uuid,uuid,uuid)",
    "aap_labeling_v1.handoff_membership_annotation(uuid,uuid,uuid)",
    "aap_labeling_v1._membership_python_repr(jsonb)",
    "aap_labeling_v1.lock_membership_work(uuid,uuid,text,boolean)",
    "aap_labeling_v1.read_membership_work(uuid,uuid,uuid,uuid)",
    "aap_audit_v1.record_workspace_membership_event(text,jsonb,uuid,jsonb,jsonb)",
    "aap_identity_v1.read_workspace_membership_accounts(uuid[])",
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
                "SELECT count(*) FROM public.workspace_membership_operation_receipts"
            )
        ).scalar_one()
    )
    if receipts:
        raise RuntimeError(
            "Cannot remove Workspace membership ownership while receipts exist: "
            f"receipts={receipts}."
        )

    for signature in _FUNCTIONS:
        _execute(f"DROP FUNCTION IF EXISTS {signature} RESTRICT")
    op.drop_table("workspace_membership_operation_receipts", schema="public")
    _execute("DROP SCHEMA aap_workspace_membership_v1 RESTRICT")
