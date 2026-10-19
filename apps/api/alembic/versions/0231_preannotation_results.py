"""Add durable ordinary batch prediction result authority.

Revision ID: 0231
Revises: 0230
"""

from __future__ import annotations

import re
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0231"
down_revision = "0230"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_UPGRADE_ARTIFACTS = ("0231_preannotation_results.sql",)
_STATEMENT_SEPARATOR = "-- aap-statement-break"

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
    for signature in _new_function_signatures():
        _execute(f"DROP FUNCTION IF EXISTS {signature} RESTRICT")
    for table_name in (
        "batch_result_parts",
        "batch_result_payloads",
        "batch_result_bundles",
        "batch_result_members",
        "batch_result_executions",
    ):
        op.drop_table(table_name, schema="public")
