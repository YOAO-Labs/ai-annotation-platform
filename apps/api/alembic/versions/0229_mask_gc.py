"""Add native fencing and durable operations for Raster Mask content GC.

Revision ID: bo0229
Revises: bo0228
"""

from __future__ import annotations

import re
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0229"
down_revision = "bo0228"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_UPGRADE_ARTIFACTS = ("0229_mask_gc.sql",)
_STATEMENT_SEPARATOR = "-- aap-statement-break"

_ROOT_TRIGGERS = (
    ("trg_mask_gc_roots_annotations", "annotations"),
    ("trg_mask_gc_roots_predictions", "predictions"),
    ("trg_mask_gc_roots_uploads", "raster_mask_uploads"),
    ("trg_mask_gc_roots_tracker_jobs", "video_tracker_jobs"),
    ("trg_mask_gc_roots_accept_decisions", "ai_mask_accept_decisions"),
    ("trg_mask_gc_roots_revisions", "mask_annotation_revisions"),
    ("trg_mask_gc_roots_qc_runs", "mask_qc_runs"),
    ("trg_mask_gc_roots_qc_issues", "mask_qc_issues"),
    ("trg_mask_gc_roots_review_scopes", "mask_review_scopes"),
    ("trg_mask_gc_roots_tracker_scopes", "video_tracker_input_scopes"),
    ("trg_mask_gc_roots_repair_batches", "mask_repair_batches"),
    ("trg_mask_gc_tracker_jobs_retention", "video_tracker_jobs"),
    (
        "trg_mask_gc_tracker_pins_retention",
        "annotation_video_tracker_input_sources",
    ),
)

_DOWNGRADE_GUARDS = {
    "epochs": "SELECT count(*) FROM public.mask_gc_key_epochs",
    "intents": "SELECT count(*) FROM public.mask_gc_intents",
    "sweeps": "SELECT count(*) FROM public.mask_gc_sweeps",
    "roots": "SELECT count(*) FROM public.mask_gc_roots",
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


def _count(bind, query: str) -> int:
    return int(bind.execute(sa.text(query)).scalar_one())


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
    retained = {name: _count(bind, query) for name, query in _DOWNGRADE_GUARDS.items()}
    nonempty = {name: count for name, count in retained.items() if count}
    if nonempty:
        detail = ", ".join(f"{name}={count}" for name, count in nonempty.items())
        raise RuntimeError(
            "Cannot remove Raster Mask GC epochs, intents, sweeps or live roots: "
            f"{detail}."
        )

    for trigger_name, table_name in _ROOT_TRIGGERS:
        _execute(
            f"DROP TRIGGER IF EXISTS {trigger_name} ON public.{table_name} RESTRICT"
        )
    for signature in _new_function_signatures():
        _execute(f"DROP FUNCTION IF EXISTS {signature} RESTRICT")
    for table_name in (
        "mask_gc_roots",
        "mask_gc_write_permit_secrets",
        "mask_gc_sweeps",
        "mask_gc_key_epochs",
        "mask_gc_intents",
    ):
        op.drop_table(table_name, schema="public")
