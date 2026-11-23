"""Database-backed Mask AI inventory metrics for the API scrape process."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text

from app.observability.metrics import (
    MASK_AI_ACCEPT_DECISIONS,
    MASK_AI_CORRECTION_JOBS,
    MASK_AI_CORRECTION_OLDEST_AGE_SECONDS,
    MASK_AI_OLDEST_EXPIRED_DECISION_AGE_SECONDS,
    MASK_AI_STAGED_MASK_REFERENCES,
)

log = logging.getLogger(__name__)

CORRECTION_STATUSES = (
    "queued",
    "running",
    "pending_review",
    "partially_reviewed",
    "accepted",
    "discarded",
    "failed",
    "cancelled",
)
ACTIVE_CORRECTION_STATUSES = (
    "queued",
    "running",
    "pending_review",
    "partially_reviewed",
)
TRACKER_JOB_KINDS = ("tracking", "correction")


async def refresh_mask_ai_inventory(db: Any) -> dict[str, Any]:
    inventory = await db.scalar(text("SELECT yoao_labeling_v1.mask_ai_inventory()"))
    correction_counts = inventory["correction_jobs"]
    correction_ages = inventory["correction_oldest_age_seconds"]
    staged_counts = inventory["staged_mask_references"]
    decision_counts = inventory["accept_decisions"]
    oldest_expired_age = inventory["oldest_expired_decision_age_seconds"]

    for status in CORRECTION_STATUSES:
        MASK_AI_CORRECTION_JOBS.labels(status=status).set(
            correction_counts.get(status, 0)
        )
    for status in ACTIVE_CORRECTION_STATUSES:
        MASK_AI_CORRECTION_OLDEST_AGE_SECONDS.labels(status=status).set(
            correction_ages.get(status, 0.0)
        )
    for job_kind in TRACKER_JOB_KINDS:
        MASK_AI_STAGED_MASK_REFERENCES.labels(job_kind=job_kind).set(
            staged_counts.get(job_kind, 0)
        )
    for state in ("active", "expired"):
        MASK_AI_ACCEPT_DECISIONS.labels(state=state).set(decision_counts[state])
    MASK_AI_OLDEST_EXPIRED_DECISION_AGE_SECONDS.set(oldest_expired_age)

    return {
        "correction_jobs": correction_counts,
        "correction_oldest_age_seconds": correction_ages,
        "staged_mask_references": staged_counts,
        "accept_decisions": decision_counts,
        "oldest_expired_decision_age_seconds": oldest_expired_age,
    }


async def refresh_mask_ai_inventory_safely(db: Any) -> dict[str, Any] | None:
    try:
        return await refresh_mask_ai_inventory(db)
    except Exception as exc:  # noqa: BLE001 - stale metrics must not break scrape
        try:
            await db.rollback()
        except Exception as rollback_exc:  # noqa: BLE001
            log.warning("rollback after Mask AI metric query failed: %s", rollback_exc)
        log.warning("refresh Mask AI inventory metric failed: %s", exc)
        return None


__all__ = [
    "ACTIVE_CORRECTION_STATUSES",
    "CORRECTION_STATUSES",
    "TRACKER_JOB_KINDS",
    "refresh_mask_ai_inventory",
    "refresh_mask_ai_inventory_safely",
]
