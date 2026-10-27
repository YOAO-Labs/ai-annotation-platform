"""Add durable ordinary batch prediction result authority.

Revision ID: bo0231
Revises: bo0230
"""

from __future__ import annotations

import re
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0231"
down_revision = "bo0230"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_UPGRADE_ARTIFACTS = (
    "0231_preannotation_results.sql",
    "0231_preannotation_result_mask_roots.sql",
)
_STATEMENT_SEPARATOR = "-- aap-statement-break"
_MASK_GC_ROOT_LIVENESS = (
    "aap_labeling_v1._mask_gc_root_entry_live(text, text, timestamptz, boolean)"
)

_DOWNGRADE_GUARDS = {
    "executions": "SELECT count(*) FROM public.batch_result_executions",
    "members": "SELECT count(*) FROM public.batch_result_members",
    "bundles": "SELECT count(*) FROM public.batch_result_bundles",
    "payloads": "SELECT count(*) FROM public.batch_result_payloads",
    "parts": "SELECT count(*) FROM public.batch_result_parts",
}


def _statements(filename: str) -> list[str]:
    return [
        statement.strip()
        for statement in (_SQL_DIR / filename)
        .read_text(encoding="utf-8")
        .split(_STATEMENT_SEPARATOR)
        if statement.strip()
    ]


def _execute(statement: str) -> None:
    op.execute(sa.text(statement.replace(":", r"\:")))


def _function_signature(statement: str) -> str | None:
    match = re.search(
        r"(?is)\bREVOKE\s+ALL\s+ON\s+FUNCTION\s+(.+?)\s+FROM\s+PUBLIC\s*;\s*$",
        statement,
    )
    return " ".join(match.group(1).split()) if match else None


def _function_name(signature: str) -> str:
    return signature.split("(", 1)[0].strip().lower()


def _signature_key(signature: str) -> str:
    return re.sub(r"\s+", "", signature).lower()


def _previous_mask_gc_root_liveness_statements() -> list[str]:
    signature = _MASK_GC_ROOT_LIVENESS
    create_statement = None
    revoke_statement = None
    for statement in _statements("0229_mask_gc.sql"):
        if re.match(
            r"(?is)^CREATE\s+FUNCTION\s+"
            r"aap_labeling_v1[.]_mask_gc_root_entry_live\s*\(",
            statement,
        ):
            create_statement = re.sub(
                r"(?is)^CREATE\s+FUNCTION\b",
                "CREATE OR REPLACE FUNCTION",
                statement,
                count=1,
            )
        found_signature = _function_signature(statement)
        if found_signature and _signature_key(found_signature) == _signature_key(
            signature
        ):
            revoke_statement = statement
    if create_statement is None or revoke_statement is None:
        raise RuntimeError("Cannot restore 0229 Mask GC root liveness behavior.")
    return [create_statement, revoke_statement]


def _restore_previous_mask_gc_root_liveness() -> None:
    for statement in _previous_mask_gc_root_liveness_statements():
        _execute(statement)


def _new_function_signatures() -> list[str]:
    signatures: list[str] = []
    for filename in reversed(_UPGRADE_ARTIFACTS):
        for statement in reversed(_statements(filename)):
            signature = _function_signature(statement)
            if signature:
                signatures.append(signature)
    return signatures


def _run_artifact(filename: str) -> None:
    for statement in _statements(filename):
        _execute(statement)


def upgrade() -> None:
    for filename in _UPGRADE_ARTIFACTS:
        _run_artifact(filename)


def downgrade() -> None:
    bind = op.get_bind()
    retained = {
        name: int(bind.execute(sa.text(query)).scalar_one())
        for name, query in _DOWNGRADE_GUARDS.items()
    }
    nonempty = {name: count for name, count in retained.items() if count}
    if nonempty:
        detail = ", ".join(f"{name}={count}" for name, count in nonempty.items())
        raise RuntimeError(
            "Cannot remove durable preannotation result execution or receipts: "
            f"{detail}."
        )

    _execute(
        "DROP TRIGGER IF EXISTS trg_batch_result_payload_mask_roots "
        "ON public.batch_result_payloads RESTRICT"
    )
    _execute(
        "DROP TRIGGER IF EXISTS trg_async_jobs_batch_result_terminal "
        "ON public.async_jobs RESTRICT"
    )
    _execute(
        "DROP TRIGGER IF EXISTS trg_async_jobs_batch_result_delete_guard "
        "ON public.async_jobs RESTRICT"
    )
    _execute(
        "DELETE FROM public.mask_gc_roots WHERE root_kind = 'batch_result_payloads'"
    )
    _restore_previous_mask_gc_root_liveness()
    for signature in _new_function_signatures():
        if _signature_key(signature) == _signature_key(_MASK_GC_ROOT_LIVENESS):
            continue
        _execute(f"DROP FUNCTION IF EXISTS {signature} RESTRICT")
    for table_name in (
        "batch_result_parts",
        "batch_result_payloads",
        "batch_result_bundles",
        "batch_result_members",
        "batch_result_executions",
    ):
        op.drop_table(table_name, schema="public")
