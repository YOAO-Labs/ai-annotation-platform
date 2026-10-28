"""Bulk HTTP admission freezes membership with its Job before publishing."""

from unittest.mock import Mock
from types import SimpleNamespace
from uuid import UUID, uuid4
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from aap_assets_input_manifests import contract as c
from app.db.models.async_job import AsyncJob
from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset, Scene
from app.db.models.task import Task
from app.db.models.task_lock import TaskLock
from app.services.accepted_input_manifests import (
    SqlAlchemyManifestDatabase,
    get_input_manifest_capture,
)
from tests.test_batch_predict_idempotency import _seed
from tests.test_assets_catalog_native import _seed_dataset
from tests.test_assets_images_execution import _seed_image
from tests.test_managed_prediction_requests import managed_task
from tests.factory import create_batch, create_project, create_task

pytestmark = pytest.mark.asyncio


@pytest.fixture
def dispatch(monkeypatch):
    from app.workers.batch_prediction_dispatch import CeleryBatchPredictionDispatcher

    dispatched = Mock(side_effect=lambda request, **_kwargs: request.async_job_id)
    monkeypatch.setattr(CeleryBatchPredictionDispatcher, "dispatch", dispatched)
    return dispatched


async def _record(db, job, actor_id):
    return await SqlAlchemyManifestDatabase(db, commit=False).call(
        "read_accepted_image_input_manifest",
        c.ManifestScope(job_id=job.id, actor_id=actor_id, project_id=job.project_id),
    )


async def test_bulk_replay_keeps_captured_members_after_new_tasks_and_batch_changes(
    httpx_client, db_session, super_admin, dispatch
):
    actor, token = super_admin
    project, backend, batch, tasks = await _seed(db_session, actor.id)
    endpoint = f"/api/v1/projects/{project.id}/preannotate"
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid4())}
    body = {
        "ml_backend_id": str(backend.id),
        "batch_id": str(batch.id),
        "predict_mode": "append",
    }
    response = await httpx_client.post(endpoint, headers=headers, json=body)
    assert response.status_code == 200, response.text
    job = await db_session.get(AsyncJob, UUID(response.json()["job_id"]))
    header = await _record(db_session, job, actor.id)
    assert header.selected_count == response.json()["total_tasks"] == 3
    assert header.selection.batch_id == batch.id
    assert header.selection.task_ids is None
    original_digest = header.selection_digest
    assert job.payload["accepted_input_manifest_protocol"] == c.MANIFEST_SCHEMA
    assert job.payload["accepted_input_manifest_id"] == str(job.id)
    assert job.payload["accepted_input_manifest_digest"] == original_digest
    assert job.payload["total_tasks"] == job.payload["input_candidate_count"] == 3
    assert "task_ids" not in job.payload and "accepted_input_ids" not in job.payload
    assert job.payload["execution_task_ids"] == sorted(str(task.id) for task in tasks)
    message = dispatch.call_args.args[0]
    assert message.task_ids is None and message.accepted_input_ids is None
    assert message.accepted_input_manifest_digest == original_digest

    late_task = Task(
        project_id=project.id,
        batch_id=batch.id,
        display_id=f"T-{uuid4().hex[:12]}",
        file_name="late.png",
        file_path="images/late.png",
        file_type="image",
        status="pending",
    )
    db_session.add(late_task)
    batch.admin_locked = True
    tasks[0].status = "completed"
    await db_session.commit()

    replay = await httpx_client.post(endpoint, headers=headers, json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json()["job_id"] == str(job.id)
    assert replay.json()["total_tasks"] == 3
    replay_header = await _record(db_session, job, actor.id)
    assert replay_header.selection_digest == original_digest
    page = await SqlAlchemyManifestDatabase(db_session, commit=False).call(
        "read_accepted_image_input_manifest_page",
        c.ManifestPageRequest(
            job_id=job.id,
            actor_id=actor.id,
            project_id=project.id,
            selection_digest=original_digest,
        ),
    )
    assert {member.task_id for member in page.members} == {task.id for task in tasks}
    assert late_task.id not in {member.task_id for member in page.members}
    # A confirmed durable publication is not sent a second time on replay.
    assert dispatch.call_count == 1


async def test_explicit_selection_is_canonical_and_excludes_other_members(
    httpx_client, db_session, super_admin, dispatch
):
    actor, token = super_admin
    project, backend, _batch, tasks = await _seed(db_session, actor.id)
    requested = [str(tasks[2].id), str(tasks[0].id), str(tasks[0].id)]
    canonical = sorted(set(requested))
    response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/preannotate",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "ml_backend_id": str(backend.id),
            "task_ids": requested,
            "predict_mode": "append",
        },
    )
    assert response.status_code == 200, response.text
    job = await db_session.get(AsyncJob, UUID(response.json()["job_id"]))
    header = await _record(db_session, job, actor.id)
    assert [str(task) for task in header.selection.task_ids] == canonical
    assert header.selected_count == response.json()["total_tasks"] == 2
    assert job.payload["task_ids"] == job.payload["scope"]["task_ids"] == canonical
    assert job.payload["execution_task_ids"] == canonical
    assert dispatch.call_args.args[0].task_ids == canonical
    page = await SqlAlchemyManifestDatabase(db_session, commit=False).call(
        "read_accepted_image_input_manifest_page",
        c.ManifestPageRequest(
            job_id=job.id,
            actor_id=actor.id,
            project_id=project.id,
            selection_digest=header.selection_digest,
        ),
    )
    assert [str(member.task_id) for member in page.members] == canonical


@pytest.mark.parametrize("all_predicted,selected", [(False, 2), (True, 0)])
async def test_skip_predicted_counts_frozen_selected_members_not_candidates(
    httpx_client, db_session, super_admin, dispatch, all_predicted, selected
):
    actor, token = super_admin
    project, backend, _batch, tasks = await _seed(db_session, actor.id)
    for task in tasks if all_predicted else tasks[:1]:
        task.total_predictions = 1
    await db_session.commit()
    response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/preannotate",
        headers={"Authorization": f"Bearer {token}"},
        json={"ml_backend_id": str(backend.id)},
    )
    assert response.status_code == 200, response.text
    job = await db_session.get(AsyncJob, UUID(response.json()["job_id"]))
    header = await _record(db_session, job, actor.id)
    assert response.json()["total_tasks"] == header.selected_count == selected
    assert header.candidate_count == job.payload["input_candidate_count"] == 3
    assert job.payload["total_tasks"] == selected
    expected = sorted(str(task.id) for task in tasks if task.total_predictions == 0)
    assert job.payload["execution_task_ids"] == expected
    assert dispatch.call_args.args[0].task_ids is None
    assert header.selection.task_ids is None
    assert dispatch.call_args.args[0].accepted_input_manifest_id == str(job.id)


@pytest.mark.parametrize("all_predicted", [False, True])
async def test_explicit_skip_retains_selector_and_replays_frozen_execution(
    httpx_client, db_session, super_admin, dispatch, all_predicted
):
    actor, token = super_admin
    project, backend, _batch, tasks = await _seed(db_session, actor.id)
    tasks[0].total_predictions = 1
    if all_predicted:
        tasks[1].total_predictions = 1
    await db_session.commit()
    selector = sorted([str(tasks[0].id), str(tasks[1].id)])
    selected = [] if all_predicted else [str(tasks[1].id)]
    endpoint = f"/api/v1/projects/{project.id}/preannotate"
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid4())}
    body = {"ml_backend_id": str(backend.id), "task_ids": selector}

    response = await httpx_client.post(endpoint, headers=headers, json=body)
    assert response.status_code == 200, response.text
    job = await db_session.get(AsyncJob, UUID(response.json()["job_id"]))
    header = await _record(db_session, job, actor.id)
    assert [str(task_id) for task_id in header.selection.task_ids] == selector
    assert header.selected_count == len(selected)
    assert job.payload["scope"]["task_ids"] == job.payload["task_ids"] == selector
    assert job.payload["execution_task_ids"] == selected
    assert dispatch.call_args.args[0].task_ids == selector

    tasks[0].total_predictions = 0
    await db_session.commit()
    replay = await httpx_client.post(endpoint, headers=headers, json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json()["job_id"] == str(job.id)
    assert replay.json()["total_tasks"] == len(selected)
    assert (
        await _record(db_session, job, actor.id)
    ).selection_digest == header.selection_digest
    page = await SqlAlchemyManifestDatabase(db_session, commit=False).call(
        "read_accepted_image_input_manifest_page",
        c.ManifestPageRequest(
            job_id=job.id,
            actor_id=actor.id,
            project_id=project.id,
            selection_digest=header.selection_digest,
        ),
    )
    assert sorted(str(member.task_id) for member in page.members) == selected


async def test_unbounded_selector_retains_more_than_200_execution_members(
    httpx_client, db_session, super_admin, dispatch
):
    actor, token = super_admin
    project, backend, batch, tasks = await _seed(db_session, actor.id)
    for index in range(198):
        task = Task(
            id=uuid4(),
            project_id=project.id,
            batch_id=batch.id,
            display_id=f"T-{uuid4().hex[:20]}",
            file_name=f"extra-{index}.jpg",
            file_path=f"items/{uuid4().hex}.jpg",
            file_type="image",
            status="pending",
        )
        db_session.add(task)
        await db_session.flush()
        await managed_task(db_session, project, SimpleNamespace(id=actor.id), task=task)
        tasks.append(task)
    await db_session.commit()
    response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/preannotate",
        headers={"Authorization": f"Bearer {token}"},
        json={"ml_backend_id": str(backend.id), "batch_id": str(batch.id)},
    )
    assert response.status_code == 200, response.text
    job = await db_session.get(AsyncJob, UUID(response.json()["job_id"]))
    header = await _record(db_session, job, actor.id)
    assert header.selection.task_ids is None
    assert header.selected_count == response.json()["total_tasks"] == 201
    assert job.payload["execution_task_ids"] == sorted(str(task.id) for task in tasks)
    assert dispatch.call_args.args[0].task_ids is None


async def test_project_all_selection_uses_bulk_eligibility_before_frozen_counts(
    httpx_client, db_session, super_admin, dispatch
):
    actor, token = super_admin
    project, backend, _active, tasks = await _seed(db_session, actor.id)
    tasks[0].total_predictions = 1
    db_session.add(
        TaskLock(
            task_id=tasks[1].id,
            user_id=actor.id,
            expire_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
    )
    unassigned = await create_batch(db_session, project_id=project.id)
    draft_task = await create_task(db_session, project_id=project.id)
    draft_task.batch_id = unassigned.id
    standalone = await create_task(db_session, project_id=project.id)
    for kind, status in (
        ("assigned", "draft"),
        ("reviewer", "draft"),
        ("locked", "active"),
        ("review", "review"),
    ):
        excluded = await create_batch(db_session, project_id=project.id, status=status)
        if kind == "assigned":
            excluded.annotator_id = actor.id
        elif kind == "reviewer":
            excluded.reviewer_id = actor.id
        elif kind == "locked":
            excluded.admin_locked = True
        excluded_task = await create_task(db_session, project_id=project.id)
        excluded_task.batch_id = excluded.id
    foreign_project = await create_project(db_session, owner_id=actor.id)
    foreign_batch = await create_batch(
        db_session, project_id=foreign_project.id, status="active"
    )
    inconsistent_task = await create_task(db_session, project_id=project.id)
    inconsistent_task.batch_id = foreign_batch.id
    await db_session.commit()
    response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/preannotate",
        headers={"Authorization": f"Bearer {token}"},
        json={"ml_backend_id": str(backend.id)},
    )
    assert response.status_code == 200, response.text
    job = await db_session.get(AsyncJob, UUID(response.json()["job_id"]))
    header = await _record(db_session, job, actor.id)
    assert header.candidate_count == 4
    assert header.selected_count == response.json()["total_tasks"] == 3
    page = await SqlAlchemyManifestDatabase(db_session, commit=False).call(
        "read_accepted_image_input_manifest_page",
        c.ManifestPageRequest(
            job_id=job.id,
            actor_id=actor.id,
            project_id=project.id,
            selection_digest=header.selection_digest,
        ),
    )
    assert {member.task_id for member in page.members} == {
        tasks[2].id,
        draft_task.id,
        standalone.id,
    }
    assert job.payload["execution_task_ids"] == sorted(
        str(task_id) for task_id in (tasks[2].id, draft_task.id, standalone.id)
    )
    assert dispatch.call_count == 1


async def test_scene_mode_image_dataset_task_uses_retained_manifest(
    httpx_client, db_session, super_admin, dispatch
):
    actor, token = super_admin
    project, backend, _batch, tasks = await _seed(db_session, actor.id)
    project.scene_mode = True
    dataset_id = await _seed_dataset(
        db_session,
        actor_id=actor.id,
        name=f"scene-image-{uuid4().hex[:8]}",
        data_type="image",
        file_count=1,
    )
    dataset = await db_session.get(Dataset, dataset_id)
    dataset.is_temporal = True
    db_session.add(ProjectDataset(project_id=project.id, dataset_id=dataset_id))
    scene = Scene(
        display_id=f"S-{uuid4().hex[:12]}",
        dataset_id=dataset_id,
        name="sequence",
        created_by=actor.id,
    )
    db_session.add(scene)
    await db_session.flush()
    item_id = await _seed_image(
        db_session, actor_id=actor.id, dataset_id=dataset_id, width=16, height=8
    )
    item = await db_session.get(DatasetItem, item_id)
    item.scene_id = scene.id
    item.frame_index = 7
    task = tasks[0]
    task.dataset_item_id = item.id
    task.file_path = item.file_path
    task.file_name = item.file_name
    await db_session.commit()
    endpoint = f"/api/v1/projects/{project.id}/preannotate"
    response = await httpx_client.post(
        endpoint,
        headers={"Authorization": f"Bearer {token}"},
        json={
            "ml_backend_id": str(backend.id),
            "task_ids": [str(task.id)],
        },
    )
    assert response.status_code == 200, response.text
    job = await db_session.get(AsyncJob, UUID(response.json()["job_id"]))
    header = await _record(db_session, job, actor.id)
    assert header.selected_count == response.json()["total_tasks"] == 1
    assert header.actor_id == actor.id and header.project_id == project.id
    assert job.payload["accepted_input_manifest_protocol"] == c.MANIFEST_SCHEMA
    assert job.payload["accepted_input_manifest_id"] == str(job.id)
    message = dispatch.call_args.args[0]
    assert message.execution_scope == "bulk"
    assert message.task_ids == [str(task.id)]
    assert message.accepted_input_manifest_protocol == c.MANIFEST_SCHEMA
    assert message.accepted_input_manifest_id == str(job.id)
    assert message.accepted_input_manifest_digest == header.selection_digest
    page = await SqlAlchemyManifestDatabase(db_session, commit=False).call(
        "read_accepted_image_input_manifest_page",
        c.ManifestPageRequest(
            job_id=job.id,
            actor_id=actor.id,
            project_id=project.id,
            selection_digest=header.selection_digest,
        ),
    )
    assert len(page.members) == 1
    member = page.members[0]
    assert member.task_id == task.id and member.dataset_item_id == item.id
    assert member.source is not None
    assert member.source.owner.kind == "dataset_item"
    assert member.source.owner.id == item.id
    assert len(member.source.source_snapshot) == 64
    assert member.source.source_generation == item.media_source_generation
    assert member.source.upload_ready is True
    assert member.source.object_identity is not None
    assert member.source.object_identity.byte_size == 128


async def test_scene_execution_unit_remains_outside_image_manifest_admission(
    httpx_client, db_session, super_admin, dispatch, app_module, monkeypatch
):
    actor, token = super_admin
    project, backend, _batch, tasks = await _seed(db_session, actor.id)
    project.scene_mode = True
    await db_session.commit()

    def forbidden_capture(_db):
        pytest.fail("explicit Scene execution must not capture an image manifest")

    monkeypatch.setitem(
        app_module.dependency_overrides,
        get_input_manifest_capture,
        lambda: forbidden_capture,
    )
    response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/preannotate",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "task_ids": [str(tasks[0].id)],
            "pipeline_stages": [
                {
                    "stage": 9,
                    "parent_stage": None,
                    "ml_backend_id": str(backend.id),
                    "task": "detect",
                    "source": {
                        "kind": "dataset",
                        "data_type": "image",
                        "execution_unit": "scene",
                    },
                }
            ],
        },
    )
    assert response.status_code == 200, response.text
    message = dispatch.call_args.args[0]
    assert message.accepted_input_manifest_protocol is None
    assert message.accepted_input_manifest_id is None
    assert message.accepted_input_manifest_digest is None
    assert message.execution_unit == "scene"


@pytest.mark.parametrize(
    "code,status",
    [
        ("storage_unavailable", 503),
        ("source_busy", 503),
        ("job_busy", 503),
        ("manifest_scope_mismatch", 409),
    ],
)
async def test_capture_failure_rolls_back_new_job_and_never_publishes(
    httpx_client,
    db_session,
    super_admin,
    dispatch,
    app_module,
    monkeypatch,
    code,
    status,
):
    actor, token = super_admin
    project, backend, _batch, _tasks = await _seed(db_session, actor.id)
    project_id, actor_id = project.id, actor.id
    attempted = []

    class Capture:
        def __init__(self, db):
            self.db = db

        async def capture(self, request):
            job = await self.db.get(AsyncJob, request.job_id)
            assert job.user_id == actor_id
            assert job.payload["accepted_input_manifest_id"] == str(job.id)
            assert job.payload["input_selection"] == request.selection.model_dump(
                mode="json"
            )
            attempted.append(request.job_id)
            raise c.ManifestFailure(code)

    monkeypatch.setitem(
        app_module.dependency_overrides, get_input_manifest_capture, lambda: Capture
    )
    response = await httpx_client.post(
        f"/api/v1/projects/{project_id}/preannotate",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid4())},
        json={"ml_backend_id": str(backend.id)},
    )
    assert response.status_code == status, response.text
    dispatch.assert_not_called()
    assert len(attempted) == 1
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(AsyncJob)
            .where(AsyncJob.id == attempted[0])
        )
        == 0
    )
