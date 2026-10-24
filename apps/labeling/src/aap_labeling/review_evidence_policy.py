"""Pure policies for task review and contributor evidence."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import datetime
from typing import Protocol


class ReviewEvidenceTask(Protocol):
    review_round_id: uuid.UUID | None
    annotation_contributor_ids: object | None
    review_contributor_ids: object | None
    review_submitter_id: uuid.UUID | None
    first_review_eligible: bool | None
    first_reviewed_at: datetime | None
    first_review_result: str | None
    first_review_contributor_ids: object | None


class ReviewEvidenceUnknown(ValueError):
    pass


class ReviewActorIneligible(ValueError):
    pass


def start_review_round(task: ReviewEvidenceTask) -> uuid.UUID:
    """Create the stable identifier shared by one submit and its review."""
    task.review_round_id = uuid.uuid4()
    return task.review_round_id


def ensure_review_round(task: ReviewEvidenceTask) -> uuid.UUID:
    """Return a round id, assigning one for a legacy task when needed."""
    if task.review_round_id is None:
        task.review_round_id = uuid.uuid4()
    return task.review_round_id


def capture_first_review_contributor_snapshot(
    task: ReviewEvidenceTask, contributor_ids: Iterable[str]
) -> None:
    """Persist the first-round contributors before audit retention can run."""
    if task.first_review_eligible is not True or task.first_reviewed_at is not None:
        return
    task.first_review_contributor_ids = sorted(
        {str(value) for value in contributor_ids if value}
    )


def record_first_review_fact(
    task: ReviewEvidenceTask,
    *,
    reviewed_at: datetime,
    result: str,
    contributor_ids: Iterable[str],
) -> bool:
    """Write the first completed review fact once for eligible new tasks."""
    if task.first_review_eligible is not True or task.first_reviewed_at is not None:
        return False
    task.first_reviewed_at = reviewed_at
    task.first_review_result = result
    task.first_review_contributor_ids = sorted(
        {str(value) for value in contributor_ids if value}
    )
    return True


def canonical_actor_id(value: object) -> str | None:
    """Return a canonical UUID string, or ``None`` for unusable evidence."""
    if isinstance(value, uuid.UUID):
        return str(value)
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        return None


def canonical_actor_set(values: object) -> set[str] | None:
    """Canonicalize a stored accumulator, or retain unknown when malformed."""
    if not isinstance(values, list):
        return None
    canonical: set[str] = set()
    for value in values:
        parsed = canonical_actor_id(value)
        if parsed is None:
            return None
        canonical.add(parsed)
    return canonical


def known_annotation_contributor_ids(task: ReviewEvidenceTask) -> set[str] | None:
    return canonical_actor_set(task.annotation_contributor_ids)


def record_annotation_actor_policy(task: ReviewEvidenceTask, actor_id: object) -> bool:
    """Apply the pure sticky-unknown accumulator rule to an in-memory value."""
    actor = canonical_actor_id(actor_id)
    if actor is None or getattr(task, "status", None) == "review":
        return False
    current = known_annotation_contributor_ids(task)
    if current is None or actor in current:
        return False
    task.annotation_contributor_ids = sorted(current | {actor})
    return True


def freeze_review_contributor_evidence(
    task: ReviewEvidenceTask,
    *,
    submitter_id: uuid.UUID | None,
    contributor_ids: Iterable[str] = (),
) -> list[str] | None:
    """Freeze the current round without manufacturing complete legacy evidence."""
    submitter = canonical_actor_id(submitter_id)
    task.review_submitter_id = submitter
    if submitter_id is not None and submitter is None:
        task.review_contributor_ids = None
        return None
    accumulator = known_annotation_contributor_ids(task)
    if accumulator is None:
        task.review_contributor_ids = None
        return None
    frozen = set(accumulator)
    for value in contributor_ids:
        parsed = canonical_actor_id(value)
        if parsed is None:
            task.review_contributor_ids = None
            return None
        frozen.add(parsed)
    if submitter is not None:
        frozen.add(submitter)
    task.review_contributor_ids = sorted(frozen)
    return task.review_contributor_ids


def clear_review_contributor_evidence(task: ReviewEvidenceTask) -> None:
    """Invalidate only the current round, retaining historical contributors."""
    task.review_contributor_ids = None
    task.review_submitter_id = None


def current_review_evidence(
    task: ReviewEvidenceTask,
) -> tuple[list[str], str] | None:
    if task.review_round_id is None:
        return None
    accumulated = known_annotation_contributor_ids(task)
    if accumulated is None:
        return None
    contributors = canonical_actor_set(task.review_contributor_ids)
    submitter = canonical_actor_id(task.review_submitter_id)
    if not contributors or submitter is None:
        return None
    if submitter not in contributors or not accumulated.issubset(contributors):
        return None
    return sorted(contributors), submitter


def assert_review_evidence_current(
    task: ReviewEvidenceTask,
    actor_id: object,
    *,
    effective_annotator_id: object | None = None,
) -> None:
    """Reject unknown evidence and prevent contributors from reviewing it."""
    evidence = current_review_evidence(task)
    if evidence is None:
        raise ReviewEvidenceUnknown()
    contributors, submitter = evidence
    contributor_set = set(contributors)
    actor = canonical_actor_id(actor_id)
    effective_annotator = canonical_actor_id(effective_annotator_id)
    if (
        actor is None
        or actor in contributor_set
        or actor == submitter
        or (effective_annotator is not None and actor == effective_annotator)
    ):
        raise ReviewActorIneligible()
