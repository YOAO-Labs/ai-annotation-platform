"""Retain generated tracker Masks while their Job is running.

Revision ID: bo0232
Revises: bo0231
"""

from __future__ import annotations

import re
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0232"
down_revision = "bo0231"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_ARTIFACT = "0232_tracker_running_mask_roots.sql"
_STATEMENT_SEPARATOR = "-- aap-statement-break"
_MASK_GC_ROOT_LIVENESS = (
    "aap_labeling_v1._mask_gc_root_entry_live(text, text, timestamptz, boolean)"
)
_DOWNGRADE_GUARD = (
    "SELECT count(*) FROM public.mask_gc_roots "
    "WHERE root_kind = 'video_tracker_running_outputs'"
)


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


def _signature_key(signature: str) -> str:
    return re.sub(r"\s+", "", signature).lower()


def _previous_mask_gc_root_liveness_statements() -> list[str]:
    create_statement = None
    revoke_statement = None
    for statement in _statements("0231_preannotation_result_mask_roots.sql"):
        if re.search(
            r"(?is)\bCREATE\s+OR\s+REPLACE\s+FUNCTION\s+"
            r"aap_labeling_v1[.]_mask_gc_root_entry_live\s*\(",
            statement,
        ):
            create_statement = re.sub(
                r"(?is)^.*?(?=CREATE\s+OR\s+REPLACE\s+FUNCTION)",
                "",
                statement,
                count=1,
            )
        signature = _function_signature(statement)
        if signature and _signature_key(signature) == _signature_key(
            _MASK_GC_ROOT_LIVENESS
        ):
            revoke_statement = statement
    if create_statement is None or revoke_statement is None:
        raise RuntimeError("Cannot restore 0231 Mask GC root liveness behavior.")
    return [create_statement, revoke_statement]


def upgrade() -> None:
    for statement in _statements(_ARTIFACT):
        _execute(statement)


def downgrade() -> None:
    bind = op.get_bind()
    retained = int(bind.execute(sa.text(_DOWNGRADE_GUARD)).scalar_one())
    if retained:
        raise RuntimeError(
            "Cannot remove tracker running-output Mask roots while roots remain: "
            f"{retained}."
        )

    _execute(
        "DROP TRIGGER IF EXISTS trg_mask_gc_release_tracker_running_outputs "
        "ON public.video_tracker_jobs RESTRICT"
    )
    for statement in _previous_mask_gc_root_liveness_statements():
        _execute(statement)
    _execute(
        "DROP FUNCTION IF EXISTS "
        "aap_labeling_v1.register_tracker_running_mask_root(jsonb) RESTRICT"
    )
    _execute(
        "DROP FUNCTION IF EXISTS "
        "aap_labeling_v1._mask_gc_release_tracker_running_outputs() RESTRICT"
    )
