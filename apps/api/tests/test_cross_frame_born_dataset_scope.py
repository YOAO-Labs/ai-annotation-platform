"""Born cross-frame Job Dataset scope from authentic Scene/Task/source facts."""

from __future__ import annotations

import inspect
import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.db.models.annotation import Annotation
from app.db.models.async_job import AsyncJob, AsyncJobStatus
from app.db.models.dataset import Dataset, DatasetItem, Scene
from app.db.models.task import Task
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.services import fixture_run_ownership as ownership
from app.services.cross_frame_job import summarize_items
from app.workers.cross_frame_job import execute_cross_frame_job
from tests.factory import create_project

pytestmark = pytest.mark.asyncio

_FUNCTIONS = (
    "_cross_frame_scope_uuid(jsonb)",
    "_cross_frame_scope_int(jsonb)",
    "_cross_frame_dataset_scope(uuid,uuid,jsonb)",
    "cross_frame_born_dataset_scope(uuid,uuid,uuid,jsonb)",
    "fence_cross_frame_job_dataset_scope(uuid,uuid,uuid,jsonb)",
)


def _install_jobs_queue(monkeypatch):
    from app.services import jobs_dispatch, jobs_queue

    requests = []

    async def prepare_dispatch(_db, *, execution_id, **_request):
        return execution_id

    async def publish_prepared_dispatch(_db, _lease, *, publish):
        result = publish()
        if inspect.isawaitable(result):
            return await result
        return result

    async def dispatch_job(**request):
        requests.append(request)
        return request["execution_id"]

    monkeypatch.setattr(jobs_dispatch, "prepare_job_dispatch", prepare_dispatch)
    monkeypatch.setattr(
        jobs_dispatch, "publish_prepared_dispatch", publish_prepared_dispatch
    )
    monkeypatch.setattr(jobs_queue, "dispatch_job", dispatch_job)
    return requests


def _box3d() -> dict:
    return {
        "type": "box_3d",
        "center": [1.0, 2.0, 3.0],
        "size": [4.0, 5.0, 6.0],
        "rotation": [0.0, 0.0, 0.0],
        "convention_at_create": "iso_8855",
    }


def _request(annotation_id, *, start: int, end: int) -> dict:
    return {
        "operation": "propagate",
        "scope": "selected",
        "annotation_ids": [str(annotation_id)],
        "direction": "forward",
        "start_frame": start,
        "end_frame": end,
        "conflict_policy": "skip_existing",
    }


def _copy(payload: dict) -> dict:
    return json.loads(json.dumps(payload))


async def _dataset(db, owner_id, suffix: str) -> Dataset:
    dataset = Dataset(
        display_id=f"DS-CF-{uuid.uuid4().hex[:12]}",
        name=f"cross-frame-{suffix}-{uuid.uuid4().hex[:6]}",
        data_type="point_cloud",
        created_by=owner_id,
    )
    db.add(dataset)
    await db.flush()
    return dataset


async def _scene(db, dataset: Dataset, suffix: str) -> Scene:
    scene = Scene(
        display_id=f"SCN-CF-{uuid.uuid4().hex[:12]}",
        dataset_id=dataset.id,
        name=f"scene-{suffix}-{uuid.uuid4().hex[:6]}",
    )
    db.add(scene)
    await db.flush()
    return scene


async def _item(
    db,
    dataset: Dataset,
    scene: Scene,
    frame_index: int,
    *,
    file_type: str = "point_cloud",
) -> DatasetItem:
    item = DatasetItem(
        dataset_id=dataset.id,
        file_name=f"{frame_index:06d}-{uuid.uuid4().hex[:4]}.pcd",
        file_path=f"lidar/{frame_index:06d}-{uuid.uuid4().hex[:6]}.pcd",
        file_type=file_type,
        scene_id=scene.id,
        frame_index=frame_index,
    )
    db.add(item)
    await db.flush()
    return item


async def _task(db, project, item: DatasetItem | None, *, status="in_progress") -> Task:
    task = Task(
        project_id=project.id,
        dataset_item_id=item.id if item is not None else None,
        display_id=f"T-CF-{uuid.uuid4().hex[:8]}",
        file_name=(
            item.file_name if item is not None else f"task-{uuid.uuid4().hex}.pcd"
        ),
        file_path=(
            item.file_path if item is not None else f"task/{uuid.uuid4().hex}.pcd"
        ),
        file_type="point_cloud",
        status=status,
    )
    db.add(task)
    await db.flush()
    return task


async def _add_box(db, task: Task, project, user_id, *, track_id=None) -> Annotation:
    row = Annotation(
        task_id=task.id,
        project_id=project.id,
        user_id=user_id,
        source="manual",
        annotation_type="box_3d",
        tool_unit_id="lidar_box_3d",
        class_name="car",
        geometry=_box3d(),
        track_id=track_id,
    )
    db.add(row)
    await db.flush()
    return row


async def _link(db, task: Task, item: DatasetItem, role: str) -> TaskDatasetItemLink:
    row = TaskDatasetItemLink(
        task_id=task.id,
        dataset_item_id=item.id,
        role=role,
    )
    db.add(row)
    await db.flush()
    return row


async def _world(db, *, owner_id, label: str) -> SimpleNamespace:
    project = await create_project(
        db,
        owner_id=owner_id,
        name=f"cross-frame-{label}-{uuid.uuid4().hex[:6]}",
        type_key="lidar",
    )
    project.data_type = "lidar"
    dataset = await _dataset(db, owner_id, label)
    scene = await _scene(db, dataset, label)
    source_item = await _item(db, dataset, scene, 0)
    source_task = await _task(db, project, source_item)
    target_item = await _item(db, dataset, scene, 1)
    target_task = await _task(db, project, target_item)
    source = await _add_box(db, source_task, project, owner_id)
    return SimpleNamespace(
        project=project,
        dataset=dataset,
        scene=scene,
        source_item=source_item,
        source_task=source_task,
        target_item=target_item,
        target_task=target_task,
        source_annotation=source,
    )


async def _produce(httpx_client, token, db, world, *, start=1, end=1):
    response = await httpx_client.post(
        f"/api/v1/tasks/{world.source_task.id}/cross-frame-jobs",
        json=_request(world.source_annotation.id, start=start, end=end),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 202, response.text
    job_id = uuid.UUID(response.json()["id"])
    job = await db.get(AsyncJob, job_id, populate_existing=True)
    assert job is not None
    return job_id, job


async def _classifier(db, job_id: uuid.UUID):
    return await db.scalar(
        text(
            "SELECT yoao_jobs_v1.fixture_job_dataset_scope("
            "id,kind,payload,project_id,user_id) "
            "FROM public.async_jobs WHERE id=:id"
        ),
        {"id": job_id},
    )


def _scope_ids(value) -> tuple[uuid.UUID, ...] | None:
    assert value is not None, "classifier returned no row"
    kind = value.get("kind")
    if kind == "unknown":
        return None
    if kind == "unrelated":
        return ()
    if kind == "datasets":
        return tuple(sorted(uuid.UUID(str(item)) for item in value["dataset_ids"]))
    if kind == "dataset":
        return (uuid.UUID(str(value["dataset_id"])),)
    raise AssertionError(value)


async def _birth_scope(db, *, project_id, actor_id, payload, job_id=None):
    value = await db.scalar(
        text(
            "SELECT yoao_assets_v1.cross_frame_born_dataset_scope("
            ":job,:project,:actor,CAST(:payload AS jsonb))"
        ),
        {
            "job": job_id or uuid.uuid4(),
            "project": project_id,
            "actor": actor_id,
            "payload": json.dumps(payload, ensure_ascii=False),
        },
    )
    return tuple(sorted(value)) if value is not None else None


async def _fence(db, job_id, project_id, actor_id, payload) -> bool:
    return bool(
        await db.scalar(
            text(
                "SELECT yoao_assets_v1.fence_cross_frame_job_dataset_scope("
                ":job,:project,:actor,CAST(:payload AS jsonb))"
            ),
            {
                "job": job_id,
                "project": project_id,
                "actor": actor_id,
                "payload": json.dumps(payload, ensure_ascii=False),
            },
        )
    )


async def _annotation_count(db, task_id) -> int:
    return int(
        await db.scalar(
            select(func.count(Annotation.id))
            .where(Annotation.task_id == task_id)
            .where(Annotation.is_active.is_(True))
        )
        or 0
    )


async def test_real_producer_birth_binds_exact_dataset_scope(
    db_session, httpx_client, super_admin, monkeypatch
):
    user, token = super_admin
    world = await _world(db_session, owner_id=user.id, label="exact")
    _install_jobs_queue(monkeypatch)

    job_id, job = await _produce(httpx_client, token, db_session, world)

    assert _scope_ids(await _classifier(db_session, job_id)) == (world.dataset.id,)
    assert await _birth_scope(
        db_session,
        project_id=world.project.id,
        actor_id=user.id,
        payload=job.payload,
    ) == (world.dataset.id,)
    assert (
        await _fence(db_session, job_id, world.project.id, user.id, job.payload) is True
    )
    assert await _fence(db_session, job_id, uuid.uuid4(), user.id, job.payload) is False
    assert (
        await _fence(db_session, job_id, world.project.id, uuid.uuid4(), job.payload)
        is False
    )
    assert not await _fence(
        db_session,
        job_id,
        world.project.id,
        user.id,
        {**job.payload, "source_frame": 9},
    )
    with pytest.raises(HTTPException) as blocked:
        async with db_session.begin_nested():
            await ownership.guard_dataset_jobs(db_session, [world.dataset.id])
    assert blocked.value.detail["code"] == "fixture_dataset_job_unsettled"
    await db_session.delete(job)
    await db_session.flush()
    with pytest.raises(HTTPException) as orphan:
        async with db_session.begin_nested():
            await ownership.guard_dataset_jobs(db_session, [world.dataset.id])
    assert orphan.value.detail["code"] == "fixture_dataset_job_unsettled"
    await ownership.guard_dataset_jobs(db_session, [uuid.uuid4()])


async def test_primary_lidar_and_related_links_stay_in_scope(
    db_session, httpx_client, super_admin, monkeypatch
):
    user, token = super_admin
    project = await create_project(
        db_session,
        owner_id=user.id,
        name=f"cross-frame-multichannel-{uuid.uuid4().hex[:6]}",
        type_key="lidar",
    )
    project.data_type = "lidar"
    scene_dataset = await _dataset(db_session, user.id, "scene")
    scene = await _scene(db_session, scene_dataset, "multichannel")
    linked_dataset = await _dataset(db_session, user.id, "lidar")
    camera_dataset = await _dataset(db_session, user.id, "camera")

    linked_source_item = await _item(db_session, linked_dataset, scene, 0)
    source_task = await _task(db_session, project, None)
    await _link(db_session, source_task, linked_source_item, "primary_lidar")
    source = await _add_box(db_session, source_task, project, user.id)

    target_item = await _item(db_session, linked_dataset, scene, 1)
    target_task = await _task(db_session, project, target_item)
    camera_item = await _item(db_session, camera_dataset, scene, 1, file_type="image")
    await _link(db_session, target_task, camera_item, "camera_front")

    _install_jobs_queue(monkeypatch)
    response = await httpx_client.post(
        f"/api/v1/tasks/{source_task.id}/cross-frame-jobs",
        json=_request(source.id, start=1, end=1),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 202, response.text
    job_id = uuid.UUID(response.json()["id"])
    job = await db_session.get(AsyncJob, job_id, populate_existing=True)
    assert job is not None

    expected = tuple(sorted({scene_dataset.id, linked_dataset.id, camera_dataset.id}))
    assert _scope_ids(await _classifier(db_session, job_id)) == expected
    assert (
        await _birth_scope(
            db_session,
            project_id=project.id,
            actor_id=user.id,
            payload=job.payload,
        )
        == expected
    )

    mismatched = _copy(job.payload)
    mismatched["targets"][0]["frame_index"] = 9
    assert (
        await _birth_scope(
            db_session,
            project_id=project.id,
            actor_id=user.id,
            payload=mismatched,
        )
        is None
    )

    # Removing the only canonical source relation leaves the shape unproven.
    await db_session.execute(
        text(
            "DELETE FROM public.task_dataset_item_links "
            "WHERE task_id=:task AND role='primary_lidar'"
        ),
        {"task": source_task.id},
    )
    await db_session.flush()
    current = _copy(job.payload)
    assert (
        await _birth_scope(
            db_session,
            project_id=project.id,
            actor_id=user.id,
            payload=current,
        )
        is None
    )


async def test_payload_fields_cannot_add_or_remove_dataset_authority(
    db_session, httpx_client, super_admin, monkeypatch
):
    user, token = super_admin
    world = await _world(db_session, owner_id=user.id, label="authority")
    other_dataset = await _dataset(db_session, user.id, "other")
    _install_jobs_queue(monkeypatch)
    job_id, job = await _produce(httpx_client, token, db_session, world, end=2)

    scoped = await _birth_scope(
        db_session,
        project_id=world.project.id,
        actor_id=user.id,
        payload=job.payload,
    )
    assert scoped == (world.dataset.id,)

    added = _copy(job.payload)
    added["dataset_id"] = str(other_dataset.id)
    added["dataset_ids"] = [str(other_dataset.id)]
    reported = await _birth_scope(
        db_session,
        project_id=world.project.id,
        actor_id=user.id,
        payload=added,
    )
    assert reported == (world.dataset.id,)
    assert other_dataset.id not in reported

    for field, value in (
        ("contract_version", 2),
        ("operation", "interpolate_range"),
        ("scope", "everything"),
        ("direction", "sideways"),
        ("conflict_policy", "overwrite"),
    ):
        mutated = _copy(job.payload)
        mutated[field] = value
        assert (
            await _birth_scope(
                db_session,
                project_id=world.project.id,
                actor_id=user.id,
                payload=mutated,
            )
            is None
        ), field

    no_sources = _copy(job.payload)
    no_sources["sources"] = []
    assert (
        await _birth_scope(
            db_session,
            project_id=world.project.id,
            actor_id=user.id,
            payload=no_sources,
        )
        is None
    )

    bad_total = _copy(job.payload)
    bad_total["total_frames"] = len(bad_total["targets"]) + 1
    assert (
        await _birth_scope(
            db_session,
            project_id=world.project.id,
            actor_id=user.id,
            payload=bad_total,
        )
        is None
    )

    duplicate = _copy(job.payload)
    duplicate["targets"][1] = _copy(duplicate["targets"][0])
    duplicate["total_frames"] = len(duplicate["targets"])
    assert (
        await _birth_scope(
            db_session,
            project_id=world.project.id,
            actor_id=user.id,
            payload=duplicate,
        )
        is None
    )

    unproven_task = _copy(job.payload)
    unproven_task["targets"][0]["task_id"] = str(uuid.uuid4())
    assert (
        await _birth_scope(
            db_session,
            project_id=world.project.id,
            actor_id=user.id,
            payload=unproven_task,
        )
        is None
    )

    stranger_parent = _copy(job.payload)
    stranger_parent["parent_job_id"] = str(uuid.uuid4())
    assert (
        await _birth_scope(
            db_session,
            project_id=world.project.id,
            actor_id=user.id,
            payload=stranger_parent,
        )
        is None
    )

    # An existing Job is never re-adopted under another identity or kind.
    assert (
        await _birth_scope(
            db_session,
            project_id=uuid.uuid4(),
            actor_id=user.id,
            payload=job.payload,
            job_id=job_id,
        )
        is None
    )
    assert (
        await _birth_scope(
            db_session,
            project_id=world.project.id,
            actor_id=uuid.uuid4(),
            payload=job.payload,
            job_id=job_id,
        )
        is None
    )


async def test_inconsistent_mapping_never_becomes_unrelated(
    db_session, httpx_client, super_admin, monkeypatch
):
    user, token = super_admin
    world = await _world(db_session, owner_id=user.id, label="inconsistent")
    _install_jobs_queue(monkeypatch)
    _, job = await _produce(httpx_client, token, db_session, world)

    malformed = _copy(job.payload)
    malformed["targets"][0]["frame_index"] = 8
    assert (
        await _birth_scope(
            db_session,
            project_id=world.project.id,
            actor_id=user.id,
            payload=malformed,
        )
        is None
    )

    unproven_job = uuid.uuid4()
    rejected = False
    try:
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "INSERT INTO public.async_jobs "
                    "(id,kind,project_id,user_id,status,progress_pct,payload,result) "
                    "VALUES (:id,'point_cloud_cross_frame',:project,:actor,'pending',0,"
                    "CAST(:payload AS jsonb),CAST('{}' AS jsonb))"
                ),
                {
                    "id": unproven_job,
                    "project": world.project.id,
                    "actor": user.id,
                    "payload": json.dumps(malformed),
                },
            )
    except DBAPIError:
        # The strict birth admission may refuse the unproven Job outright.
        rejected = True
    if not rejected:
        assert _scope_ids(await _classifier(db_session, unproven_job)) is None
        with pytest.raises(HTTPException) as blocked:
            await ownership.guard_dataset_jobs(db_session, [world.dataset.id])
        assert blocked.value.detail["code"] == "fixture_dataset_job_unsettled"


async def test_retry_births_its_own_narrower_scope(
    db_session, httpx_client, super_admin, monkeypatch
):
    user, token = super_admin
    project = await create_project(
        db_session,
        owner_id=user.id,
        name=f"cross-frame-retry-{uuid.uuid4().hex[:6]}",
        type_key="lidar",
    )
    project.data_type = "lidar"
    scene_dataset = await _dataset(db_session, user.id, "retry-scene")
    scene = await _scene(db_session, scene_dataset, "retry")
    source_item = await _item(db_session, scene_dataset, scene, 0)
    source_task = await _task(db_session, project, source_item)
    source = await _add_box(db_session, source_task, project, user.id)
    wider_dataset = await _dataset(db_session, user.id, "retry-wider")
    first_item = await _item(db_session, wider_dataset, scene, 1)
    first_task = await _task(db_session, project, first_item)
    second_item = await _item(db_session, scene_dataset, scene, 2)
    second_task = await _task(db_session, project, second_item)

    _install_jobs_queue(monkeypatch)
    response = await httpx_client.post(
        f"/api/v1/tasks/{source_task.id}/cross-frame-jobs",
        json=_request(source.id, start=1, end=2),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 202, response.text
    parent_id = uuid.UUID(response.json()["id"])
    parent = await db_session.get(AsyncJob, parent_id, populate_existing=True)
    assert parent is not None
    assert _scope_ids(await _classifier(db_session, parent_id)) == tuple(
        sorted({scene_dataset.id, wider_dataset.id})
    )

    result = summarize_items(
        [
            {
                "frame_index": 1,
                "task_id": str(first_task.id),
                "status": "success",
                "created_count": 1,
                "skipped_count": 0,
                "reason": None,
            },
            {
                "frame_index": 2,
                "task_id": str(second_task.id),
                "status": "failed",
                "created_count": 0,
                "skipped_count": 0,
                "reason": "propagate_failed",
            },
        ]
    )
    parent.status = AsyncJobStatus.COMPLETED.value
    parent.progress_pct = 100
    parent.result = result
    await db_session.flush()

    retry_response = await httpx_client.post(
        f"/api/v1/tasks/{source_task.id}/cross-frame-jobs/{parent_id}/retry-failed",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert retry_response.status_code == 202, retry_response.text
    retry_id = uuid.UUID(retry_response.json()["id"])
    retry = await db_session.get(AsyncJob, retry_id, populate_existing=True)
    assert retry is not None
    assert retry.payload["parent_job_id"] == str(parent_id)
    assert [target["frame_index"] for target in retry.payload["targets"]] == [2]
    assert _scope_ids(await _classifier(db_session, retry_id)) == (scene_dataset.id,)
    assert _scope_ids(await _classifier(db_session, parent_id)) == tuple(
        sorted({scene_dataset.id, wider_dataset.id})
    )

    non_terminal = _copy(retry.payload)
    non_terminal["parent_job_id"] = str(retry_id)
    assert (
        await _birth_scope(
            db_session,
            project_id=project.id,
            actor_id=user.id,
            payload=non_terminal,
        )
        is None
    )

    extra_source = await _add_box(db_session, source_task, project, user.id)
    wrong_sources = _copy(retry.payload)
    wrong_sources["sources"][0]["annotation_id"] = str(extra_source.id)
    assert (
        await _birth_scope(
            db_session,
            project_id=project.id,
            actor_id=user.id,
            payload=wrong_sources,
        )
        is None
    )


async def test_task_reassignment_fails_closed_without_writes(
    db_session, httpx_client, super_admin, monkeypatch
):
    user, token = super_admin
    world = await _world(db_session, owner_id=user.id, label="reassign")
    _install_jobs_queue(monkeypatch)
    job_id, job = await _produce(httpx_client, token, db_session, world)
    assert _scope_ids(await _classifier(db_session, job_id)) == (world.dataset.id,)

    other_dataset = await _dataset(db_session, user.id, "reassigned")
    other_item = await _item(db_session, other_dataset, world.scene, 1)
    world.target_task.dataset_item_id = other_item.id
    await db_session.flush()

    # The born receipt is immutable; the new Dataset is not silently in scope.
    assert _scope_ids(await _classifier(db_session, job_id)) == (world.dataset.id,)
    current = _copy(job.payload)
    assert await _birth_scope(
        db_session,
        project_id=world.project.id,
        actor_id=user.id,
        payload=current,
    ) == tuple(sorted({world.dataset.id, other_dataset.id}))
    assert not await _fence(db_session, job_id, world.project.id, user.id, current)
    await ownership.guard_dataset_jobs(db_session, [other_dataset.id])

    await execute_cross_frame_job(db_session, job_id=job_id)

    failed = await db_session.get(AsyncJob, job_id, populate_existing=True)
    assert failed is not None
    assert failed.status == AsyncJobStatus.FAILED.value
    assert "dataset scope changed" in (failed.error_message or "")
    assert {item["reason"] for item in failed.result["items"]} == {
        "dataset_scope_changed"
    }
    assert await _annotation_count(db_session, world.target_task.id) == 0


async def test_primary_lidar_repoint_fails_closed_without_writes(
    db_session, httpx_client, super_admin, monkeypatch
):
    user, token = super_admin
    world = await _world(db_session, owner_id=user.id, label="primary")
    _install_jobs_queue(monkeypatch)
    job_id, job = await _produce(httpx_client, token, db_session, world)
    assert _scope_ids(await _classifier(db_session, job_id)) == (world.dataset.id,)

    other_dataset = await _dataset(db_session, user.id, "primary-other")
    other_item = await _item(db_session, other_dataset, world.scene, 1)
    await _link(db_session, world.target_task, other_item, "primary_lidar")
    await db_session.flush()

    current = _copy(job.payload)
    assert await _birth_scope(
        db_session,
        project_id=world.project.id,
        actor_id=user.id,
        payload=current,
    ) == tuple(sorted({world.dataset.id, other_dataset.id}))
    assert not await _fence(db_session, job_id, world.project.id, user.id, current)

    await execute_cross_frame_job(db_session, job_id=job_id)
    failed = await db_session.get(AsyncJob, job_id, populate_existing=True)
    assert failed is not None
    assert failed.status == AsyncJobStatus.FAILED.value
    assert await _annotation_count(db_session, world.target_task.id) == 0


async def test_scene_remap_fails_closed_without_writes(
    db_session, httpx_client, super_admin, monkeypatch
):
    user, token = super_admin
    world = await _world(db_session, owner_id=user.id, label="scene")
    _install_jobs_queue(monkeypatch)
    job_id, job = await _produce(httpx_client, token, db_session, world)
    assert _scope_ids(await _classifier(db_session, job_id)) == (world.dataset.id,)

    other_dataset = await _dataset(db_session, user.id, "scene-remap")
    world.scene.dataset_id = other_dataset.id
    await db_session.flush()

    current = _copy(job.payload)
    assert await _birth_scope(
        db_session,
        project_id=world.project.id,
        actor_id=user.id,
        payload=current,
    ) == tuple(sorted({world.dataset.id, other_dataset.id}))
    assert not await _fence(db_session, job_id, world.project.id, user.id, current)

    await execute_cross_frame_job(db_session, job_id=job_id)
    failed = await db_session.get(AsyncJob, job_id, populate_existing=True)
    assert failed is not None
    assert failed.status == AsyncJobStatus.FAILED.value
    assert await _annotation_count(db_session, world.target_task.id) == 0


async def test_neighbor_born_job_keeps_execution_and_classification(
    db_session, httpx_client, super_admin, monkeypatch
):
    user, token = super_admin
    keeper = await _world(db_session, owner_id=user.id, label="keeper")
    widened = await _world(db_session, owner_id=user.id, label="widened")
    _install_jobs_queue(monkeypatch)
    keeper_id, _ = await _produce(httpx_client, token, db_session, keeper)
    widened_id, _ = await _produce(httpx_client, token, db_session, widened)
    assert _scope_ids(await _classifier(db_session, keeper_id)) == (keeper.dataset.id,)
    assert _scope_ids(await _classifier(db_session, widened_id)) == (
        widened.dataset.id,
    )

    extra_dataset = await _dataset(db_session, user.id, "neighbor-extra")
    extra_item = await _item(db_session, extra_dataset, widened.scene, 1)
    widened.target_task.dataset_item_id = extra_item.id
    await db_session.flush()

    # Only the widened Job's current facts moved; its receipt did not.
    assert _scope_ids(await _classifier(db_session, keeper_id)) == (keeper.dataset.id,)
    assert _scope_ids(await _classifier(db_session, widened_id)) == (
        widened.dataset.id,
    )
    # The executor refuses the widened Job before writing there, so cleanup of
    # that new Dataset remains safe rather than silently widening the receipt.
    await ownership.guard_dataset_jobs(db_session, [extra_dataset.id])

    await execute_cross_frame_job(db_session, job_id=widened_id)
    stopped = await db_session.get(AsyncJob, widened_id, populate_existing=True)
    assert stopped is not None
    assert stopped.status == AsyncJobStatus.FAILED.value
    assert "dataset scope changed" in (stopped.error_message or "")
    assert await _annotation_count(db_session, widened.target_task.id) == 0

    await execute_cross_frame_job(db_session, job_id=keeper_id)
    finished = await db_session.get(AsyncJob, keeper_id, populate_existing=True)
    assert finished is not None
    assert finished.status == AsyncJobStatus.COMPLETED.value
    assert await _annotation_count(db_session, keeper.target_task.id) == 1


async def test_family_scope_functions_stay_private(db_session):
    assets_owner = await db_session.scalar(
        text(
            "SELECT pg_catalog.pg_get_userbyid(proowner) FROM pg_catalog.pg_proc "
            "WHERE oid='yoao_assets_v1.fixture_task_dataset_scope(uuid,jsonb)'::regprocedure"
        )
    )
    assert assets_owner
    rows = (
        await db_session.execute(
            text(
                "SELECT p.oid::pg_catalog.regprocedure::text AS signature, "
                "p.prosecdef, pg_catalog.pg_get_userbyid(p.proowner) AS owner, "
                "EXISTS (SELECT 1 FROM pg_catalog.aclexplode("
                "        COALESCE(p.proacl, pg_catalog.acldefault('f', p.proowner))"
                "    ) AS acl WHERE acl.grantee = 0 AND acl.privilege_type = 'EXECUTE'"
                ") AS public_execute, "
                "p.proconfig "
                "FROM pg_catalog.pg_proc AS p "
                "JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace "
                "WHERE n.nspname = 'yoao_assets_v1' "
                "AND p.proname LIKE '%cross_frame%'"
            )
        )
    ).mappings()
    found = {row["signature"]: row for row in rows}
    expected = {f"yoao_assets_v1.{name}" for name in _FUNCTIONS}
    assert expected <= set(found)
    for signature in expected:
        row = found[signature]
        assert row["owner"] == assets_owner, signature
        if signature in {
            "yoao_assets_v1._cross_frame_dataset_scope(uuid,uuid,jsonb)",
            "yoao_assets_v1.cross_frame_born_dataset_scope(uuid,uuid,uuid,jsonb)",
            "yoao_assets_v1.fence_cross_frame_job_dataset_scope(uuid,uuid,uuid,jsonb)",
        }:
            assert row["prosecdef"] is True, signature
        assert row["public_execute"] is False, signature
        assert row["proconfig"] == ["search_path=pg_catalog, pg_temp"], signature
