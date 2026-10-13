"""Trust-boundary and idempotency checks for Workbench time events."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from tests.factory import create_membership


def _project(owner_id: uuid.UUID, display_id: str):
    from app.db.models.project import Project

    return Project(
        id=uuid.uuid4(),
        display_id=display_id,
        name=display_id,
        type_label="图像检测",
        type_key="image-det",
        owner_id=owner_id,
    )


def _task(project_id: uuid.UUID, display_id: str):
    from app.db.models.task import Task

    return Task(
        id=uuid.uuid4(),
        project_id=project_id,
        display_id=display_id,
        file_name="image.jpg",
        file_path="image.jpg",
    )


def _member(project_id: uuid.UUID, user_id: uuid.UUID, role: str):
    from app.db.models.project_member import ProjectMember

    return ProjectMember(project_id=project_id, user_id=user_id, role=role)


def _event(task_id: uuid.UUID, project_id: uuid.UUID, event_id: uuid.UUID):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return {
        "client_id": str(event_id),
        "task_id": str(task_id),
        "project_id": str(project_id),
        "kind": "annotate",
        "started_at": (now - timedelta(seconds=2)).isoformat(),
        "ended_at": now.isoformat(),
        "duration_ms": 2_000,
        "collector_version": "session-v2",
    }


@pytest.mark.asyncio
async def test_task_event_derives_project_and_is_idempotent(
    httpx_client, annotator, db_session, monkeypatch
):
    from app.config import settings
    from app.db.models.task_event import TaskEvent

    user, token = annotator
    project = _project(user.id, "P-TE-TRUST")
    task = _task(project.id, "T-TE-TRUST")
    # Explicit unbatched assignment makes the task visible to its annotator.
    task.assignee_id = user.id
    db_session.add_all([project, task, _member(project.id, user.id, "annotator")])
    await db_session.flush()
    event_id = uuid.uuid4()
    payload = {"events": [_event(task.id, project.id, event_id)]}
    monkeypatch.setattr(settings, "task_events_async", False)

    for _ in range(2):
        response = await httpx_client.post(
            "/api/v1/auth/me/task-events:batch",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["accepted"] == 1

    row = (
        await db_session.execute(select(TaskEvent).where(TaskEvent.id == event_id))
    ).scalar_one()
    assert row.project_id == task.project_id
    assert row.collection_source == "session"
    assert row.collection_coverage == "qualified"
    assert (
        await db_session.scalar(
            select(func.count()).select_from(TaskEvent).where(TaskEvent.id == event_id)
        )
    ) == 1

    legacy_payload = _event(task.id, project.id, uuid.uuid4())
    legacy_payload.pop("collector_version")
    response = await httpx_client.post(
        "/api/v1/auth/me/task-events:batch",
        json={"events": [legacy_payload]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    legacy = (
        await db_session.execute(
            select(TaskEvent).where(
                TaskEvent.id == uuid.UUID(legacy_payload["client_id"])
            )
        )
    ).scalar_one()
    assert legacy.collection_source == "legacy"
    assert legacy.collection_coverage == "unverified_collection"

    conflict = {"events": [{**payload["events"][0], "annotation_count": 1}]}
    response = await httpx_client.post(
        "/api/v1/auth/me/task-events:batch",
        json=conflict,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "duplicate_client_event_conflict"


@pytest.mark.asyncio
async def test_task_event_batch_discards_stale_row_without_blocking_valid_row(
    httpx_client, annotator, db_session, monkeypatch
):
    """A deleted task must not strand newer valid intervals behind it."""

    from app.config import settings
    from app.db.models.task_event import TaskEvent

    user, token = annotator
    project = _project(user.id, "P-TE-PARTIAL")
    task = _task(project.id, "T-TE-PARTIAL")
    task.assignee_id = user.id
    db_session.add_all([project, task, _member(project.id, user.id, "annotator")])
    await db_session.flush()
    monkeypatch.setattr(settings, "task_events_async", False)

    stale_id = uuid.uuid4()
    valid_id = uuid.uuid4()
    response = await httpx_client.post(
        "/api/v1/auth/me/task-events:batch",
        json={
            "events": [
                _event(uuid.uuid4(), project.id, stale_id),
                _event(task.id, project.id, valid_id),
            ]
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["accepted"] == 1
    assert body["discarded"] == [
        {"index": 0, "client_id": str(stale_id), "reason": "task_not_found"}
    ]
    assert (
        await db_session.scalar(
            select(func.count()).select_from(TaskEvent).where(TaskEvent.id == valid_id)
        )
    ) == 1
    assert (
        await db_session.scalar(
            select(func.count()).select_from(TaskEvent).where(TaskEvent.id == stale_id)
        )
    ) == 0

    partial_id = uuid.uuid4()
    partial = _event(task.id, project.id, partial_id)
    partial["collection_coverage"] = "partial"
    response = await httpx_client.post(
        "/api/v1/auth/me/task-events:batch",
        json={"events": [partial]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    partial_row = await db_session.get(TaskEvent, partial_id)
    assert partial_row is not None
    assert partial_row.collection_source == "session"
    assert partial_row.collection_coverage == "unverified_collection"


@pytest.mark.asyncio
async def test_async_payload_coverage_survives_worker_revalidation(
    httpx_client, annotator, db_session, monkeypatch
):
    """API-normalized coverage values remain valid through the Celery path."""

    from app.config import settings
    from app.db.models.task_event import TaskEvent
    from app.workers.task_events import _async_persist

    user, token = annotator
    project = _project(user.id, "P-TE-ASYNC-COVERAGE")
    task = _task(project.id, "T-TE-ASYNC-COVERAGE")
    task.assignee_id = user.id
    db_session.add_all([project, task, _member(project.id, user.id, "annotator")])
    await db_session.flush()

    captured: list[dict] = []
    monkeypatch.setattr(settings, "task_events_async", True)
    monkeypatch.setattr(
        "app.api.v1.me._enqueue_task_events",
        lambda payload: captured.extend(payload) or True,
    )
    partial_id = uuid.uuid4()
    legacy_id = uuid.uuid4()
    partial = _event(task.id, project.id, partial_id)
    partial["collection_coverage"] = "partial"
    legacy = _event(task.id, project.id, legacy_id)
    legacy.pop("collector_version")
    response = await httpx_client.post(
        "/api/v1/auth/me/task-events:batch",
        json={"events": [partial, legacy]},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "accepted": 2,
        "queued_async": True,
        "discarded": [],
    }
    assert [payload["collection_coverage"] for payload in captured] == [
        "unverified_collection",
        "unverified_collection",
    ]

    @asynccontextmanager
    async def fake_task_session():
        yield db_session

    monkeypatch.setattr("app.workers._db.task_session", fake_task_session)
    assert await _async_persist(captured) == 2
    assert await _async_persist(captured) == 0
    rows = (
        (
            await db_session.execute(
                select(TaskEvent).where(TaskEvent.id.in_({partial_id, legacy_id}))
            )
        )
        .scalars()
        .all()
    )
    assert {row.id for row in rows} == {partial_id, legacy_id}
    assert all(row.collection_coverage == "unverified_collection" for row in rows)
    assert {row.collection_source for row in rows} == {"session", "legacy"}


@pytest.mark.asyncio
async def test_task_event_rejects_conflicting_project_and_future_interval(
    httpx_client, annotator, db_session, monkeypatch
):
    from app.config import settings

    user, token = annotator
    project = _project(user.id, "P-TE-MISMATCH")
    task = _task(project.id, "T-TE-MISMATCH")
    db_session.add_all([project, task, _member(project.id, user.id, "annotator")])
    await db_session.flush()
    monkeypatch.setattr(settings, "task_events_async", False)

    mismatch = _event(task.id, uuid.uuid4(), uuid.uuid4())
    response = await httpx_client.post(
        "/api/v1/auth/me/task-events:batch",
        json={"events": [mismatch]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "task_project_mismatch"

    now = datetime.now(timezone.utc).replace(microsecond=0)
    future = _event(task.id, project.id, uuid.uuid4())
    future["started_at"] = now.isoformat()
    future["ended_at"] = (now + timedelta(seconds=2)).isoformat()
    response = await httpx_client.post(
        "/api/v1/auth/me/task-events:batch",
        json={"events": [future]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "interval_in_future"


@pytest.mark.asyncio
async def test_final_close_requires_current_project_access(
    httpx_client, super_admin, annotator, db_session, monkeypatch
):
    from app.config import settings
    from app.db.models.project_member import ProjectMember
    from app.db.models.task_batch import TaskBatch

    owner, _ = super_admin
    user, token = annotator
    project = _project(owner.id, "P-TE-CLOSE")
    batch = TaskBatch(
        id=uuid.uuid4(),
        project_id=project.id,
        display_id="B-TE-CLOSE",
        name="Batch",
        status="reviewing",
        annotator_id=user.id,
    )
    task = _task(project.id, "T-TE-CLOSE")
    task.batch_id = batch.id
    task.assignee_id = user.id
    task.status = "review"
    task.submitted_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db_session.add(project)
    await db_session.flush()
    db_session.add(batch)
    await db_session.flush()
    db_session.add_all(
        [
            task,
            ProjectMember(project_id=project.id, user_id=user.id, role="annotator"),
        ]
    )
    await db_session.flush()
    monkeypatch.setattr(settings, "task_events_async", False)

    response = await httpx_client.post(
        "/api/v1/auth/me/task-events:batch",
        json={"events": [_event(task.id, project.id, uuid.uuid4())]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text

    await db_session.execute(
        ProjectMember.__table__.delete().where(
            ProjectMember.project_id == project.id,
            ProjectMember.user_id == user.id,
        )
    )
    response = await httpx_client.post(
        "/api/v1/auth/me/task-events:batch",
        json={"events": [_event(task.id, project.id, uuid.uuid4())]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 404

    task.submitted_at = datetime.now(timezone.utc) - timedelta(minutes=6)
    await db_session.flush()
    stale = await httpx_client.post(
        "/api/v1/auth/me/task-events:batch",
        json={"events": [_event(task.id, project.id, uuid.uuid4())]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert stale.status_code == 404


@pytest.mark.asyncio
async def test_worker_revalidates_payload_and_deduplicates(
    db_session, super_admin, monkeypatch
):
    from app.db.models.task_event import TaskEvent
    from app.workers.task_events import _async_persist

    user, _ = super_admin
    project = _project(user.id, "P-TE-WORKER")
    task = _task(project.id, "T-TE-WORKER")
    db_session.add_all([project, task])
    await db_session.flush()
    event_id = uuid.uuid4()
    payload = _event(task.id, project.id, event_id)
    payload = {
        **payload,
        "id": payload.pop("client_id"),
        "user_id": str(user.id),
    }
    foreign = {**payload, "project_id": str(uuid.uuid4())}

    @asynccontextmanager
    async def fake_task_session():
        yield db_session

    monkeypatch.setattr("app.workers._db.task_session", fake_task_session)
    inserted = await _async_persist([payload, payload, foreign])

    assert inserted == 1
    assert (
        await db_session.scalar(
            select(func.count()).select_from(TaskEvent).where(TaskEvent.id == event_id)
        )
    ) == 1

    partial_id = uuid.uuid4()
    partial = {**payload, "id": str(partial_id), "collection_coverage": "partial"}
    assert await _async_persist([partial]) == 1
    partial_row = await db_session.get(TaskEvent, partial_id)
    assert partial_row is not None
    assert partial_row.collection_source == "session"
    assert partial_row.collection_coverage == "unverified_collection"


def _buffered_events(task, *, started_at, kind="annotate"):
    events = []
    for chunk in range(2):
        started = started_at + timedelta(minutes=30 * chunk)
        events.append(
            {
                **_event(task.id, task.project_id, uuid.uuid4()),
                "kind": kind,
                "started_at": started.isoformat(),
                "ended_at": (started + timedelta(minutes=30)).isoformat(),
                "duration_ms": 30 * 60 * 1000,
            }
        )
    return events


def _worker_payload(event, user_id):
    return {
        **event,
        "id": event["client_id"],
        "user_id": str(user_id),
    }


@pytest.mark.parametrize("endpoint", ["submit", "skip"])
@pytest.mark.parametrize("assignment", ["batch", "open_pool", "unbatched"])
async def test_buffered_chunks_survive_submit_and_skip(
    httpx_client,
    super_admin,
    annotator,
    db_session,
    monkeypatch,
    endpoint,
    assignment,
):
    """Real submission preserves a work start even when assignment is inherited."""

    from app.config import settings
    from app.db.models.task_event import TaskEvent
    from app.db.models.task_batch import TaskBatch
    from app.db.models.task_lock import TaskLock
    from app.workers.task_events import _async_persist

    owner, _ = super_admin
    user, token = annotator
    project = _project(owner.id, "P-TE-BUFFERED-SUBMIT")
    task = _task(project.id, "T-TE-BUFFERED-SUBMIT")
    started = datetime.now(timezone.utc) - timedelta(minutes=70)
    db_session.add(project)
    await db_session.flush()
    await create_membership(
        db_session, project_id=project.id, user_id=user.id, role="annotator"
    )
    batch = None
    if assignment == "unbatched":
        task.assignee_id = user.id
        task.assigned_at = started
    else:
        batch = TaskBatch(
            project_id=project.id,
            display_id="B-TE-BUFFERED-SUBMIT",
            name="Buffered task",
            status="annotating",
            annotator_id=user.id if assignment == "batch" else None,
        )
        db_session.add(batch)
        await db_session.flush()
        task.batch_id = batch.id
    db_session.add(task)
    await db_session.flush()
    headers = {"Authorization": f"Bearer {token}"}
    locked = await httpx_client.post(f"/api/v1/tasks/{task.id}/lock", headers=headers)
    assert locked.status_code == 200, locked.text
    lock = (
        await db_session.execute(select(TaskLock).where(TaskLock.task_id == task.id))
    ).scalar_one()
    # Advance the elapsed work time without waiting an hour. The lifecycle must
    # preserve this server-side lock timestamp before deleting the actual lock.
    lock.created_at = started
    await db_session.flush()
    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/{endpoint}",
        headers=headers,
        **({"json": {"reason": "no_target"}} if endpoint == "skip" else {}),
    )
    assert response.status_code == 200, response.text
    await db_session.refresh(task)
    assert task.assignee_id == user.id
    assert task.assigned_at == started
    assert (
        await db_session.scalar(select(TaskLock.id).where(TaskLock.task_id == task.id))
        is None
    )
    if batch is not None:
        await db_session.refresh(batch)
        assert batch.status == "reviewing"

    # The collector starts when the task renders, just before lock admission.
    events = _buffered_events(task, started_at=started - timedelta(seconds=1))

    @asynccontextmanager
    async def fake_task_session():
        yield db_session

    monkeypatch.setattr("app.workers._db.task_session", fake_task_session)
    assert (
        await _async_persist([_worker_payload(event, user.id) for event in events]) == 2
    )
    # Broker failure uses the same persisted-event comparison and access policy.
    monkeypatch.setattr(settings, "task_events_async", True)
    monkeypatch.setattr("app.api.v1.me._enqueue_task_events", lambda _: False)
    response = await httpx_client.post(
        "/api/v1/auth/me/task-events:batch", json={"events": events}, headers=headers
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"accepted": 2, "queued_async": False, "discarded": []}
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(TaskEvent)
            .where(TaskEvent.task_id == task.id)
        )
        == 2
    )


@pytest.mark.parametrize("endpoint", ["approve", "reject"])
async def test_buffered_review_chunks_survive_real_claim_and_decision(
    httpx_client,
    super_admin,
    reviewer,
    db_session,
    monkeypatch,
    endpoint,
):
    from app.config import settings
    from app.db.models.project_member import ProjectMember
    from app.db.models.task_batch import TaskBatch
    from app.workers.task_events import _async_persist

    from tests.factory import create_user

    owner, _ = super_admin
    user, token = reviewer
    project = _project(owner.id, "P-TE-BUFFERED-REVIEW")
    task = _task(project.id, "T-TE-BUFFERED-REVIEW")
    task.status = "review"
    # A distinct literal annotator submitted the round; the reviewer's decision
    # is authorized against that frozen contributor evidence.
    writer = await create_user(
        db_session,
        "employee",
        f"te-writer-{uuid.uuid4().hex[:8]}@test.local",
        "TE Writer",
    )
    task.annotation_contributor_ids = [str(writer.id)]
    task.review_contributor_ids = [str(writer.id)]
    task.review_submitter_id = writer.id
    task.review_round_id = uuid.uuid4()
    db_session.add(project)
    await db_session.flush()
    batch = TaskBatch(
        project_id=project.id,
        display_id="B-TE-BUFFERED-REVIEW",
        name="Review",
        status="reviewing",
    )
    db_session.add(batch)
    await db_session.flush()
    task.batch_id = batch.id
    db_session.add_all(
        [task, ProjectMember(project_id=project.id, user_id=user.id, role="reviewer")]
    )
    await db_session.flush()
    headers = {"Authorization": f"Bearer {token}"}
    started = datetime.now(timezone.utc) - timedelta(minutes=70)

    class ClaimClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return started.astimezone(tz)

    with monkeypatch.context() as clock_patch:
        clock_patch.setattr("app.api.v1.tasks.review.datetime", ClaimClock)
        claimed = await httpx_client.post(
            f"/api/v1/tasks/{task.id}/review/claim", headers=headers
        )
    assert claimed.status_code == 200, claimed.text
    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/review/{endpoint}",
        headers=headers,
        **({"json": {"reason_type": "missing"}} if endpoint == "reject" else {}),
    )
    assert response.status_code == 200, response.text
    transition = await httpx_client.post(
        f"/api/v1/projects/{project.id}/batches/{batch.id}/transition",
        json={"target_status": "approved" if endpoint == "approve" else "rejected"},
        headers=headers,
    )
    assert transition.status_code == 200, transition.text
    await db_session.refresh(task)
    await db_session.refresh(batch)
    assert task.reviewer_id == user.id
    assert task.reviewer_claimed_at == started
    assert batch.status == ("approved" if endpoint == "approve" else "rejected")
    events = _buffered_events(
        task, started_at=started - timedelta(seconds=1), kind="review"
    )
    monkeypatch.setattr(settings, "task_events_async", False)
    response = await httpx_client.post(
        "/api/v1/auth/me/task-events:batch", json={"events": events}, headers=headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["accepted"] == 2
    assert response.json()["discarded"] == []

    @asynccontextmanager
    async def fake_task_session():
        yield db_session

    monkeypatch.setattr("app.workers._db.task_session", fake_task_session)
    assert (
        await _async_persist([_worker_payload(event, user.id) for event in events]) == 0
    )


@pytest.mark.parametrize("kind", ["annotate", "review"])
@pytest.mark.parametrize(
    "invalid",
    [
        "before_actor_start",
        "missing_actor_start",
        "different_actor",
        "removed_member",
        "wrong_role",
        "after_transition",
    ],
)
async def test_buffered_chunks_keep_actor_and_time_boundaries(
    db_session,
    super_admin,
    annotator,
    reviewer,
    kind,
    invalid,
):
    from app.db.models.task_batch import TaskBatch
    from app.schemas.task_event import TaskEventIn
    from app.services.task_event_ingestion import (
        validate_api_events,
        validate_worker_event,
    )

    owner, _ = super_admin
    user, _ = reviewer if kind == "review" else annotator
    project = _project(owner.id, "P-TE-BUFFERED-GUARD")
    task = _task(project.id, "T-TE-BUFFERED-GUARD")
    started = datetime.now(timezone.utc) - timedelta(hours=2)
    transition = started + timedelta(minutes=70)
    task.status = "completed"
    task.assignee_id = user.id
    task.assigned_at = started
    task.submitted_at = transition
    task.reviewer_id = user.id
    task.reviewer_claimed_at = started
    task.reviewed_at = transition
    db_session.add(project)
    await db_session.flush()
    batch = TaskBatch(
        project_id=project.id,
        display_id="B-TE-BUFFERED-GUARD",
        name="Closed batch",
        status="approved",
    )
    db_session.add(batch)
    await db_session.flush()
    task.batch_id = batch.id
    db_session.add(task)
    membership = None
    if invalid != "removed_member":
        membership = _member(
            project.id, user.id, "reviewer" if kind == "review" else "annotator"
        )
        db_session.add(membership)
    event_start = started
    if invalid == "before_actor_start":
        event_start -= timedelta(minutes=31)
    elif invalid == "after_transition":
        event_start = transition + timedelta(minutes=6)
    elif invalid == "missing_actor_start":
        task.assigned_at = task.reviewer_claimed_at = None
    elif invalid == "different_actor":
        task.assignee_id = task.reviewer_id = owner.id
    elif invalid == "wrong_role":
        # The project membership role, not the account role, decides the work type.
        membership.role = "annotator" if kind == "review" else "reviewer"
    await db_session.flush()
    event = _buffered_events(task, started_at=event_start, kind=kind)[0]
    rows, rejected = await validate_api_events(
        db_session, user=user, events=[TaskEventIn.model_validate(event)]
    )
    assert rows == []
    assert len(rejected) == 1
    assert rejected[0].error.status_code == (403 if invalid == "wrong_role" else 404)
    assert (
        await validate_worker_event(db_session, _worker_payload(event, user.id)) is None
    )


@pytest.mark.parametrize("lock_state", ["missing", "foreign", "expired"])
async def test_submit_does_not_backdate_assignment_from_unowned_or_expired_lock(
    httpx_client,
    super_admin,
    annotator,
    db_session,
    monkeypatch,
    lock_state,
):
    from app.config import settings
    from app.db.models.project_member import ProjectMember
    from app.db.models.task_batch import TaskBatch
    from app.db.models.task_lock import TaskLock

    owner, owner_token = super_admin
    user, token = annotator
    project = _project(owner.id, "P-TE-LOCK-BOUNDARY")
    task = _task(project.id, "T-TE-LOCK-BOUNDARY")
    db_session.add(project)
    await db_session.flush()
    batch = TaskBatch(
        project_id=project.id,
        display_id="B-TE-LOCK-BOUNDARY",
        name="Inherited assignment",
        status="annotating",
        annotator_id=user.id,
    )
    db_session.add(batch)
    await db_session.flush()
    task.batch_id = batch.id
    db_session.add_all(
        [task, ProjectMember(project_id=project.id, user_id=user.id, role="annotator")]
    )
    await db_session.flush()
    started = datetime.now(timezone.utc) - timedelta(minutes=70)
    headers = {"Authorization": f"Bearer {token}"}
    if lock_state != "missing":
        locked = await httpx_client.post(
            f"/api/v1/tasks/{task.id}/lock",
            headers={"Authorization": f"Bearer {owner_token}"}
            if lock_state == "foreign"
            else headers,
        )
        assert locked.status_code == 200, locked.text
        lock = (
            await db_session.execute(
                select(TaskLock).where(TaskLock.task_id == task.id)
            )
        ).scalar_one()
        lock.created_at = started
        if lock_state == "expired":
            lock.expire_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db_session.flush()
    before_submit = datetime.now(timezone.utc)
    submitted = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/submit", headers=headers
    )
    assert submitted.status_code == 200, submitted.text
    await db_session.refresh(task)
    assert before_submit <= task.assigned_at <= task.submitted_at
    monkeypatch.setattr(settings, "task_events_async", False)
    response = await httpx_client.post(
        "/api/v1/auth/me/task-events:batch",
        json={"events": [_buffered_events(task, started_at=started)[0]]},
        headers=headers,
    )
    assert response.status_code == 404, response.text
