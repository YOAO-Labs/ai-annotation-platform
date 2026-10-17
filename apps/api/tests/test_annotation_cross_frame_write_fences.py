"""Manual cross-frame writes revalidate locked task and annotation state."""

from __future__ import annotations

import asyncio
import copy
import contextlib
import uuid

import httpx
import pytest
from sqlalchemy import delete, inspect, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.security import create_access_token
from app.db.models.annotation import Annotation
from app.db.models.audit_log import AuditLog
from app.db.models.dataset import Dataset, DatasetItem, Scene
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.scene_track import SceneTrack, SceneTrackOperation
from app.db.models.task import Task
from app.db.models.task_lock import TaskLock
from app.db.models.user import User
from app.deps import get_db
from app.services.scene_track_domain import bind_annotation_to_scene_track
from app.services.task_lock import TaskLockService
from tests.factory import create_membership, create_user
from tests.test_annotation_propagate import _add_annotation, _box3d, _seed_scene

_MISSING = object()
_OPERATIONS = ("propagate", "propagate_batch", "interpolate_range")


async def _seed_world(test_engine, *, operation: str, actor_role: str = "super_admin"):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    async with maker() as db:
        owner = await create_user(
            db, "super_admin", f"cross-frame-owner-{suffix}@test.local", "Owner"
        )
        actor = (
            owner
            if actor_role == "super_admin"
            else await create_user(
                db, actor_role, f"cross-frame-actor-{suffix}@test.local", "Actor"
            )
        )
        other = await create_user(
            db, "employee", f"cross-frame-other-{suffix}@test.local", "Other"
        )
        project, dataset, scene, tasks = await _seed_scene(db, owner_id=owner.id, n=3)
        await create_membership(
            db, project_id=project.id, user_id=other.id, role="annotator"
        )
        for task in tasks:
            task.status = "in_progress"
        if actor.id != owner.id:
            await create_membership(
                db, project_id=project.id, user_id=actor.id, role="annotator"
            )
            for task in tasks:
                task.assignee_id = actor.id

        track_id = "trk_cross_frame_write_fence"
        if operation == "interpolate_range":
            source = await _add_annotation(
                db,
                task=tasks[0],
                project=project,
                user_id=actor.id,
                geometry=_box3d(center=(0.0, 2.0, 3.0)),
                track_id=track_id,
            )
            await _add_annotation(
                db,
                task=tasks[2],
                project=project,
                user_id=actor.id,
                geometry=_box3d(center=(10.0, 2.0, 3.0)),
                track_id=track_id,
            )
        else:
            source = await _add_annotation(
                db,
                task=tasks[0],
                project=project,
                user_id=actor.id,
                geometry={"type": "bbox", "x": 1.0, "y": 2.0, "w": 3.0, "h": 4.0},
            )
        await db.commit()

        task_ids = [task.id for task in tasks]
        item_ids = [task.dataset_item_id for task in tasks]
        user_ids = [owner.id, actor.id, other.id]
        return {
            "actor_id": actor.id,
            "token": create_access_token(subject=str(actor.id), role=actor.role),
            "other_id": other.id,
            "project_id": project.id,
            "dataset_id": dataset.id,
            "scene_id": scene.id,
            "task_ids": task_ids,
            "item_ids": item_ids,
            "user_ids": user_ids,
            "source_annotation_id": source.id,
            "track_id": track_id,
            "target_initial_annotation_count": 1
            if operation == "interpolate_range"
            else 0,
        }


async def _cleanup_world(test_engine, world: dict) -> None:
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as db:
        await db.execute(text("SET LOCAL \"app.allow_audit_update\" = 'true'"))
        await db.execute(
            delete(AuditLog).where(AuditLog.actor_id.in_(world["user_ids"]))
        )
        await db.execute(
            delete(Annotation).where(Annotation.task_id.in_(world["task_ids"]))
        )
        await db.execute(
            delete(TaskLock).where(TaskLock.task_id.in_(world["task_ids"]))
        )
        await db.execute(delete(Task).where(Task.id.in_(world["task_ids"])))
        await db.execute(
            delete(SceneTrackOperation).where(
                SceneTrackOperation.scene_id == world["scene_id"]
            )
        )
        await db.execute(
            delete(SceneTrack).where(SceneTrack.scene_id == world["scene_id"])
        )
        await db.execute(
            delete(DatasetItem).where(DatasetItem.id.in_(world["item_ids"]))
        )
        await db.execute(delete(Scene).where(Scene.id == world["scene_id"]))
        await db.execute(delete(Dataset).where(Dataset.id == world["dataset_id"]))
        await db.execute(
            delete(ProjectMember).where(ProjectMember.project_id == world["project_id"])
        )
        await db.execute(delete(Project).where(Project.id == world["project_id"]))
        await db.execute(delete(User).where(User.id.in_(world["user_ids"])))
        await db.commit()


def _request_path_and_body(operation: str, world: dict) -> tuple[str, dict]:
    source_task_id, target_task_id = world["task_ids"][0], world["task_ids"][2]
    annotation_id = world["source_annotation_id"]
    if operation == "propagate":
        return (
            f"/api/v1/tasks/{source_task_id}/annotations/{annotation_id}/propagate-to-task",
            {"target_task_id": str(target_task_id)},
        )
    if operation == "propagate_batch":
        return (
            f"/api/v1/tasks/{source_task_id}/annotations/propagate-batch",
            {
                "target_task_id": str(target_task_id),
                "annotation_ids": [str(annotation_id)],
            },
        )
    return (
        f"/api/v1/tasks/{source_task_id}/annotations/interpolate-range",
        {"track_id": world["track_id"], "to_task_id": str(target_task_id)},
    )


def _pause_after_target_preflight(monkeypatch, target_task_id, entered, release):
    from app.application.annotation_cross_frame import _AnnotationCrossFrameSqlAdapter

    original = _AnnotationCrossFrameSqlAdapter._target_task_for_write

    def assert_preloaded_rows_are_retained(adapter):
        session = adapter._db.sync_session
        cached = session.info.get("cross_frame_cached_annotations", ())
        snapshots = session.info.get("cross_frame_cached_annotation_snapshots", {})
        for annotation in cached:
            assert session.identity_map.contains_state(inspect(annotation))
            assert (
                annotation.is_active,
                annotation.version,
                annotation.track_id,
                copy.deepcopy(annotation.geometry),
            ) == snapshots[annotation.id]

    async def paused(adapter, task_id):
        target = await original(adapter, task_id)
        if task_id == target_task_id and not entered.is_set():
            assert_preloaded_rows_are_retained(adapter)
            entered.set()
            await asyncio.wait_for(release.wait(), timeout=10)
            assert_preloaded_rows_are_retained(adapter)
        return target

    monkeypatch.setattr(
        _AnnotationCrossFrameSqlAdapter, "_target_task_for_write", paused
    )


async def _post_while_writer_changes_state(
    *,
    test_engine,
    app_module,
    world: dict,
    path: str,
    body: dict,
    change,
    gate_setup,
    preload_annotation_ids: tuple[uuid.UUID, ...] = (),
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    entered, release = asyncio.Event(), asyncio.Event()
    gate_setup(entered, release)
    async with maker() as request_db:
        cached_annotations = []
        for annotation_id in preload_annotation_ids:
            annotation = await request_db.get(Annotation, annotation_id)
            assert annotation is not None
            cached_annotations.append(annotation)
        request_db.sync_session.info["cross_frame_cached_annotations"] = (
            cached_annotations
        )
        request_db.sync_session.info["cross_frame_cached_annotation_snapshots"] = {
            annotation.id: (
                annotation.is_active,
                annotation.version,
                annotation.track_id,
                copy.deepcopy(annotation.geometry),
            )
            for annotation in cached_annotations
        }
        previous = app_module.dependency_overrides.get(get_db, _MISSING)

        async def request_session():
            yield request_db

        app_module.dependency_overrides[get_db] = request_session
        request_task = None
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_module), base_url="http://test"
            ) as client:
                request_task = asyncio.create_task(
                    client.post(
                        path,
                        json=body,
                        headers={"Authorization": f"Bearer {world['token']}"},
                    )
                )
                await asyncio.wait_for(entered.wait(), timeout=10)
                async with maker() as writer:
                    await change(writer)
                    await asyncio.wait_for(writer.commit(), timeout=10)
                release.set()
                return await asyncio.wait_for(request_task, timeout=20)
        finally:
            release.set()
            if request_task is not None and not request_task.done():
                request_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await request_task
            if previous is _MISSING:
                app_module.dependency_overrides.pop(get_db, None)
            else:
                app_module.dependency_overrides[get_db] = previous


async def _move_task_to_review(writer, *, task_id, actor_id) -> None:
    await writer.execute(
        update(Task)
        .where(Task.id == task_id)
        .values(
            status="review",
            review_round_id=uuid.uuid4(),
            annotation_contributor_ids=[str(actor_id)],
            review_contributor_ids=[str(actor_id)],
            review_submitter_id=actor_id,
        )
        .execution_options(synchronize_session=False)
    )


@pytest.mark.parametrize(
    ("operation", "changed_task"),
    [
        (operation, changed_task)
        for operation in _OPERATIONS
        for changed_task in ("source", "target")
    ],
)
async def test_cross_frame_manual_write_rechecks_source_and_target_status_after_preflight(
    test_engine, app_module, monkeypatch, operation, changed_task
):
    world = await _seed_world(test_engine, operation=operation, actor_role="employee")
    source_task_id, target_task_id = world["task_ids"][0], world["task_ids"][2]
    changed_id = source_task_id if changed_task == "source" else target_task_id
    path, body = _request_path_and_body(operation, world)

    async def submit_task(writer):
        await _move_task_to_review(
            writer, task_id=changed_id, actor_id=world["actor_id"]
        )

    try:
        response = await _post_while_writer_changes_state(
            test_engine=test_engine,
            app_module=app_module,
            world=world,
            path=path,
            body=body,
            change=submit_task,
            gate_setup=lambda entered, release: _pause_after_target_preflight(
                monkeypatch, target_task_id, entered, release
            ),
        )
        assert response.status_code == 409, response.text
        assert response.json()["detail"] == {
            "reason": "task_locked",
            "status": "review",
        }
        async with async_sessionmaker(test_engine, expire_on_commit=False)() as verify:
            source = await verify.get(Annotation, world["source_annotation_id"])
            assert source is not None
            assert source.version == 1
            target_annotations = (
                await verify.scalars(
                    select(Annotation.id).where(Annotation.task_id == target_task_id)
                )
            ).all()
            assert len(target_annotations) == world["target_initial_annotation_count"]
            current = await verify.get(Task, changed_id)
            assert current is not None and current.status == "review"
    finally:
        await _cleanup_world(test_engine, world)


@pytest.mark.asyncio
async def test_cross_frame_manual_write_rechecks_target_assignment_after_preflight(
    test_engine, app_module, monkeypatch
):
    world = await _seed_world(test_engine, operation="propagate", actor_role="employee")
    target_task_id = world["task_ids"][2]
    path, body = _request_path_and_body("propagate", world)

    async def reassign_target(writer):
        await writer.execute(
            update(Task)
            .where(Task.id == target_task_id)
            .values(assignee_id=world["other_id"])
            .execution_options(synchronize_session=False)
        )

    try:
        response = await _post_while_writer_changes_state(
            test_engine=test_engine,
            app_module=app_module,
            world=world,
            path=path,
            body=body,
            change=reassign_target,
            gate_setup=lambda entered, release: _pause_after_target_preflight(
                monkeypatch, target_task_id, entered, release
            ),
        )
        assert response.status_code == 404, response.text
        async with async_sessionmaker(test_engine, expire_on_commit=False)() as verify:
            assert (
                await verify.scalar(
                    select(Annotation.id).where(Annotation.task_id == target_task_id)
                )
                is None
            )
            source = await verify.get(Annotation, world["source_annotation_id"])
            assert (
                source is not None and source.track_id is None and source.version == 1
            )
            target = await verify.get(Task, target_task_id)
            assert target is not None and target.assignee_id == world["other_id"]
    finally:
        await _cleanup_world(test_engine, world)


@pytest.mark.parametrize("changed_task", ["source", "target"])
async def test_cross_frame_manual_write_rejects_other_editor_lock_acquired_after_preflight(
    test_engine, app_module, monkeypatch, changed_task
):
    world = await _seed_world(test_engine, operation="propagate")
    source_task_id, target_task_id = world["task_ids"][0], world["task_ids"][2]
    locked_task_id = source_task_id if changed_task == "source" else target_task_id
    path, body = _request_path_and_body("propagate", world)

    async def acquire_other_editor_lock(writer):
        lock = await TaskLockService(writer).acquire(locked_task_id, world["other_id"])
        assert lock is not None

    try:
        response = await _post_while_writer_changes_state(
            test_engine=test_engine,
            app_module=app_module,
            world=world,
            path=path,
            body=body,
            change=acquire_other_editor_lock,
            gate_setup=lambda entered, release: _pause_after_target_preflight(
                monkeypatch, target_task_id, entered, release
            ),
        )
        assert response.status_code == 409, response.text
        assert response.json()["detail"] == {"reason": "task_locked_for_editing"}
        async with async_sessionmaker(test_engine, expire_on_commit=False)() as verify:
            source = await verify.get(Annotation, world["source_annotation_id"])
            assert (
                source is not None and source.track_id is None and source.version == 1
            )
            assert (
                await verify.scalar(
                    select(Annotation.id).where(Annotation.task_id == target_task_id)
                )
                is None
            )
            lock = await verify.scalar(
                select(TaskLock).where(TaskLock.task_id == locked_task_id)
            )
            assert lock is not None and lock.user_id == world["other_id"]
    finally:
        await _cleanup_world(test_engine, world)


@pytest.mark.asyncio
async def test_cross_frame_manual_write_rechecks_target_review_evidence_after_preflight(
    test_engine, app_module, monkeypatch
):
    world = await _seed_world(test_engine, operation="propagate")
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    source_task_id, target_task_id = world["task_ids"][0], world["task_ids"][2]
    async with maker() as setup:
        source_task = await setup.get(Task, source_task_id)
        source_task.status = "review"
        source_task.assignee_id = world["other_id"]
        source_task.annotation_contributor_ids = [str(world["other_id"])]
        source_task.review_round_id = uuid.uuid4()
        source_task.review_contributor_ids = [str(world["other_id"])]
        source_task.review_submitter_id = world["other_id"]
        source_annotation = await setup.get(Annotation, world["source_annotation_id"])
        source_annotation.user_id = world["other_id"]
        await setup.commit()

    path, body = _request_path_and_body("propagate", world)

    async def submit_target_as_request_actor(writer):
        await writer.execute(
            update(Task)
            .where(Task.id == target_task_id)
            .values(
                status="review",
                assignee_id=world["actor_id"],
                review_round_id=uuid.uuid4(),
                annotation_contributor_ids=[str(world["actor_id"])],
                review_contributor_ids=[str(world["actor_id"])],
                review_submitter_id=world["actor_id"],
            )
            .execution_options(synchronize_session=False)
        )

    try:
        response = await _post_while_writer_changes_state(
            test_engine=test_engine,
            app_module=app_module,
            world=world,
            path=path,
            body=body,
            change=submit_target_as_request_actor,
            gate_setup=lambda entered, release: _pause_after_target_preflight(
                monkeypatch, target_task_id, entered, release
            ),
        )
        assert response.status_code == 403, response.text
        assert response.json()["detail"]["reason"] == "self_review_denied"
        async with maker() as verify:
            assert (
                await verify.scalar(
                    select(Annotation.id).where(Annotation.task_id == target_task_id)
                )
                is None
            )
            source = await verify.get(Annotation, world["source_annotation_id"])
            assert source is not None and source.version == 1
    finally:
        await _cleanup_world(test_engine, world)


@pytest.mark.parametrize("operation", ["propagate", "propagate_batch"])
async def test_manual_propagation_refreshes_cached_source_annotation_after_task_locks(
    test_engine, app_module, monkeypatch, operation
):
    world = await _seed_world(test_engine, operation=operation)
    target_task_id = world["task_ids"][2]
    path, body = _request_path_and_body(operation, world)

    def install_gate(reached, resume):
        _pause_after_target_preflight(monkeypatch, target_task_id, reached, resume)

    latest_geometry = _box3d(center=(42.0, 3.0, 4.0))

    async def update_source_annotation(writer):
        source = await writer.get(Annotation, world["source_annotation_id"])
        source_task = await writer.get(Task, world["task_ids"][0])
        source.geometry = latest_geometry
        source.track_id = "trk_latest"
        source.version = 7
        source.annotation_type = "box_3d"
        source.tool_unit_id = "lidar_box_3d"
        await bind_annotation_to_scene_track(
            writer,
            annotation=source,
            task=source_task,
            temporal_role="keyframe",
            interval_source="manual",
            actor_id=world["actor_id"],
        )

    try:
        response = await _post_while_writer_changes_state(
            test_engine=test_engine,
            app_module=app_module,
            world=world,
            path=path,
            body=body,
            change=update_source_annotation,
            gate_setup=install_gate,
            preload_annotation_ids=(world["source_annotation_id"],),
        )
        assert response.status_code == 201, response.text
        if operation == "propagate":
            propagated = response.json()["annotation"]
        else:
            propagated = response.json()["items"][0]["annotation"]
        assert propagated["track_id"] == "trk_latest"
        assert propagated["geometry"]["type"] == "box_3d"
        assert propagated["geometry"]["center"] == [42.0, 3.0, 4.0]
        async with async_sessionmaker(test_engine, expire_on_commit=False)() as verify:
            source = await verify.get(Annotation, world["source_annotation_id"])
            assert source is not None
            assert source.track_id == "trk_latest"
            assert source.version == 7
            assert source.geometry == latest_geometry
            bound_track = await verify.get(SceneTrack, source.scene_track_id)
            assert bound_track is not None and bound_track.track_id == "trk_latest"
    finally:
        await _cleanup_world(test_engine, world)


@pytest.mark.asyncio
async def test_interpolation_refreshes_cached_endpoint_annotations_after_task_locks(
    test_engine, app_module, monkeypatch
):
    world = await _seed_world(
        test_engine, operation="interpolate_range", actor_role="employee"
    )
    target_task_id = world["task_ids"][2]
    path, body = _request_path_and_body("interpolate_range", world)
    latest_geometry = _box3d(center=(6.0, 2.0, 3.0))

    async def update_source_annotation(writer):
        await writer.execute(
            update(Annotation)
            .where(Annotation.id == world["source_annotation_id"])
            .values(version=7, geometry=latest_geometry)
            .execution_options(synchronize_session=False)
        )

    try:
        response = await _post_while_writer_changes_state(
            test_engine=test_engine,
            app_module=app_module,
            world=world,
            path=path,
            body=body,
            change=update_source_annotation,
            gate_setup=lambda entered, release: _pause_after_target_preflight(
                monkeypatch, target_task_id, entered, release
            ),
            preload_annotation_ids=(world["source_annotation_id"],),
        )
        assert response.status_code == 201, response.text
        assert response.json()["annotations"][0]["geometry"]["center"] == [
            8.0,
            2.0,
            3.0,
        ]
        async with async_sessionmaker(test_engine, expire_on_commit=False)() as verify:
            source = await verify.get(Annotation, world["source_annotation_id"])
            assert source is not None
            assert source.geometry == latest_geometry
            assert source.version == 8
    finally:
        await _cleanup_world(test_engine, world)


@pytest.mark.parametrize("operation", ["propagate", "propagate_batch"])
async def test_manual_propagation_rejects_source_soft_deleted_after_preflight(
    test_engine, app_module, monkeypatch, operation
):
    world = await _seed_world(test_engine, operation=operation)
    target_task_id = world["task_ids"][2]
    path, body = _request_path_and_body(operation, world)

    async def soft_delete_source(writer):
        await writer.execute(
            update(Annotation)
            .where(Annotation.id == world["source_annotation_id"])
            .values(is_active=False, version=9)
            .execution_options(synchronize_session=False)
        )

    try:
        response = await _post_while_writer_changes_state(
            test_engine=test_engine,
            app_module=app_module,
            world=world,
            path=path,
            body=body,
            change=soft_delete_source,
            gate_setup=lambda reached, resume: _pause_after_target_preflight(
                monkeypatch, target_task_id, reached, resume
            ),
            preload_annotation_ids=(world["source_annotation_id"],),
        )
        assert response.status_code == 404, response.text
        async with async_sessionmaker(test_engine, expire_on_commit=False)() as verify:
            source = await verify.get(Annotation, world["source_annotation_id"])
            assert source is not None and not source.is_active and source.version == 9
            assert (
                await verify.scalar(
                    select(Annotation.id).where(Annotation.task_id == target_task_id)
                )
                is None
            )
    finally:
        await _cleanup_world(test_engine, world)


@pytest.mark.parametrize("already_present", [False, True])
async def test_interpolation_rechecks_only_non_skipped_mid_tasks_after_preflight(
    test_engine, app_module, monkeypatch, already_present
):
    world = await _seed_world(
        test_engine, operation="interpolate_range", actor_role="employee"
    )
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    mid_task_id = world["task_ids"][1]
    if already_present:
        async with maker() as setup:
            mid_task = await setup.get(Task, mid_task_id)
            project = await setup.get(Project, world["project_id"])
            await _add_annotation(
                setup,
                task=mid_task,
                project=project,
                user_id=world["actor_id"],
                geometry=_box3d(center=(5.0, 2.0, 3.0)),
                track_id=world["track_id"],
            )
            await setup.commit()

    path, body = _request_path_and_body("interpolate_range", world)
    from app.services import scene_pose

    original = scene_pose.get_trajectory

    def install_trajectory_gate(reached, resume):
        async def paused(db, scene_id):
            reached.set()
            await asyncio.wait_for(resume.wait(), timeout=10)
            return await original(db, scene_id)

        monkeypatch.setattr(scene_pose, "get_trajectory", paused)

    async def submit_mid_task(writer):
        await _move_task_to_review(
            writer, task_id=mid_task_id, actor_id=world["actor_id"]
        )

    try:
        response = await _post_while_writer_changes_state(
            test_engine=test_engine,
            app_module=app_module,
            world=world,
            path=path,
            body=body,
            change=submit_mid_task,
            gate_setup=install_trajectory_gate,
            preload_annotation_ids=(world["source_annotation_id"],),
        )
        if already_present:
            assert response.status_code == 201, response.text
            assert response.json()["skipped_frames"] == [1]
        else:
            assert response.status_code == 409, response.text
            assert response.json()["detail"] == {
                "reason": "task_locked",
                "status": "review",
            }
        async with maker() as verify:
            current_mid = await verify.get(Task, mid_task_id)
            assert current_mid is not None and current_mid.status == "review"
            annotations = list(
                (
                    await verify.execute(
                        select(Annotation).where(Annotation.task_id == mid_task_id)
                    )
                ).scalars()
            )
            assert len(annotations) == (1 if already_present else 0)
            if already_present:
                assert annotations[0].track_id == world["track_id"]
    finally:
        await _cleanup_world(test_engine, world)
