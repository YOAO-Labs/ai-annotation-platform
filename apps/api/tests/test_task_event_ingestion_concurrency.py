"""Real-connection races for task-event idempotency and async discard semantics."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager

import httpx
import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_event import TaskEvent
from app.db.models.user import User
from tests.factory import create_user
from tests.test_task_event_ingestion import _event, _project, _task, _worker_payload


@pytest.fixture
async def independent_event_actor(test_engine):
    """Commit seed data for independent request/worker connections; always remove it."""

    from app.core.security import create_access_token

    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    project_id = user_id = None
    try:
        async with maker() as seed:
            suffix = uuid.uuid4().hex
            user = await create_user(
                seed, "super_admin", f"events-{suffix}@example.test", "Event race"
            )
            user_id = user.id
            project = _project(user.id, f"P-TER-{suffix[:12]}")
            project_id = project.id
            task = _task(project.id, f"T-TER-{suffix[:12]}")
            seed.add(project)
            await seed.flush()
            seed.add(task)
            await seed.commit()
            token = create_access_token(subject=str(user.id), role=user.role)
        yield maker, user, project, task, {"Authorization": f"Bearer {token}"}
    finally:
        async with maker() as cleanup:
            if project_id is not None:
                await cleanup.execute(delete(Task).where(Task.project_id == project_id))
                await cleanup.execute(delete(Project).where(Project.id == project_id))
            if user_id is not None:
                await cleanup.execute(delete(User).where(User.id == user_id))
            await cleanup.commit()


@pytest.mark.parametrize(
    "deliveries", [("sync", "sync"), ("fallback", "worker"), ("worker", "worker")]
)
@pytest.mark.parametrize("conflicting", [False, True], ids=["identical", "conflicting"])
@pytest.mark.parametrize("mixed", [False, True], ids=["single", "mixed"])
async def test_concurrent_event_id_comparison_is_atomic(
    independent_event_actor,
    app_module,
    monkeypatch,
    caplog,
    deliveries,
    conflicting,
    mixed,
):
    from app.config import settings
    from app.deps import get_db
    from app.services import task_event_ingestion as ingestion
    from app.workers.task_events import _async_persist

    maker, user, project, task, headers = independent_event_actor
    event_id = uuid.uuid4()
    event = _event(task.id, project.id, event_id)
    batches = [
        [{**event, "annotation_count": index if conflicting else 0}]
        for index in range(2)
    ]
    if mixed:
        for batch in batches:
            batch.append(_event(task.id, project.id, uuid.uuid4()))

    async def request_session():
        async with maker() as session:
            yield session

    @asynccontextmanager
    async def worker_session():
        async with maker() as session:
            yield session

    monkeypatch.setattr(settings, "task_events_async", "fallback" in deliveries)
    monkeypatch.setattr("app.api.v1.me._enqueue_task_events", lambda _: False)
    monkeypatch.setattr("app.workers._db.task_session", worker_session)
    original_insert = ingestion.insert_task_events
    ready = asyncio.Barrier(2)
    backend_pids: set[int] = set()

    async def racing_insert(session, rows):
        # Both paths finish preflight with no stored event before either writes.
        # A process-local lock or shared fixture transaction cannot pass this test.
        backend_pids.add(await session.scalar(text("SELECT pg_backend_pid()")))
        assert (
            await session.scalar(select(TaskEvent.id).where(TaskEvent.id == event_id))
            is None
        )
        await ready.wait()
        return await original_insert(session, rows)

    monkeypatch.setattr(ingestion, "insert_task_events", racing_insert)
    monkeypatch.setattr("app.api.v1.me.insert_task_events", racing_insert)
    app_module.dependency_overrides[get_db] = request_session
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_module), base_url="http://test"
        ) as client:

            async def deliver(index):
                if deliveries[index] == "worker":
                    return await _async_persist(
                        [_worker_payload(item, user.id) for item in batches[index]]
                    )
                return await client.post(
                    "/api/v1/auth/me/task-events:batch",
                    json={"events": batches[index]},
                    headers=headers,
                )

            results = await asyncio.wait_for(
                asyncio.gather(deliver(0), deliver(1)), timeout=10
            )
    finally:
        app_module.dependency_overrides.pop(get_db, None)

    assert len(backend_pids) == 2
    async with maker() as check:
        stored = (
            (await check.execute(select(TaskEvent).where(TaskEvent.task_id == task.id)))
            .scalars()
            .all()
        )
    winner = next(row for row in stored if row.id == event_id)
    assert len(stored) == (3 if mixed else 1)
    inserted_by_workers = []
    for index, result in enumerate(results):
        lost_conflict = conflicting and winner.annotation_count != index
        if deliveries[index] == "worker":
            inserted_by_workers.append(result)
            if lost_conflict:
                assert result == (1 if mixed else 0)
                assert "duplicate_client_event_conflict" in caplog.text
                assert str(event_id) in caplog.text
        elif lost_conflict and not mixed:
            assert result.status_code == 409, result.text
            assert (
                result.json()["detail"]["reason"] == "duplicate_client_event_conflict"
            )
        else:
            assert result.status_code == 200, result.text
            assert result.json() == {
                "accepted": len(batches[index]) - int(lost_conflict),
                "queued_async": False,
                "discarded": [
                    {
                        "index": 0,
                        "client_id": str(event_id),
                        "reason": "duplicate_client_event_conflict",
                    }
                ]
                if lost_conflict
                else [],
            }
    if deliveries == ("worker", "worker"):
        assert sum(inserted_by_workers) == len(stored)


async def test_queued_conflicting_event_is_discarded_without_losing_valid_rows(
    independent_event_actor,
    app_module,
    monkeypatch,
    caplog,
):
    from app.config import settings
    from app.deps import get_db
    from app.workers.task_events import _async_persist

    maker, user, project, task, headers = independent_event_actor
    event = _event(task.id, project.id, uuid.uuid4())
    valid = _event(task.id, project.id, uuid.uuid4())
    captured = []
    monkeypatch.setattr(settings, "task_events_async", True)
    monkeypatch.setattr(
        "app.api.v1.me._enqueue_task_events",
        lambda payload: captured.append(payload) or True,
    )

    async def request_session():
        async with maker() as session:
            yield session

    @asynccontextmanager
    async def worker_session():
        async with maker() as session:
            yield session

    monkeypatch.setattr("app.workers._db.task_session", worker_session)
    app_module.dependency_overrides[get_db] = request_session
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_module), base_url="http://test"
        ) as client:
            for batch in ([event], [{**event, "annotation_count": 1}, valid]):
                response = await client.post(
                    "/api/v1/auth/me/task-events:batch",
                    json={"events": batch},
                    headers=headers,
                )
                assert response.status_code == 200, response.text
                assert response.json() == {
                    "accepted": len(batch),
                    "queued_async": True,
                    "discarded": [],
                }
    finally:
        app_module.dependency_overrides.pop(get_db, None)

    assert await _async_persist(captured[0]) == 1
    assert await _async_persist(captured[1]) == 1
    # Conflicting deliveries are permanent discards; identical retries stay no-ops.
    assert await _async_persist(captured[1]) == 0
    assert "duplicate_client_event_conflict" in caplog.text
    assert event["client_id"] in caplog.text
    async with maker() as check:
        stored = (
            (await check.execute(select(TaskEvent).where(TaskEvent.task_id == task.id)))
            .scalars()
            .all()
        )
    assert {row.id for row in stored} == {
        uuid.UUID(event["client_id"]),
        uuid.UUID(valid["client_id"]),
    }
    assert (
        next(
            row for row in stored if str(row.id) == event["client_id"]
        ).annotation_count
        == 0
    )
