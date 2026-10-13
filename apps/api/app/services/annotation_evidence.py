"""Increment A2 · durable annotation-phase contributor evidence.

The project-scoped employee roles plan needs a trustworthy record of *who*
contributed annotation-phase work to a task before any project-role model can
use that record for self-review.  This module owns the only writer of the
additive task fields introduced by the A1 schema revision:

* ``Task.annotation_contributor_ids`` — accumulated annotation-phase actors.
  ``None`` means completeness is **unknown** (legacy rows created before the
  recording binary, or a rolled-back recording interval).  ``[]`` means a
  known-empty set for a task created by the recording binary.  Any other list
  is the accumulated actor set.
* ``Task.review_contributor_ids`` — the contributor set frozen for the current
  ``review_round_id`` at submit/skip/batch-send/video completion.
* ``Task.review_submitter_id`` — the actor who actually submitted that round.

Recording is strictly additive in this increment: it never grants or denies
authorization, and it never changes first-review performance facts.

Unknown is sticky.  A later edit on a legacy task whose accumulator is ``NULL``
must not narrow it to a known set containing only the new actor, because the
missing history would be silently treated as complete.  Malformed evidence
(any non-list value, or a list containing a non-canonical user id) is treated
as unknown too, never silently dropped.

Locking policy
--------------

Producers must call :func:`record_annotation_actor` (or
:func:`record_annotation_actors_for_tasks`) **before** they take an Annotation
or SceneTrack row lock, because the existing writers already order
``Task -> Annotation``; a failed transaction rolls the evidence update back with
the mutation that failed.  Where a producer instead records after the mutation,
the task must already be locked first (for example the single-task
PATCH/DELETE endpoints).

Some producers are inherently mixed-order — an AAP import envelope, an
interpolation range, a resume, or a tracker accept cannot lock every task before
the resource rows without a repository-wide rewrite.  Those boundaries pass
``nowait=True``: the task row is acquired with ``FOR UPDATE NOWAIT`` and a busy
lock rolls the transaction back and surfaces a retryable 409 instead of
introducing a blocking wait cycle.  See
``app/services/user_lifecycle.py`` for the same nonblocking conflict pattern.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.task import Task

# Retryable PostgreSQL lock/transaction conflicts: lock_not_available (NOWAIT),
# deadlock_detected, serialization_failure.
_BUSY_SQLSTATES = {"55P03", "40P01", "40001"}


def canonical_actor_id(value: object) -> str | None:
    """Return the canonical (lowercase) UUID string for a stored actor id.

    ``None`` means the value is not a usable user id.  Callers treat an invalid
    element as evidence corruption, not as an actor to skip.
    """
    if isinstance(value, uuid.UUID):
        return str(value)
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        return None


def canonical_actor_set(values: object) -> set[str] | None:
    """Canonicalize a stored accumulator, or ``None`` when it is malformed.

    A non-array, a ``null``/empty element, or an element that is not a UUID makes
    the whole set unknown.  Evidence is never silently narrowed.
    """
    if not isinstance(values, list):
        return None
    canonical: set[str] = set()
    for value in values:
        parsed = canonical_actor_id(value)
        if parsed is None:
            return None
        canonical.add(parsed)
    return canonical


def known_annotation_contributor_ids(task: Task) -> set[str] | None:
    """Return the accumulated actor set, or ``None`` when completeness is unknown."""
    return canonical_actor_set(task.annotation_contributor_ids)


async def lock_tasks_for_evidence(
    db: AsyncSession,
    task_ids: Iterable[uuid.UUID | None],
    *,
    nowait: bool = False,
) -> None:
    """Acquire task row locks in stable id order before resource locks.

    Use ``nowait=True`` at mixed-order boundaries: a busy lock is rolled back and
    reported as a retryable 409 instead of forming a blocking wait cycle.
    """
    ids = sorted({task_id for task_id in task_ids if task_id is not None}, key=str)
    if not ids:
        return
    stmt = (
        select(Task.id)
        .where(Task.id.in_(ids))
        .order_by(Task.id)
        .with_for_update(nowait=nowait)
    )
    try:
        # An autoflush UPDATE can wait before the NOWAIT SELECT ever executes.
        # Acquire the locks before flushing any pending resource changes.
        with db.no_autoflush:
            await db.execute(stmt)
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) in _BUSY_SQLSTATES:
            await db.rollback()
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "task_evidence_busy",
                    "message": "task is being updated; refresh and retry",
                },
            ) from exc
        raise


async def refresh_task_evidence(
    db: AsyncSession, task: Task, *, nowait: bool = False
) -> None:
    """Lock the task row and refresh the evidence-relevant attributes.

    Callers that must freeze/clear evidence under the same task lock as a
    workflow transition use this to avoid trusting a stale in-memory row.
    Pending session changes are flushed first: ``Session.refresh`` expires the
    target attributes before its autoflush and would otherwise discard them.

    At mixed-order boundaries acquire NOWAIT before autoflush, then reuse the
    same flush/refresh path while holding the lock. This preserves pending
    same-transaction contributions without a second evidence merge algorithm.
    """
    if nowait:
        await lock_tasks_for_evidence(db, [task.id], nowait=True)
    await db.flush()
    await db.refresh(
        task,
        ["annotation_contributor_ids", "status"],
        with_for_update=True,
    )


async def record_annotation_actor(
    db: AsyncSession,
    task: Task,
    actor_id: uuid.UUID | None,
    *,
    lock: bool = True,
    nowait: bool = False,
) -> bool:
    """Accumulate one annotation-phase actor on ``task``.

    Returns ``True`` when the stored accumulator changed.  ``actor_id`` is the
    acting user, not the original ``Annotation.user_id`` of a mutated row.

    With ``lock`` the task row is locked and both the accumulator and the
    workflow phase are re-read under that lock.  The default blocking path
    flushes pending changes before the refresh because ``Session.refresh``
    expires the target attributes before its autoflush and would otherwise
    discard an earlier accumulator addition from the same transaction.
    ``nowait=True`` is for mixed-order boundaries and reports a busy lock as a
    retryable 409.

    No-op for a ``None``/malformed actor, a ``NULL`` (unknown) or malformed
    accumulator: unknown evidence stays unknown.

    A write while the fresh locked phase is ``review`` is a review-phase
    adjustment (the annotation endpoints only admit reviewers and privileged
    managers in that state), so it must not turn the reviewer into an
    annotation author.
    """
    actor = canonical_actor_id(actor_id)
    if actor is None:
        return False
    if lock:
        await refresh_task_evidence(db, task, nowait=nowait)
    if task.status == "review":
        return False
    current = known_annotation_contributor_ids(task)
    if current is None:
        return False
    if actor in current:
        return False
    current.add(actor)
    task.annotation_contributor_ids = sorted(current)
    return True


async def record_annotation_actor_for_task(
    db: AsyncSession,
    task_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    *,
    nowait: bool = False,
) -> bool:
    """Accumulate ``actor_id`` on a task addressed by id, locking it first."""
    if canonical_actor_id(actor_id) is None:
        return False
    with db.no_autoflush:
        task = await db.get(Task, task_id)
    if task is None:
        return False
    return await record_annotation_actor(db, task, actor_id, lock=True, nowait=nowait)


async def record_annotation_actors_for_tasks(
    db: AsyncSession,
    task_ids: Iterable[uuid.UUID],
    actor_id: uuid.UUID | None,
    *,
    nowait: bool = False,
) -> set[uuid.UUID]:
    """Accumulate one actor across distinct tasks in stable id order.

    Sorting keeps the per-task lock acquisition order deterministic.  Use
    ``nowait=True`` at mixed-order boundaries; a busy task is reported as a
    retryable 409 with the transaction rolled back.
    """
    ids = sorted(set(task_ids), key=str)
    if nowait:
        await lock_tasks_for_evidence(db, ids, nowait=True)
    touched: set[uuid.UUID] = set()
    for task_id in ids:
        await record_annotation_actor_for_task(db, task_id, actor_id, nowait=nowait)
        touched.add(task_id)
    return touched


def freeze_review_contributor_evidence(
    task: Task,
    *,
    submitter_id: uuid.UUID | None,
    contributor_ids: Iterable[str] = (),
) -> list[str] | None:
    """Freeze the current round's contributor evidence on the task row.

    The frozen set is the accumulated annotation contributors plus the surviving
    annotation authors, the effective annotation assignee and the actual
    submitter.  Returns the frozen list, or ``None`` when the accumulator is
    unknown — a known review set is never manufactured from an unknown
    accumulator.  Any malformed freeze input makes the round evidence unknown
    rather than silently dropping that actor.  ``review_submitter_id`` is
    recorded only when it is a canonical user id.
    """
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


def clear_review_contributor_evidence(task: Task) -> None:
    """Invalidate the current round's frozen evidence when a task leaves review.

    The annotation contributor accumulator is deliberately retained: an actor
    who ever contributed stays disqualified for the task even after a reset,
    reassignment, rejection or reopen.
    """
    task.review_contributor_ids = None
    task.review_submitter_id = None


def current_review_evidence(task: Task) -> tuple[list[str], str] | None:
    """Return ``(frozen_contributors, submitter_id)`` or ``None`` when unknown.

    Unknown covers a missing round, a non-canonical / incomplete contributor
    array, or a missing submitter.  Authorization callers must treat ``None``
    as unavailable work, never as an empty contributor set.
    """

    if task.review_round_id is None:
        return None
    # An unknown or malformed annotation accumulator means completeness cannot
    # be established, even when the frozen set is non-empty.
    accumulated = known_annotation_contributor_ids(task)
    if accumulated is None:
        return None
    contributors = canonical_actor_set(task.review_contributor_ids)
    submitter = canonical_actor_id(task.review_submitter_id)
    if not contributors or submitter is None:
        return None
    # The submitter must be part of the frozen set, and the frozen set must
    # cover every accumulated annotation contributor (frozen superset).
    if submitter not in contributors or not accumulated.issubset(contributors):
        return None
    return sorted(contributors), submitter


def assert_review_evidence_current(
    task: Task,
    actor_id: object,
    *,
    effective_annotator_id: object | None = None,
) -> None:
    """Authorize a review write against the current frozen evidence.

    Call this *before* any owner/super-admin privileged return so a manager
    cannot review work they contributed to.  Unknown or incomplete evidence is
    a 409; a frozen contributor, the round submitter or the effective annotator
    is a 403.  Historical performance attribution must keep using
    ``_review_round_contributor_snapshot``; this guard only authorizes.
    """

    evidence = current_review_evidence(task)
    if evidence is None:
        # Missing, non-canonical or incomplete evidence is unavailable work.
        raise HTTPException(
            status_code=409,
            detail={"reason": "review_contributors_unknown"},
        )
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
        raise HTTPException(
            status_code=403,
            detail={"reason": "self_review_denied"},
        )
