from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager

import httpx
import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.security import create_access_token
from app.db.models.annotation import Annotation
from app.db.models.audit_log import AuditLog
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.user import User
from tests.factory import create_project, create_task, create_user


_CLEANUP_ACTION = "project.cleanup_annotation_orphans"


def _tool_bindings() -> dict:
    return {
        "bbox": {
            "enabled": True,
            "classes": [{"name": "car", "order": 0}],
            "attribute_schema": {
                "fields": [{"key": "color", "label": "Color", "type": "text"}]
            },
        }
    }


async def _seed_cleanup_world(
    maker,
    *,
    transfer_target: bool = False,
    retarget: bool = False,
    foreign_project: bool = False,
):
    async with maker() as session:
        owner = await create_user(
            session,
            "project_admin",
            f"cleanup-owner-{uuid.uuid4()}@test.local",
            "Cleanup owner",
        )
        recipient = None
        if transfer_target:
            recipient = await create_user(
                session,
                "project_admin",
                f"cleanup-recipient-{uuid.uuid4()}@test.local",
                "Next owner",
            )
        project = await create_project(
            session,
            owner_id=owner.id,
            name="Schema cleanup race",
            classes=["car"],
        )
        project.tool_bindings = _tool_bindings()
        foreign = None
        if foreign_project:
            foreign = await create_project(
                session,
                owner_id=owner.id,
                name="Schema cleanup retarget destination",
                classes=["car"],
            )
        task = await create_task(
            session,
            project_id=project.id,
            status="in_progress",
        )
        task.total_annotations = 1
        task.is_labeled = True
        task.annotation_contributor_ids = []
        target_task = None
        if retarget:
            target_task = await create_task(
                session,
                project_id=project.id,
                status="pending",
            )
        annotation = Annotation(
            task_id=task.id,
            project_id=project.id,
            user_id=owner.id,
            source="manual",
            annotation_type="bbox",
            tool_unit_id="bbox",
            class_name="dog",
            geometry={"type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
            attributes={"color": "red", "legacy": "stale"},
        )
        session.add(annotation)
        await session.flush()
        await session.commit()
        return {
            "owner_id": owner.id,
            "recipient_id": recipient.id if recipient is not None else None,
            "project_id": project.id,
            "task_id": task.id,
            "target_task_id": target_task.id if target_task is not None else None,
            "foreign_project_id": foreign.id if foreign is not None else None,
            "annotation_id": annotation.id,
            "token": create_access_token(subject=str(owner.id), role=owner.role),
        }


async def _drop_cleanup_world(maker, world: dict) -> None:
    if os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") == "1":
        from tests.labeling_class_query_helpers import assert_owned_test_database

        await assert_owned_test_database(maker.kw["bind"])
        # Retain immutable Audit rows and their actors until owned database disposal.
        return
    async with maker() as cleanup:
        project_ids = [world["project_id"]]
        if world["foreign_project_id"] is not None:
            project_ids.append(world["foreign_project_id"])
        task_ids = [world["task_id"]]
        if world["target_task_id"] is not None:
            task_ids.append(world["target_task_id"])
        await cleanup.execute(
            delete(AuditLog).where(
                AuditLog.action == _CLEANUP_ACTION,
                AuditLog.target_id == str(world["project_id"]),
            )
        )
        await cleanup.execute(
            delete(Annotation).where(Annotation.project_id == world["project_id"])
        )
        await cleanup.execute(
            delete(TaskBatch).where(TaskBatch.project_id.in_(project_ids))
        )
        await cleanup.execute(delete(Task).where(Task.id.in_(task_ids)))
        await cleanup.execute(delete(Project).where(Project.id.in_(project_ids)))
        user_ids = [world["owner_id"]]
        if world["recipient_id"] is not None:
            user_ids.append(world["recipient_id"])
        await cleanup.execute(delete(User).where(User.id.in_(user_ids)))
        await cleanup.commit()


@asynccontextmanager
async def _asgi_client_for_maker(app, maker, *, raise_app_exceptions: bool = True):
    from app.deps import get_db

    missing = object()
    previous = app.dependency_overrides.get(get_db, missing)

    async def override_db():
        async with maker() as session:
            try:
                yield session
            finally:
                if session.in_transaction():
                    await session.rollback()

    app.dependency_overrides[get_db] = override_db
    try:
        transport = httpx.ASGITransport(
            app=app,
            raise_app_exceptions=raise_app_exceptions,
        )
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            yield client
    finally:
        if previous is missing:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous


def _headers(world: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {world['token']}"}


async def _audit_count(session, project_id: uuid.UUID) -> int:
    return int(
        await session.scalar(
            select(text("count(*)"))
            .select_from(AuditLog)
            .where(
                AuditLog.action == _CLEANUP_ACTION,
                AuditLog.target_id == str(project_id),
            )
        )
        or 0
    )


@pytest.mark.parametrize(
    ("race", "expected_status"),
    [("disabled_account", 401), ("transferred_project", 404)],
)
async def test_cleanup_rechecks_current_management_after_route_authorization(
    test_engine,
    app_module,
    monkeypatch,
    race,
    expected_status,
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    world = await _seed_cleanup_world(
        maker,
        transfer_target=race == "transferred_project",
    )
    import app.api.v1.projects as projects_route

    cleanup = projects_route.cleanup_project_annotation_orphans
    intervened = False

    async def revoke_after_dependency(db, **kwargs):
        nonlocal intervened
        if not intervened:
            async with maker() as revoker:
                if race == "disabled_account":
                    await revoker.execute(
                        update(User)
                        .where(User.id == world["owner_id"])
                        .values(is_active=False)
                    )
                else:
                    await revoker.execute(
                        update(Project)
                        .where(Project.id == world["project_id"])
                        .values(owner_id=world["recipient_id"])
                    )
                await revoker.commit()
            intervened = True
        return await cleanup(db, **kwargs)

    monkeypatch.setattr(
        projects_route,
        "cleanup_project_annotation_orphans",
        revoke_after_dependency,
    )
    try:
        async with _asgi_client_for_maker(app_module, maker) as client:
            response = await client.post(
                f"/api/v1/projects/{world['project_id']}/cleanup-orphans",
                headers=_headers(world),
                json={"dry_run": False},
            )
        assert intervened is True
        assert response.status_code == expected_status
        async with maker() as check:
            annotation = await check.get(Annotation, world["annotation_id"])
            task = await check.get(Task, world["task_id"])
            project = await check.get(Project, world["project_id"])
            assert annotation.is_active is True
            assert annotation.attributes == {"color": "red", "legacy": "stale"}
            assert task.total_annotations == 1
            assert task.annotation_contributor_ids == []
            assert project.owner_id == (
                world["recipient_id"]
                if race == "transferred_project"
                else world["owner_id"]
            )
            assert await _audit_count(check, world["project_id"]) == 0
    finally:
        await _drop_cleanup_world(maker, world)


async def test_cleanup_rejects_annotation_task_retarget_after_candidate_read(
    test_engine,
    app_module,
    monkeypatch,
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    world = await _seed_cleanup_world(maker, retarget=True)
    from app.services.labeling_annotation_maintenance import (
        LabelingAnnotationMaintenanceService,
    )

    command = LabelingAnnotationMaintenanceService._command
    retargeted = False

    async def retarget_at_cleanup_command(self, request):
        nonlocal retargeted
        if request.operation == "cleanup_schema" and not retargeted:
            async with maker() as mover:
                await mover.execute(
                    update(Annotation)
                    .where(Annotation.id == world["annotation_id"])
                    .values(task_id=world["target_task_id"])
                )
                await mover.commit()
            retargeted = True
        return await command(self, request)

    monkeypatch.setattr(
        LabelingAnnotationMaintenanceService,
        "_command",
        retarget_at_cleanup_command,
    )
    try:
        async with _asgi_client_for_maker(app_module, maker) as client:
            response = await client.post(
                f"/api/v1/projects/{world['project_id']}/cleanup-orphans",
                headers=_headers(world),
                json={"dry_run": False},
            )
        assert retargeted is True
        assert response.status_code == 409
        assert response.json()["detail"]["reason"] == "annotation_target_changed"
        async with maker() as check:
            annotation = await check.get(Annotation, world["annotation_id"])
            task = await check.get(Task, world["task_id"])
            target = await check.get(Task, world["target_task_id"])
            assert annotation.task_id == world["target_task_id"]
            assert annotation.is_active is True
            assert annotation.attributes == {"color": "red", "legacy": "stale"}
            assert task.total_annotations == 1
            assert task.annotation_contributor_ids == []
            assert target.total_annotations == 0
            assert await _audit_count(check, world["project_id"]) == 0
    finally:
        await _drop_cleanup_world(maker, world)


async def test_cleanup_rejects_task_project_retarget_after_candidate_read(
    test_engine,
    app_module,
    monkeypatch,
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    world = await _seed_cleanup_world(maker, foreign_project=True)
    from app.services.labeling_annotation_maintenance import (
        LabelingAnnotationMaintenanceService,
    )

    command = LabelingAnnotationMaintenanceService._command
    retargeted = False

    async def retarget_at_cleanup_command(self, request):
        nonlocal retargeted
        if request.operation == "cleanup_schema" and not retargeted:
            async with maker() as mover:
                await mover.execute(
                    update(Task)
                    .where(Task.id == world["task_id"])
                    .values(project_id=world["foreign_project_id"])
                )
                await mover.commit()
            retargeted = True
        return await command(self, request)

    monkeypatch.setattr(
        LabelingAnnotationMaintenanceService,
        "_command",
        retarget_at_cleanup_command,
    )
    try:
        async with _asgi_client_for_maker(app_module, maker) as client:
            response = await client.post(
                f"/api/v1/projects/{world['project_id']}/cleanup-orphans",
                headers=_headers(world),
                json={"dry_run": False},
            )
        assert retargeted is True
        assert response.status_code == 409
        assert response.json()["detail"]["reason"] == "task_target_changed"
        async with maker() as check:
            annotation = await check.get(Annotation, world["annotation_id"])
            task = await check.get(Task, world["task_id"])
            assert annotation.is_active is True
            assert annotation.attributes == {"color": "red", "legacy": "stale"}
            assert task.project_id == world["foreign_project_id"]
            assert task.total_annotations == 1
            assert task.annotation_contributor_ids == []
            assert await _audit_count(check, world["project_id"]) == 0
    finally:
        await _drop_cleanup_world(maker, world)


async def test_cleanup_nowait_task_contention_returns_conflict_without_mutation(
    test_engine,
    app_module,
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    world = await _seed_cleanup_world(maker)
    try:
        async with maker() as holder:
            await holder.execute(
                select(Task.id).where(Task.id == world["task_id"]).with_for_update()
            )
            async with _asgi_client_for_maker(app_module, maker) as client:
                response = await asyncio.wait_for(
                    client.post(
                        f"/api/v1/projects/{world['project_id']}/cleanup-orphans",
                        headers=_headers(world),
                        json={"dry_run": False},
                    ),
                    timeout=2,
                )
            assert response.status_code == 409
            assert response.json()["detail"]["reason"] == "task_busy"
            await holder.rollback()
        async with maker() as check:
            annotation = await check.get(Annotation, world["annotation_id"])
            task = await check.get(Task, world["task_id"])
            assert annotation.is_active is True
            assert annotation.attributes == {"color": "red", "legacy": "stale"}
            assert task.total_annotations == 1
            assert task.annotation_contributor_ids == []
            assert await _audit_count(check, world["project_id"]) == 0
    finally:
        await _drop_cleanup_world(maker, world)


async def test_cleanup_commit_failure_rolls_back_annotations_evidence_stats_and_audit(
    test_engine,
    app_module,
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    world = await _seed_cleanup_world(maker)
    suffix = uuid.uuid4().hex[:12]
    function_name = f"aap_cleanup_commit_fail_{suffix}"
    trigger_name = f"aap_cleanup_commit_fail_{suffix}"
    try:
        async with maker() as installer:
            await installer.execute(
                text(
                    f"""CREATE FUNCTION {function_name}() RETURNS trigger
                    LANGUAGE plpgsql AS $$
                    BEGIN
                      IF NEW.action = '{_CLEANUP_ACTION}'
                         AND NEW.target_id = '{world["project_id"]}' THEN
                        RAISE EXCEPTION 'injected cleanup commit failure';
                      END IF;
                      RETURN NEW;
                    END;
                    $$"""
                )
            )
            await installer.execute(
                text(
                    f"""CREATE CONSTRAINT TRIGGER {trigger_name}
                    AFTER INSERT ON audit_logs
                    DEFERRABLE INITIALLY DEFERRED
                    FOR EACH ROW EXECUTE FUNCTION {function_name}()"""
                )
            )
            await installer.commit()

        async with _asgi_client_for_maker(
            app_module,
            maker,
            raise_app_exceptions=False,
        ) as client:
            response = await client.post(
                f"/api/v1/projects/{world['project_id']}/cleanup-orphans",
                headers=_headers(world),
                json={"dry_run": False},
            )
        assert response.status_code == 500

        async with maker() as check:
            annotation = await check.get(Annotation, world["annotation_id"])
            task = await check.get(Task, world["task_id"])
            assert annotation.is_active is True
            assert annotation.attributes == {"color": "red", "legacy": "stale"}
            assert task.total_annotations == 1
            assert task.is_labeled is True
            assert task.status == "in_progress"
            assert task.annotation_contributor_ids == []
            assert await _audit_count(check, world["project_id"]) == 0
    finally:
        async with maker() as cleanup:
            await cleanup.execute(
                text(f"DROP TRIGGER IF EXISTS {trigger_name} ON audit_logs")
            )
            await cleanup.execute(text(f"DROP FUNCTION IF EXISTS {function_name}()"))
            await cleanup.commit()
        await _drop_cleanup_world(maker, world)
