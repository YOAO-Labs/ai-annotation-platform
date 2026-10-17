"""Committed idempotency receipt behavior when the ASGI response is lost."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.security import create_access_token
from app.db.models.annotation import Annotation
from app.db.models.annotation_operation import AnnotationOperation
from app.db.models.audit_log import AuditLog
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User
from app.services.audit import AuditAction
from tests.factory import create_project, create_task, create_user


class _LoseFirstCompletedResponse:
    """Drop one completed ASGI response after the application has sent it."""

    def __init__(self, app) -> None:
        self.app = app
        self.dropped = False

    async def __call__(self, scope, receive, send) -> None:
        status_code = None
        completed = False

        async def observe_send(message) -> None:
            nonlocal status_code, completed
            if message["type"] == "http.response.start":
                status_code = message["status"]
            elif message["type"] == "http.response.body" and not message.get(
                "more_body", False
            ):
                completed = True
            await send(message)

        await self.app(scope, receive, observe_send)
        if (
            scope["type"] == "http"
            and scope["method"] == "POST"
            and scope["path"].endswith("/annotations")
            and status_code == 201
            and completed
            and not self.dropped
        ):
            self.dropped = True
            raise httpx.ReadError("simulated loss after completed ASGI response")


@pytest.fixture
async def committed_annotation_world(test_db_url, apply_migrations, app_module):
    """Use committed rows and a new DB session for every real route request."""

    engine = create_async_engine(test_db_url, echo=False)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    owner_id = successor_id = project_id = task_id = None
    app = app_module
    old_overrides = app.dependency_overrides.copy()
    route_sessions = []

    async def committed_get_db():
        async with sessions() as db:
            route_sessions.append(db)
            yield db

    from app.deps import get_db

    app.dependency_overrides[get_db] = committed_get_db
    try:
        async with sessions() as db:
            owner = await create_user(
                db,
                "project_admin",
                f"response-loss-{uuid.uuid4().hex}@test.local",
                "Response loss owner",
            )
            owner_id = owner.id
            successor = await create_user(
                db,
                "super_admin",
                f"response-loss-successor-{uuid.uuid4().hex}@test.local",
                "Response loss successor",
            )
            successor_id = successor.id
            project = await create_project(db, owner_id=owner.id)
            project_id = project.id
            task = await create_task(db, project_id=project.id, status="in_progress")
            task_id = task.id
            await db.commit()

        yield SimpleNamespace(
            app=app,
            sessions=sessions,
            route_sessions=route_sessions,
            owner_id=owner_id,
            successor_id=successor_id,
            project_id=project_id,
            task_id=task_id,
            token=create_access_token(subject=str(owner_id), role="project_admin"),
            key=f"response-loss-{uuid.uuid4().hex}",
        )
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(old_overrides)
        if any(
            value is not None for value in (owner_id, successor_id, project_id, task_id)
        ):
            async with sessions() as db:
                await db.execute(text("SET LOCAL app.allow_audit_update = 'true'"))
                user_ids = [
                    user_id
                    for user_id in (owner_id, successor_id)
                    if user_id is not None
                ]
                if user_ids:
                    await db.execute(
                        delete(AuditLog).where(AuditLog.actor_id.in_(user_ids))
                    )
                if task_id is not None:
                    await db.execute(
                        delete(AnnotationOperation).where(
                            AnnotationOperation.task_id == task_id
                        )
                    )
                    await db.execute(
                        delete(Annotation).where(Annotation.task_id == task_id)
                    )
                    await db.execute(delete(Task).where(Task.id == task_id))
                if project_id is not None:
                    await db.execute(delete(Project).where(Project.id == project_id))
                if user_ids:
                    await db.execute(delete(User).where(User.id.in_(user_ids)))
                await db.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_lost_create_response_replays_receipt_and_rechecks_current_authority(
    committed_annotation_world,
):
    world = committed_annotation_world
    path = f"/api/v1/tasks/{world.task_id}/annotations"
    headers = {
        "Authorization": f"Bearer {world.token}",
        "Idempotency-Key": world.key,
    }
    payload = {
        "annotation_type": "bbox",
        "class_name": "car",
        "geometry": {"type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
    }
    dropped = _LoseFirstCompletedResponse(world.app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=dropped), base_url="http://test"
    ) as client:
        with pytest.raises(httpx.ReadError, match="simulated loss"):
            await client.post(path, json=payload, headers=headers)

    assert dropped.dropped
    assert len(world.route_sessions) == 1

    async with world.sessions() as observer:
        operation = await observer.scalar(
            select(AnnotationOperation).where(
                AnnotationOperation.task_id == world.task_id,
                AnnotationOperation.actor_id == world.owner_id,
                AnnotationOperation.idempotency_key == world.key,
            )
        )
        assert operation is not None
        receipt = operation.response_json
        annotation_id = uuid.UUID(receipt["id"])
        assert (
            await observer.scalar(
                select(func.count())
                .select_from(Annotation)
                .where(Annotation.task_id == world.task_id)
            )
            == 1
        )
        assert (await observer.get(Task, world.task_id)).total_annotations == 1
        assert await observer.get(Annotation, annotation_id) is not None
        assert (
            await observer.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.actor_id == world.owner_id,
                    AuditLog.action == AuditAction.ANNOTATION_CREATE.value,
                    AuditLog.target_type == "annotation",
                    AuditLog.target_id == str(annotation_id),
                )
            )
            == 1
        )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=world.app), base_url="http://test"
    ) as client:
        replay = await client.post(path, json=payload, headers=headers)

    assert replay.status_code == 201, replay.text
    assert replay.json() == receipt
    assert len(world.route_sessions) == 2
    assert world.route_sessions[0] is not world.route_sessions[1]

    async with world.sessions() as admin:
        project = await admin.get(Project, world.project_id)
        project.owner_id = world.successor_id
        await admin.commit()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=world.app), base_url="http://test"
    ) as client:
        revoked_replay = await client.post(path, json=payload, headers=headers)

    assert revoked_replay.status_code == 404, revoked_replay.text
    assert len(world.route_sessions) == 3
    async with world.sessions() as observer:
        assert (
            await observer.scalar(
                select(func.count())
                .select_from(Annotation)
                .where(Annotation.task_id == world.task_id)
            )
            == 1
        )
        assert (await observer.get(Task, world.task_id)).total_annotations == 1
        assert (
            await observer.scalar(
                select(func.count())
                .select_from(AnnotationOperation)
                .where(
                    AnnotationOperation.task_id == world.task_id,
                    AnnotationOperation.actor_id == world.owner_id,
                    AnnotationOperation.idempotency_key == world.key,
                )
            )
            == 1
        )
        assert (
            await observer.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.actor_id == world.owner_id,
                    AuditLog.action == AuditAction.ANNOTATION_CREATE.value,
                    AuditLog.target_type == "annotation",
                    AuditLog.target_id == str(annotation_id),
                )
            )
            == 1
        )
