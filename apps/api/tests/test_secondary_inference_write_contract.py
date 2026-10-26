"""HTTP and durable transaction acceptance for the secondary-inference write port."""

from __future__ import annotations

import uuid
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import delete, event, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.security import create_access_token
from app.db.models.annotation import Annotation
from app.db.models.audit_log import AuditLog
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User
from app.services.audit import AuditAction, AuditService
from app.services.audit_origin_contract import AuditOriginContext
from app.services.labeling_write_scope import capture_labeling_write_scope
from app.services.secondary_inference_result import (
    SecondaryInferenceResult,
)
from aap_inference_contract.execution import (
    ExecutionProvenance,
    freeze_json,
)
from tests.factory import create_project, create_task, create_user


@pytest.mark.parametrize(
    "field", ["task_id", "project_id", "source_annotation_id", "actor_id"]
)
async def test_secondary_inference_rejects_cross_bound_scope_before_sql(
    committed_secondary_inference_world, field
):
    from app.application.annotation_secondary_inference import (
        _AnnotationSecondaryInferenceSqlAdapter,
    )

    world = committed_secondary_inference_world
    async with world.sessions() as db:
        actor = await db.get(User, world.actor_id)
        task = await db.get(Task, world.task_id)
        annotation = await db.get(Annotation, world.annotation_id)
        scope = capture_labeling_write_scope(
            task,
            actor_id=actor.id,
            actor_known=True,
            operation="secondary_inference",
            execution_scope=None,
            execution_scope_known=True,
            job_id=None,
            job_id_known=True,
            source_annotation=annotation,
        )
        adapter = _AnnotationSecondaryInferenceSqlAdapter(
            db,
            task=task,
            annotation=annotation,
            actor=actor,
        )
        statements = []

        def count(_connection, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)

        event.listen(db.bind.sync_engine, "before_cursor_execute", count)
        try:
            with pytest.raises(ValueError):
                await adapter.apply_secondary_inference(
                    write_scope=replace(scope, **{field: uuid.uuid4()}),
                    result=SecondaryInferenceResult(
                        write_target="attributes",
                        model_ref=(),
                        attributes=(),
                        geometry_shapes=(),
                    ),
                    write_target="attributes",
                    audit_origin=AuditOriginContext(
                        method="POST", path="/fixture", ip=None
                    ),
                )
        finally:
            event.remove(db.bind.sync_engine, "before_cursor_execute", count)
        assert statements == []


@pytest.fixture
async def committed_secondary_inference_world(
    test_db_url, apply_migrations, app_module
):
    """Give each route request a real Session and observe commits independently."""

    from app.deps import get_db
    from app.services.annotation import AnnotationService

    engine = create_async_engine(test_db_url, echo=False)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    actor_id = project_id = task_id = annotation_id = None
    app = app_module
    old_overrides = app.dependency_overrides.copy()
    route_sessions = []

    async def committed_get_db():
        async with sessions() as db:
            route_sessions.append(db)
            yield db

    app.dependency_overrides[get_db] = committed_get_db
    try:
        async with sessions() as db:
            actor = await create_user(
                db,
                "project_admin",
                f"secondary-inference-{uuid.uuid4().hex}@test.local",
                "Secondary inference owner",
            )
            actor_id = actor.id
            project = await create_project(db, owner_id=actor.id)
            project_id = project.id
            task = await create_task(db, project_id=project.id, status="in_progress")
            task_id = task.id
            annotation = await AnnotationService(db).create(
                task_id=task.id,
                user_id=actor.id,
                annotation_type="bbox",
                class_name="car",
                geometry={"type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
                tool_unit_id="bbox",
            )
            annotation_id = annotation.id
            initial_version = annotation.version
            initial_count = task.total_annotations
            await db.commit()

        yield SimpleNamespace(
            app=app,
            sessions=sessions,
            route_sessions=route_sessions,
            actor_id=actor_id,
            project_id=project_id,
            task_id=task_id,
            annotation_id=annotation_id,
            initial_version=initial_version,
            initial_count=initial_count,
            token=create_access_token(subject=str(actor_id), role="project_admin"),
            backend_id=uuid.uuid4(),
            pool_id=uuid.uuid4(),
        )
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(old_overrides)
        if any(value is not None for value in (actor_id, project_id, task_id)):
            async with sessions() as db:
                await db.execute(text("SET LOCAL app.allow_audit_update = 'true'"))
                if actor_id is not None:
                    await db.execute(
                        delete(AuditLog).where(AuditLog.actor_id == actor_id)
                    )
                if task_id is not None:
                    await db.execute(
                        delete(Annotation).where(Annotation.task_id == task_id)
                    )
                    await db.execute(delete(Task).where(Task.id == task_id))
                if project_id is not None:
                    await db.execute(delete(Project).where(Project.id == project_id))
                if actor_id is not None:
                    await db.execute(delete(User).where(User.id == actor_id))
                await db.commit()
        await engine.dispose()


@pytest.fixture
def controlled_secondary_inference(monkeypatch, committed_secondary_inference_world):
    from app.api.v1.tasks import annotations as annotations_api
    from app.api.v1 import projects

    world = committed_secondary_inference_world

    class _FakeMLBackendService:
        def __init__(self, _db):
            pass

        async def get_registry(self, backend_id):
            return SimpleNamespace(id=backend_id, health_meta=None)

        async def is_enabled(self, _project_id, _backend_id):
            return True

        async def pool_id_for_registry(self, _backend_id):
            return world.pool_id

    async def run_controlled(_db, *, write_target, **_kwargs):
        provenance = ExecutionProvenance(
            requested_registry_id=world.backend_id,
            requested_pool_id=world.pool_id,
            registry_id=world.backend_id,
            pool_id=world.pool_id,
            instance_id=world.backend_id,
            route_scope_id=uuid.uuid4(),
            attempt_id=uuid.uuid4(),
        )
        if write_target == "attributes":
            return SecondaryInferenceResult(
                write_target="attributes",
                model_ref=(("model_id", "test-classifier"),),
                attributes=(("color", freeze_json("blue")),),
                geometry_shapes=(),
                execution_provenance=provenance,
            )
        return SecondaryInferenceResult(
            write_target="geometry",
            model_ref=(("model_id", "test-detector"),),
            attributes=(),
            geometry_shapes=(
                freeze_json(
                    {
                        "type": "bbox",
                        "class_name": "car",
                        "geometry": {
                            "type": "bbox",
                            "x": 0.3,
                            "y": 0.2,
                            "w": 0.4,
                            "h": 0.3,
                        },
                        "confidence": 0.9,
                    }
                ),
            ),
            execution_provenance=provenance,
        )

    monkeypatch.setattr(annotations_api, "MLBackendService", _FakeMLBackendService)
    monkeypatch.setattr(projects, "_stage_model", lambda *_args: {})
    monkeypatch.setattr(
        annotations_api, "check_capability_violations", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(annotations_api, "run_secondary_inference", run_controlled)
    return world


async def _post_secondary_inference(world, client, write_target):
    return await client.post(
        f"/api/v1/tasks/{world.task_id}/annotations/{world.annotation_id}/secondary-inference",
        headers={"Authorization": f"Bearer {world.token}"},
        json={"ml_backend_id": str(world.backend_id), "write_target": write_target},
    )


@pytest.mark.asyncio
async def test_secondary_inference_http_commits_attribute_and_audit(
    controlled_secondary_inference,
):
    world = controlled_secondary_inference
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=world.app), base_url="http://test"
    ) as client:
        response = await _post_secondary_inference(world, client, "attributes")

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["annotation"]["attributes"]["color"] == "blue"
    assert body["annotation"]["attributes_meta"]["color"]["origin"] == "ai"
    assert body["created_children"] == []
    assert len(world.route_sessions) == 1

    async with world.sessions() as observer:
        annotation = await observer.get(Annotation, world.annotation_id)
        task = await observer.get(Task, world.task_id)
        assert annotation.attributes["color"] == "blue"
        assert annotation.version == world.initial_version + 1
        assert task.total_annotations == world.initial_count
        assert (
            await observer.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.actor_id == world.actor_id,
                    AuditLog.action == AuditAction.ANNOTATION_UPDATE.value,
                    AuditLog.target_id == str(world.annotation_id),
                )
            )
            == 1
        )
        audit = await observer.scalar(
            select(AuditLog).where(
                AuditLog.actor_id == world.actor_id,
                AuditLog.action == AuditAction.ANNOTATION_UPDATE.value,
                AuditLog.target_id == str(world.annotation_id),
            )
        )
        assert audit is not None
        assert audit.detail_json["ml_backend_pool_id"] == str(world.pool_id)
        assert audit.detail_json["write_target"] == "attributes"
        assert audit.detail_json["created_children"] == 0
        assert audit.method == "POST"
        assert audit.path.endswith(
            f"/tasks/{world.task_id}/annotations/{world.annotation_id}/secondary-inference"
        )


@pytest.mark.asyncio
async def test_secondary_inference_http_commits_child_and_task_count(
    controlled_secondary_inference,
):
    world = controlled_secondary_inference
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=world.app), base_url="http://test"
    ) as client:
        response = await _post_secondary_inference(world, client, "geometry")

    assert response.status_code == 201, response.text
    body = response.json()
    assert len(body["created_children"]) == 1
    child_id = uuid.UUID(body["created_children"][0]["id"])
    assert body["created_children"][0]["parent_annotation_id"] == str(
        world.annotation_id
    )
    assert body["created_children"][0]["source"] == "prediction_based"
    assert len(world.route_sessions) == 1

    async with world.sessions() as observer:
        child = await observer.get(Annotation, child_id)
        source = await observer.get(Annotation, world.annotation_id)
        task = await observer.get(Task, world.task_id)
        assert child is not None
        assert source.version == world.initial_version
        assert child.parent_annotation_id == world.annotation_id
        assert child.source == "prediction_based"
        assert task.total_annotations == world.initial_count + 1
        assert (
            await observer.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.actor_id == world.actor_id,
                    AuditLog.action == AuditAction.ANNOTATION_UPDATE.value,
                    AuditLog.target_id == str(world.annotation_id),
                )
            )
            == 1
        )
        audit = await observer.scalar(
            select(AuditLog).where(
                AuditLog.actor_id == world.actor_id,
                AuditLog.action == AuditAction.ANNOTATION_UPDATE.value,
                AuditLog.target_id == str(world.annotation_id),
            )
        )
        assert audit is not None
        assert audit.detail_json["created_children"] == 1
        assert audit.detail_json["write_target"] == "geometry"


@pytest.mark.asyncio
async def test_secondary_inference_http_rolls_back_write_and_audit_after_apply(
    controlled_secondary_inference, monkeypatch
):
    from app.application import annotation_secondary_inference

    world = controlled_secondary_inference
    write_audit_then_fail = AuditService.log

    async def fail_after_audit_flush(db, **kwargs):
        await write_audit_then_fail(db, **kwargs)
        await db.flush()
        raise HTTPException(status_code=503, detail={"reason": "injected_after_apply"})

    monkeypatch.setattr(
        annotation_secondary_inference.AuditService, "log", fail_after_audit_flush
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=world.app), base_url="http://test"
    ) as client:
        response = await _post_secondary_inference(world, client, "geometry")

    assert response.status_code == 503
    assert response.json()["detail"] == {"reason": "injected_after_apply"}
    assert len(world.route_sessions) == 1

    async with world.sessions() as observer:
        annotation = await observer.get(Annotation, world.annotation_id)
        task = await observer.get(Task, world.task_id)
        assert annotation.version == world.initial_version
        assert annotation.attributes == {}
        assert annotation.attributes_meta == {}
        assert task.total_annotations == world.initial_count
        assert (
            await observer.scalar(
                select(func.count())
                .select_from(Annotation)
                .where(
                    Annotation.task_id == world.task_id,
                    Annotation.parent_annotation_id == world.annotation_id,
                )
            )
            == 0
        )
        assert (
            await observer.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.actor_id == world.actor_id,
                    AuditLog.action == AuditAction.ANNOTATION_UPDATE.value,
                    AuditLog.target_id == str(world.annotation_id),
                )
            )
            == 0
        )
