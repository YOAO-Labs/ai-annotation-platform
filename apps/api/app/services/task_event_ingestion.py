"""Trust boundary for Workbench task-time events.

The browser supplies a closed interval, but it cannot choose the account or
project that owns it.  This module is shared by the API and Celery worker so a
queued event is held to the same rules when it is finally persisted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.tasks._shared import _assert_task_visible
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_event import TaskEvent
from app.db.models.user import User
from app.schemas.task_event import TaskEventIn
from app.services.project_access import ProjectCapability, resolve_project_access

MAX_EVENT_DURATION_MS = 4 * 60 * 60 * 1000
MAX_ANNOTATION_COUNT = 1_000_000
MAX_TIMESTAMP_SKEW_MS = 1
FINAL_CLOSE_GRACE_MS = 5 * 60 * 1000
SESSION_COLLECTOR_VERSION = "session-v2"

_RETRYABLE_EVENT_STATUS_CODES = {401, 408, 425, 429}


@dataclass(frozen=True, slots=True)
class TaskEventRejection:
    """A permanently rejected input row and its original API error."""

    index: int
    client_id: UUID | None
    error: HTTPException


@dataclass(frozen=True, slots=True)
class TaskEventInsertResult:
    inserted: int
    conflicts: frozenset[UUID] = frozenset()


def task_event_rejection_reason(error: HTTPException) -> str:
    """Return the stable, short reason exposed for a rejected input row."""

    detail = error.detail
    if isinstance(detail, dict):
        reason = detail.get("reason")
        if isinstance(reason, str) and reason:
            return reason[:64]
    if isinstance(detail, str) and detail:
        return detail[:64]
    return "task_event_rejected"


def _is_permanent_event_error(error: HTTPException) -> bool:
    return (
        error.status_code < 500
        and error.status_code not in _RETRYABLE_EVENT_STATUS_CODES
    )


def _reject(reason: str, *, status_code: int = 422) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"reason": reason})


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise _reject("timestamp_timezone_required")
    return value.astimezone(timezone.utc)


def validate_interval(
    *,
    started_at: datetime,
    ended_at: datetime,
    duration_ms: int,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Normalize and validate one event independently of SQLAlchemy."""

    started = _utc(started_at)
    ended = _utc(ended_at)
    if ended < started:
        raise _reject("ended_before_started")
    if duration_ms < 0 or duration_ms > MAX_EVENT_DURATION_MS:
        raise _reject("duration_out_of_range")
    elapsed_ms = round((ended - started).total_seconds() * 1000)
    if abs(elapsed_ms - int(duration_ms)) > MAX_TIMESTAMP_SKEW_MS:
        raise _reject("duration_mismatch")
    current = _utc(now or datetime.now(timezone.utc))
    if ended > current:
        raise _reject("interval_in_future")
    return started, ended


def _within_final_close_grace(
    *, ended_at: datetime | None, transition_at: datetime | None
) -> bool:
    """Keep the terminal-state exception tied to the actual transition."""

    if ended_at is None or transition_at is None:
        return False
    try:
        ended = _utc(ended_at)
        transition = _utc(transition_at)
    except HTTPException:
        return False
    return abs((ended - transition).total_seconds() * 1000) <= FINAL_CLOSE_GRACE_MS


def _within_actor_work_interval(
    *,
    started_at: datetime | None,
    ended_at: datetime | None,
    actor_started_at: datetime | None,
    transition_at: datetime | None,
) -> bool:
    """Bound buffered chunks by the server actor span and admission clock grace."""

    if any(
        value is None
        for value in (started_at, ended_at, actor_started_at, transition_at)
    ):
        return False
    try:
        return (
            _utc(actor_started_at) - timedelta(milliseconds=FINAL_CLOSE_GRACE_MS)
            <= _utc(started_at)
            <= _utc(ended_at)
            <= _utc(transition_at)
        )
    except HTTPException:
        return False


async def assert_task_event_access(
    db: AsyncSession,
    *,
    task: Task,
    user: User,
    kind: str,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
) -> None:
    """Apply the normal project/task visibility policy with final-close grace.

    A task can transition to review/completed between the last visible render
    and the next task switch. The task's own actor binding is enough to close
    that actor's final interval within the five-minute transition window.
    Earlier buffered chunks must fit between the same actor's server-recorded
    assignment/claim and transition; a foreign or unbounded historical interval
    still fails the normal policy.
    """

    project = await db.get(Project, task.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Task not found")
    # Current account + actual-project membership authority, locked for the
    # persisted interval write.  Legacy global annotator/reviewer roles are not
    # an authority source; an anomalous non-administrative owner is rejected
    # because resolve_project_access fails closed without a valid membership.
    access = await resolve_project_access(
        db, user=user, project=project, lock_membership=True
    )
    required_capability = (
        ProjectCapability.REVIEW_WRITE.value
        if kind == "review"
        else ProjectCapability.ANNOTATION_WRITE.value
    )
    if not access.is_manager and not access.has(required_capability):
        raise _reject("work_type_not_allowed", status_code=403)

    try:
        await _assert_task_visible(db, task, user, access=access)
    except HTTPException as exc:
        can_close_submitted = (
            kind == "annotate"
            and task.assignee_id == user.id
            and task.submitted_at is not None
            and task.status in {"review", "rejected", "completed"}
            and (
                _within_final_close_grace(
                    ended_at=ended_at, transition_at=task.submitted_at
                )
                or _within_actor_work_interval(
                    started_at=started_at,
                    ended_at=ended_at,
                    actor_started_at=task.assigned_at,
                    transition_at=task.submitted_at,
                )
            )
        )
        can_close_reviewed = (
            kind == "review"
            and task.reviewer_id == user.id
            and task.reviewed_at is not None
            and task.status in {"rejected", "completed"}
            and (
                _within_final_close_grace(
                    ended_at=ended_at, transition_at=task.reviewed_at
                )
                or _within_actor_work_interval(
                    started_at=started_at,
                    ended_at=ended_at,
                    actor_started_at=task.reviewer_claimed_at,
                    transition_at=task.reviewed_at,
                )
            )
        )
        if exc.status_code != 404 or not (can_close_submitted or can_close_reviewed):
            raise


def _fingerprint(payload: Mapping[str, Any]) -> tuple[Any, ...]:
    def canonical(value: Any) -> str:
        if isinstance(value, datetime):
            return _utc(value).isoformat()
        return str(value)

    return (
        canonical(payload["task_id"]),
        canonical(payload["user_id"]),
        canonical(payload["project_id"]),
        canonical(payload["kind"]),
        canonical(payload["started_at"]),
        canonical(payload["ended_at"]),
        canonical(payload["duration_ms"]),
        canonical(payload.get("annotation_count", 0)),
        canonical(payload.get("was_rejected", False)),
        canonical(payload.get("collector_version")),
        canonical(payload.get("collection_coverage", "qualified")),
    )


async def validate_api_events(
    db: AsyncSession,
    *,
    user: User,
    events: Iterable[TaskEventIn],
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], list[TaskEventRejection]]:
    """Validate browser payloads without letting one stale row block a batch.

    Per-event HTTP validation failures are permanent for a closed interval and
    are returned to the API as discard details. Database and other transient
    failures still raise so the caller retains the whole batch for retry.
    """

    event_list = list(events)
    task_ids = {event.task_id for event in event_list}
    task_rows = (
        await db.execute(select(Task).where(Task.id.in_(task_ids)))
        if task_ids
        else None
    )
    tasks = {task.id: task for task in task_rows.scalars()} if task_rows else {}
    seen: dict[UUID, tuple[Any, ...]] = {}
    rows: list[dict[str, Any]] = []
    row_indexes: dict[UUID, int] = {}
    rejections: list[TaskEventRejection] = []
    for index, event in enumerate(event_list):
        try:
            task = tasks.get(event.task_id)
            if task is None:
                raise _reject("task_not_found", status_code=404)
            if task.project_id != event.project_id:
                raise _reject("task_project_mismatch")
            started, ended = validate_interval(
                started_at=event.started_at,
                ended_at=event.ended_at,
                duration_ms=event.duration_ms,
                now=now,
            )
            await assert_task_event_access(
                db,
                task=task,
                user=user,
                kind=event.kind,
                started_at=started,
                ended_at=ended,
            )

            event_id = event.client_id or uuid4()
            row = {
                "id": event_id,
                "task_id": task.id,
                "user_id": user.id,
                "project_id": task.project_id,
                "kind": event.kind,
                "started_at": started,
                "ended_at": ended,
                "duration_ms": event.duration_ms,
                "annotation_count": event.annotation_count,
                "was_rejected": event.was_rejected,
                "collector_version": event.collector_version,
                "collection_source": (
                    "session"
                    if event.collector_version == SESSION_COLLECTOR_VERSION
                    else "legacy"
                ),
                "collection_coverage": (
                    "qualified"
                    if (
                        event.collector_version == SESSION_COLLECTOR_VERSION
                        and event.collection_coverage == "qualified"
                    )
                    else "unverified_collection"
                ),
            }
            if event.client_id is not None:
                fingerprint = _fingerprint(
                    {
                        **row,
                        "task_id": str(row["task_id"]),
                        "user_id": str(row["user_id"]),
                        "project_id": str(row["project_id"]),
                        "started_at": started.isoformat(),
                        "ended_at": ended.isoformat(),
                    }
                )
                previous = seen.get(event.client_id)
                if previous is not None and previous != fingerprint:
                    raise _reject("duplicate_client_event_conflict", status_code=409)
                if previous is not None:
                    continue
                seen[event.client_id] = fingerprint
                row_indexes[event_id] = index
            rows.append(row)
        except HTTPException as exc:
            if not _is_permanent_event_error(exc):
                raise
            rejections.append(
                TaskEventRejection(index=index, client_id=event.client_id, error=exc)
            )

    client_ids = {
        event.client_id for event in event_list if event.client_id is not None
    }
    if client_ids:
        existing = (
            await db.execute(
                select(TaskEvent.__table__).where(TaskEvent.id.in_(client_ids))
            )
        ).mappings()
        existing_by_id = {row.id: row for row in existing}
        accepted_rows: list[dict[str, Any]] = []
        for row in rows:
            stored = existing_by_id.get(row["id"])
            if stored is None:
                accepted_rows.append(row)
                continue
            if _fingerprint(row) != _fingerprint(stored):
                rejections.append(
                    TaskEventRejection(
                        index=row_indexes[row["id"]],
                        client_id=row["id"],
                        error=_reject(
                            "duplicate_client_event_conflict", status_code=409
                        ),
                    )
                )
                continue
            accepted_rows.append(row)
        rows = accepted_rows
    return rows, rejections


async def validate_worker_event(
    db: AsyncSession,
    payload: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Revalidate one Celery payload; invalid/stale messages are discarded."""

    try:
        event_id = UUID(str(payload["id"]))
        task_id = UUID(str(payload["task_id"]))
        user_id = UUID(str(payload["user_id"]))
        client_project_id = UUID(str(payload["project_id"]))
        kind = str(payload["kind"])
        started_at = datetime.fromisoformat(str(payload["started_at"]))
        ended_at = datetime.fromisoformat(str(payload["ended_at"]))
        duration_ms = int(payload["duration_ms"])
        annotation_count = int(payload.get("annotation_count", 0))
        was_rejected = bool(payload.get("was_rejected", False))
        collector_version = payload.get("collector_version")
        collection_coverage = payload.get("collection_coverage", "qualified")
    except (KeyError, TypeError, ValueError):
        return None

    if (
        kind not in {"annotate", "review"}
        or not 0 <= annotation_count <= MAX_ANNOTATION_COUNT
        or (
            collector_version is not None
            and (not isinstance(collector_version, str) or len(collector_version) > 32)
        )
        or collection_coverage not in {"qualified", "partial", "unverified_collection"}
    ):
        return None
    try:
        started, ended = validate_interval(
            started_at=started_at,
            ended_at=ended_at,
            duration_ms=duration_ms,
            now=now,
        )
    except HTTPException:
        return None

    user = await db.get(User, user_id)
    task = await db.get(Task, task_id)
    if user is None or not user.is_active or task is None:
        return None
    if task.project_id != client_project_id:
        return None
    try:
        await assert_task_event_access(
            db,
            task=task,
            user=user,
            kind=kind,
            started_at=started,
            ended_at=ended,
        )
    except HTTPException:
        return None

    return {
        "id": event_id,
        "task_id": task.id,
        "user_id": user.id,
        "project_id": task.project_id,
        "kind": kind,
        "started_at": started,
        "ended_at": ended,
        "duration_ms": duration_ms,
        "annotation_count": annotation_count,
        "was_rejected": was_rejected,
        "collector_version": collector_version,
        "collection_source": (
            "session" if collector_version == SESSION_COLLECTOR_VERSION else "legacy"
        ),
        "collection_coverage": (
            "qualified"
            if (
                collector_version == SESSION_COLLECTOR_VERSION
                and collection_coverage == "qualified"
            )
            else "unverified_collection"
        ),
    }


async def insert_task_events(
    db: AsyncSession,
    rows: list[dict[str, Any]],
) -> TaskEventInsertResult:
    """Serialize ID comparison and insertion for API and worker deliveries.

    A preflight read cannot distinguish identical retries from conflicting
    concurrent payloads. Transaction-scoped ID locks keep that comparison
    authoritative until commit, including when the event does not exist yet.
    Sorted acquisition also lets overlapping batches arrive in any order.
    """

    if not rows:
        return TaskEventInsertResult(inserted=0)
    by_id: dict[UUID, dict[str, Any]] = {}
    conflicts: set[UUID] = set()
    for row in rows:
        event_id = row["id"]
        previous = by_id.get(event_id)
        if previous is not None and _fingerprint(previous) != _fingerprint(row):
            conflicts.add(event_id)
        by_id[event_id] = row

    for event_id in sorted(by_id):
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"aap:task-event:{event_id}"},
        )
    existing = (
        await db.execute(select(TaskEvent.__table__).where(TaskEvent.id.in_(by_id)))
    ).mappings()
    stored_ids: set[UUID] = set()
    for stored in existing:
        stored_ids.add(stored.id)
        if _fingerprint(by_id[stored.id]) != _fingerprint(stored):
            conflicts.add(stored.id)
    new_rows = [
        by_id[event_id]
        for event_id in sorted(by_id)
        if event_id not in stored_ids and event_id not in conflicts
    ]
    if new_rows:
        await db.execute(pg_insert(TaskEvent).values(new_rows))
    await db.commit()
    return TaskEventInsertResult(inserted=len(new_rows), conflicts=frozenset(conflicts))
