"""HTTP and durable transaction acceptance for the secondary-inference write port."""

from __future__ import annotations

import os
import uuid
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import delete, event, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.security import create_access_token
from app.db.models.annotation import Annotation
from app.db.models.audit_log import AuditLog
from app.db.models.ml_backend_registry import ProjectMLBackendPool
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User
from app.deps import get_db
from app.services.annotation import AnnotationService
from app.services.audit import AuditAction, AuditService
from app.services.audit_origin_contract import AuditOriginContext
from app.services.identity_contracts import IdentityActorFacts
from app.services.labeling_core_read_relations import (
    annotation_row,
    get_core_row,
    task_row,
)
from app.services.labeling_write_scope import capture_labeling_write_scope
from app.services.secondary_inference_result import (
    SecondaryInferenceResult,
)
from yoao_inference_contract.execution import ModelTaskResult, freeze_json, thaw_json
from tests.conftest import create_registry_with_pool
from tests.factory import create_project, create_task, create_user
from tests.inference_execution_test_ports import FakeModelExecutionPort
from tests.managed_image_inference_fixtures import (
    attach_managed,
    input_world as input_world,
    sync_world as sync_world,
)
from tests.test_workspace_project_authority_locking import _api_url


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


async def _seed_secondary_world(sessions, *, managed):
    async with sessions() as db:
        actor = await create_user(
            db,
            "project_admin",
            f"secondary-inference-{uuid.uuid4().hex}@test.local",
            "Secondary inference owner",
        )
        project = await create_project(db, owner_id=actor.id)
        task = await create_task(db, project_id=project.id, status="in_progress")
        if managed:
            await attach_managed(db, project, actor, task=task, size=(128, 128))
        annotation = await AnnotationService(db).create(
            task_id=task.id,
            user_id=actor.id,
            annotation_type="bbox",
            class_name="car",
            geometry={"type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
            tool_unit_id="bbox",
        )
        await db.commit()
        return SimpleNamespace(
            sessions=sessions,
            actor_id=actor.id,
            project_id=project.id,
            task_id=task.id,
            annotation_id=annotation.id,
            initial_version=annotation.version,
            initial_count=task.total_annotations,
        )


@pytest.fixture
async def committed_secondary_inference_world(test_engine):
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    world = await _seed_secondary_world(sessions, managed=False)
    try:
        yield world
    finally:
        async with sessions.begin() as db:
            await db.execute(text("SET LOCAL app.allow_audit_update = 'true'"))
            await db.execute(
                delete(AuditLog).where(AuditLog.actor_id == world.actor_id)
            )
            await db.execute(
                delete(Annotation).where(Annotation.task_id == world.task_id)
            )
            await db.execute(delete(Task).where(Task.id == world.task_id))
            await db.execute(delete(Project).where(Project.id == world.project_id))
            await db.execute(delete(User).where(User.id == world.actor_id))


@pytest.fixture
async def controlled_secondary_inference(monkeypatch, sync_world, app_module):
    world = await _seed_secondary_world(sync_world.factory, managed=True)
    async with world.sessions.begin() as db:
        backend, pool = await create_registry_with_pool(
            db,
            name="secondary-" + uuid.uuid4().hex[:8],
            enabled_pool=True,
            health_meta={
                "capabilities": {
                    "task_contract_revision": 1,
                    "models": [
                        {
                            "id": "cls",
                            "task": "classify",
                            "supported_inputs": ["crop"],
                            "output_attribute_types": ["class"],
                            "supported_geometric_outputs": [],
                            "resource_profile": {"device": "cpu", "batchable": True},
                        },
                        {
                            "id": "det",
                            "task": "detect",
                            "supported_inputs": ["crop"],
                            "supported_geometric_outputs": ["bbox"],
                            "resource_profile": {"device": "cpu", "batchable": True},
                        },
                    ],
                }
            },
        )
        db.add(
            ProjectMLBackendPool(
                project_id=world.project_id, pool_id=pool.id, enabled=True
            )
        )
        world.backend_id = backend.id
        world.pool_id = pool.id

    def predict(operation, task_input):
        if thaw_json(operation.context)["model_id"] == "cls":
            result = [{"attributes": {"color": "blue"}, "score": 0.9}]
        else:
            result = [
                {
                    "type": "rectanglelabels",
                    "value": {
                        "x": 20.0,
                        "y": 20.0,
                        "width": 40.0,
                        "height": 40.0,
                        "rectanglelabels": ["car"],
                    },
                    "score": 0.9,
                }
            ]
        return ModelTaskResult(task_id=thaw_json(task_input)["id"], result=result)

    monkeypatch.setattr(
        "app.services.inference_model_execution.model_execution_port",
        lambda _db: FakeModelExecutionPort(predict),
    )
    world.app = app_module
    world.route_sessions = []
    world.token = create_access_token(subject=str(world.actor_id), role="project_admin")

    async def committed_get_db():
        async with world.sessions() as db:
            world.route_sessions.append(db)
            yield db

    monkeypatch.setitem(app_module.dependency_overrides, get_db, committed_get_db)
    yield world


async def _post_secondary_inference(world, client, write_target):
    return await client.post(
        f"/api/v1/tasks/{world.task_id}/annotations/{world.annotation_id}/secondary-inference",
        headers={"Authorization": f"Bearer {world.token}"},
        json={
            "ml_backend_id": str(world.backend_id),
            "write_target": write_target,
            "model_id": "cls" if write_target == "attributes" else "det",
        },
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


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
    reason="requires the owned module-data verifier and actual API LOGIN",
)
@pytest.mark.parametrize("write_target", ["attributes", "geometry"])
async def test_secondary_result_commits_with_restricted_api_login(
    committed_secondary_inference_world, test_engine, write_target
):
    from app.application.annotation_secondary_inference import (
        _AnnotationSecondaryInferenceSqlAdapter,
    )

    world = committed_secondary_inference_world
    async with world.sessions() as observer:
        user = await observer.get(User, world.actor_id)
        actor = IdentityActorFacts(
            user.id, user.name, user.email, user.role, user.is_active
        )

    restricted = create_async_engine(_api_url(test_engine), hide_parameters=True)
    sessions = async_sessionmaker(restricted, expire_on_commit=False)
    try:
        async with sessions() as db:
            assert (
                await db.scalar(text("SELECT current_user")) == "identity_api_consumer"
            )
            for table in ("tasks", "annotations"):
                for statement in (
                    f"SELECT id FROM public.{table} LIMIT 1",
                    f"UPDATE public.{table} SET version = 1 WHERE false",
                ):
                    with pytest.raises(DBAPIError) as denied:
                        async with db.begin_nested():
                            await db.execute(text(statement))
                    assert denied.value.orig.sqlstate == "42501"

            task = await get_core_row(db, task_row, world.task_id)
            parent = await get_core_row(db, annotation_row, world.annotation_id)
            scope = capture_labeling_write_scope(
                task,
                actor_id=actor.id,
                actor_known=True,
                operation="secondary_inference",
                execution_scope=None,
                execution_scope_known=True,
                job_id=None,
                job_id_known=True,
                source_annotation=parent,
            )
            writes_attributes = write_target == "attributes"
            result = SecondaryInferenceResult(
                write_target=write_target,
                model_ref=(("model_id", "secondary-test"),),
                attributes=(("color", freeze_json("blue")),)
                if writes_attributes
                else (),
                geometry_shapes=(
                    ()
                    if writes_attributes
                    else (
                        freeze_json(
                            {
                                "type": "bbox",
                                "class_name": "car",
                                "geometry": {
                                    "type": "bbox",
                                    "x": 0.2,
                                    "y": 0.2,
                                    "w": 0.3,
                                    "h": 0.4,
                                },
                            }
                        ),
                    )
                ),
            )
            response = await _AnnotationSecondaryInferenceSqlAdapter(
                db, task=task, annotation=parent, actor=actor
            ).apply_secondary_inference(
                write_scope=scope,
                result=result,
                write_target=write_target,
                audit_origin=AuditOriginContext(
                    method="POST", path="/fixture", ip=None
                ),
            )
    finally:
        await restricted.dispose()

    async with world.sessions() as observer:
        parent = await observer.get(Annotation, world.annotation_id)
        task = await observer.get(Task, world.task_id)
        if writes_attributes:
            assert response.created_children == []
            assert parent.version == world.initial_version + 1
            assert parent.attributes == {"color": "blue"}
            assert parent.attributes_meta["color"]["origin"] == "ai"
            assert task.total_annotations == world.initial_count
        else:
            assert len(response.created_children) == 1
            child = await observer.get(Annotation, response.created_children[0].id)
            assert child.parent_annotation_id == parent.id
            assert child.task_id == task.id
            assert child.project_id == world.project_id
            assert child.user_id == world.actor_id
            assert (child.source, child.version) == ("prediction_based", 1)
            assert parent.version == world.initial_version
            assert task.total_annotations == world.initial_count + 1
        audit = (
            await observer.scalars(
                select(AuditLog).where(
                    AuditLog.actor_id == world.actor_id,
                    AuditLog.target_id == str(parent.id),
                    AuditLog.action == AuditAction.ANNOTATION_UPDATE.value,
                )
            )
        ).one()
        assert audit.detail_json["write_target"] == write_target
