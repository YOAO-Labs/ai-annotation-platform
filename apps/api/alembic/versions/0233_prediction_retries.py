"""Add durable failed-prediction retry authority and delivery outbox.

Revision ID: bo0233
Revises: bo0232
"""

from __future__ import annotations

import re
from pathlib import Path

import sqlalchemy as sa
from alembic import context, op

revision = "bo0233"
down_revision = "bo0232"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_UPGRADE_ARTIFACTS = (
    "0233_prediction_retry_state.sql",
    "0233_prediction_retry_result_scope.sql",
    "0233_prediction_retry_finalization.sql",
)
_STATEMENT_SEPARATOR = "-- aap-statement-break"
_RESTORED_FUNCTIONS = (
    "aap_jobs_v1._lock_batch_result_scope(jsonb, boolean, boolean)",
    "aap_jobs_v1._batch_result_scope_matches_member(jsonb, uuid, uuid, uuid, uuid)",
    "aap_jobs_v1.claim_batch_result_execution(jsonb)",
    "aap_jobs_v1.read_batch_result_execution(jsonb)",
    "aap_jobs_v1.batch_result_execution_referenced(jsonb)",
)
_DOWNGRADE_GUARDS = {
    "attempts": "SELECT count(*) FROM public.prediction_retry_attempts",
    "deliveries": "SELECT count(*) FROM public.prediction_retry_deliveries",
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


def _signature_key(signature: str) -> str:
    return re.sub(r"\s+", "", signature).lower()


def _function_signature(statement: str) -> str | None:
    match = re.search(
        r"(?is)^CREATE(?:\s+OR\s+REPLACE)?\s+FUNCTION\s+(.+?)\s*\(",
        statement,
    )
    if not match:
        return None
    start = match.start(1)
    open_paren = statement.find("(", start)
    depth = 0
    for index in range(open_paren, len(statement)):
        if statement[index] == "(":
            depth += 1
        elif statement[index] == ")":
            depth -= 1
            if depth == 0:
                return " ".join(statement[start : index + 1].split())
    return None


def _replacement_functions() -> list[str]:
    wanted = {signature.split("(", 1)[0].lower() for signature in _RESTORED_FUNCTIONS}
    found: dict[str, str] = {}
    for statement in _statements("0231_preannotation_results.sql"):
        signature = _function_signature(statement)
        if signature is None:
            continue
        function_name = signature.split("(", 1)[0].lower()
        if function_name not in wanted:
            continue
        found[function_name] = re.sub(
            r"(?is)^CREATE\s+FUNCTION\b",
            "CREATE OR REPLACE FUNCTION",
            statement,
            count=1,
        )
    missing = wanted - found.keys()
    if missing:
        raise RuntimeError(f"Cannot restore 0231 result functions: {sorted(missing)}")
    return [
        found[signature.split("(", 1)[0].lower()] for signature in _RESTORED_FUNCTIONS
    ]


def upgrade() -> None:
    if (
        context.get_x_argument(as_dictionary=True).get(
            "prediction_retry_legacy_drained"
        )
        == "true"
    ):
        _execute(
            "SELECT pg_catalog.set_config('aap.prediction_retry_legacy_drained','true',true)"
        )
    for filename in _UPGRADE_ARTIFACTS:
        for statement in _statements(filename):
            _execute(statement)


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
            "Cannot remove durable failed-prediction retry authority or history: "
            f"{detail}."
        )

    for statement in (
        "DROP TRIGGER IF EXISTS trg_async_jobs_prediction_retry_terminal "
        "ON public.async_jobs RESTRICT",
        "DROP TRIGGER IF EXISTS trg_async_jobs_prediction_retry_delete_guard "
        "ON public.async_jobs RESTRICT",
        "DROP TRIGGER IF EXISTS trg_batch_result_prediction_retry_execution_guard "
        "ON public.batch_result_executions RESTRICT",
        "DROP TRIGGER IF EXISTS trg_batch_result_prediction_retry_member_guard "
        "ON public.batch_result_members RESTRICT",
        "DROP TRIGGER IF EXISTS trg_batch_result_prediction_retry_bundle_guard "
        "ON public.batch_result_bundles RESTRICT",
    ):
        _execute(statement)
    for statement in _replacement_functions():
        _execute(statement)
    for filename in reversed(_UPGRADE_ARTIFACTS):
        for statement in reversed(_statements(filename)):
            match = re.search(
                r"(?is)^REVOKE\s+ALL\s+ON\s+FUNCTION\s+(.+?)\s+FROM\s+PUBLIC\s*;$",
                statement,
            )
            if match:
                signature = " ".join(match.group(1).split())
                if _signature_key(signature) in {
                    _signature_key(item) for item in _RESTORED_FUNCTIONS
                }:
                    continue
                _execute(f"DROP FUNCTION IF EXISTS {signature} RESTRICT")
    _execute("DROP TABLE public.prediction_retry_deliveries RESTRICT")
    _execute("DROP TABLE public.prediction_retry_attempts RESTRICT")
