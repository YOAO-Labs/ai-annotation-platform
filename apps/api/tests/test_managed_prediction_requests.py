"""Image requests freeze canonical inputs before dispatch and never rebind retries."""

from copy import deepcopy
import hashlib
import io
from types import SimpleNamespace
from unittest.mock import Mock
import uuid

from PIL import Image
import pytest
from sqlalchemy import select

from app.db.models.async_job import AsyncJob
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.prediction import FailedPrediction
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.services import async_job as jobs
from app.services.dataset_assets import register_item_asset
from app.services.prediction_execution import load_snapshot
from app.workers import predictions_retry, tasks as worker
from tests.factory import create_project, create_task
from tests.test_prediction_reference_delivery import backend_for


async def managed_task(db, project, owner, *, primary=False, task=None):
    """Register logical source facts; physical readers need a separate I/O fixture."""
    output = io.BytesIO()
    with Image.new("RGB", (12, 8), "red") as image:
        image.save(output, "PNG")
    raw = output.getvalue()
    dataset = Dataset(
        id=uuid.uuid4(),
        display_id="mi-" + uuid.uuid4().hex[:16],
        name="Managed prediction input",
        created_by=owner.id,
    )
    db.add(dataset)
    await db.flush()
    item = DatasetItem(
        id=uuid.uuid4(),
        dataset_id=dataset.id,
        file_name="same-name.png",
        file_path="managed-input/" + uuid.uuid4().hex + ".png",
        file_type="image",
        file_size=len(raw),
        width=12,
        height=8,
    )
    db.add(item)
    await db.flush()
    asset = await register_item_asset(
        db, item, sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw)
    )
    task = task or await create_task(db, project_id=project.id)
    task.file_path, task.file_name = item.file_path, item.file_name
    if primary:
        db.add(
            TaskDatasetItemLink(
                id=uuid.uuid4(),
                task_id=task.id,
                dataset_item_id=item.id,
                role="primary_lidar",
                sensor_name="primary",
            )
        )
    else:
        task.dataset_item_id = item.id
    await db.flush()
    return SimpleNamespace(task=task, item=item, asset=asset, raw=raw)


@pytest.fixture
async def world(db_session, super_admin, monkeypatch):
    actor, token = super_admin
    project = await create_project(db_session, owner_id=actor.id)
    backend = await backend_for(db_session, project)
    dispatch = Mock(side_effect=lambda **kw: SimpleNamespace(id=kw["task_id"]))
    retry_dispatch = Mock()
    monkeypatch.setattr(worker.batch_predict, "apply_async", dispatch)
    monkeypatch.setattr(
        predictions_retry.retry_failed_prediction, "apply_async", retry_dispatch
    )
    return SimpleNamespace(
        db=db_session,
        actor=actor,
        project=project,
        backend=backend,
        dispatch=dispatch,
        retry_dispatch=retry_dispatch,
        headers={"Authorization": "Bearer " + token},
    )


async def submit(world, client, *, body=None, key=None):
    headers = dict(world.headers)
    if key is not None:
        headers["Idempotency-Key"] = key
    return await client.post(
        f"/api/v1/projects/{world.project.id}/preannotate",
        headers=headers,
        json={
            "ml_backend_id": str(world.backend.id),
            "model_id": "detect",
            **(body or {}),
        },
    )


@pytest.mark.parametrize("primary", [False, True])
@pytest.mark.parametrize("scope", ["bulk", "workbench"])
async def test_managed_single_stage_freezes_source_scope_and_execution(
    world, httpx_client, primary, scope
):
    member = await managed_task(world.db, world.project, world.actor, primary=primary)
    response = await submit(
        world,
        httpx_client,
        body={"task_ids": [str(member.task.id)], "execution_scope": scope},
    )
    assert response.status_code == 200, response.text
    job = await world.db.get(AsyncJob, uuid.UUID(response.json()["job_id"]))
    proof = job.payload["prediction_authority"]
    assert proof["schema"] == 2
    assert job.payload["scope"]["task_ids"] == [str(member.task.id)]
    assert job.execution_snapshot_id is not None
    snapshot, digest = await load_snapshot(
        world.db, job.execution_snapshot_id, world.project.id
    )
    assert snapshot["execution_scope"] == scope
    assert proof["snapshot_digest"] == digest
    original = proof["tasks"][0]["original"]
    assert original["content_id"] == str(member.asset.id)
    assert original["sha256"] == hashlib.sha256(member.raw).hexdigest()
    assert proof["tasks"][0]["version"] == member.task.version
    assert world.dispatch.call_args.kwargs["args"][2] == [str(member.task.id)]


@pytest.mark.parametrize("empty", [False, True])
async def test_skip_predicted_filters_before_freeze_and_empty_scope_stays_empty(
    world, httpx_client, empty
):
    first = await managed_task(world.db, world.project, world.actor)
    first.task.total_predictions = 1
    second = None if empty else await managed_task(world.db, world.project, world.actor)
    response = await submit(
        world, httpx_client, body={"predict_mode": "skip_predicted"}
    )
    assert response.status_code == 200, response.text
    job = await world.db.get(AsyncJob, uuid.UUID(response.json()["job_id"]))
    expected = [] if empty else [str(second.task.id)]
    assert job.payload["scope"]["task_ids"] == expected
    assert [
        row["task_id"] for row in job.payload["prediction_authority"]["tasks"]
    ] == expected
    assert world.dispatch.call_args.kwargs["args"][2] == expected
    assert job.payload["total_tasks"] == len(expected)


async def test_pending_idempotent_republish_reuses_original_scope_and_source(
    world, httpx_client
):
    first = await managed_task(world.db, world.project, world.actor)
    response = await submit(world, httpx_client, key="managed-once")
    assert response.status_code == 200, response.text
    job_id = uuid.UUID(response.json()["job_id"])
    job = await world.db.get(AsyncJob, job_id)
    proof, snapshot_id = (
        deepcopy(job.payload["prediction_authority"]),
        job.execution_snapshot_id,
    )
    second = await managed_task(world.db, world.project, world.actor)
    first.task.dataset_item_id = second.item.id
    first.task.file_path = second.item.file_path
    await world.db.commit()
    response = await submit(world, httpx_client, key="managed-once")
    assert response.status_code == 200, response.text
    assert response.json()["job_id"] == str(job_id)
    await world.db.refresh(job)
    assert job.execution_snapshot_id == snapshot_id
    assert job.payload["prediction_authority"] == proof
    assert world.dispatch.call_args.kwargs["args"][2] == [str(first.task.id)]
    assert str(second.task.id) not in job.payload["scope"]["task_ids"]


async def test_pending_legacy_image_job_requires_fresh_request(world, httpx_client):
    await managed_task(world.db, world.project, world.actor)
    response = await submit(world, httpx_client, key="legacy-pending")
    assert response.status_code == 200, response.text
    job = await world.db.get(AsyncJob, uuid.UUID(response.json()["job_id"]))
    job.payload = {
        key: value
        for key, value in job.payload.items()
        if key != "prediction_authority"
    }
    await world.db.commit()
    count = world.dispatch.call_count
    response = await submit(world, httpx_client, key="legacy-pending")
    assert response.status_code == 409
    assert (
        response.json()["detail"]["code"] == "prediction_original_requires_new_request"
    )
    assert "重新发起预标" in response.json()["detail"]["message"]
    assert world.dispatch.call_count == count


async def test_image_scope_budget_rejects_before_reading_unregistered_sources(
    world, httpx_client
):
    from app.db.models.task import Task

    world.db.add_all(
        [
            Task(
                id=uuid.uuid4(),
                project_id=world.project.id,
                display_id="budget-" + uuid.uuid4().hex[:16],
                file_name="unknown.png",
                file_path="unknown/" + str(index),
                file_type="image",
                status="pending",
            )
            for index in range(1001)
        ]
    )
    await world.db.flush()
    response = await submit(world, httpx_client)
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "prediction_scope_budget_exceeded"
    world.dispatch.assert_not_called()


async def test_managed_request_keeps_existing_key_scope_contract(world, httpx_client):
    await managed_task(world.db, world.project, world.actor)
    response = await httpx_client.post(
        "/api/v1/me/api-keys",
        headers=world.headers,
        json={"name": "Managed input authority", "scopes": []},
    )
    assert response.status_code == 201, response.text
    key = response.json()
    world.headers = {"Authorization": "Bearer " + key["plaintext"]}
    response = await submit(world, httpx_client)
    assert response.status_code == 200, response.text
    job = await world.db.get(AsyncJob, uuid.UUID(response.json()["job_id"]))
    assert job.payload["prediction_authority"]["api_key_id"] == key["id"]


@pytest.mark.parametrize("entry", ["single", "job"])
@pytest.mark.parametrize("change", [None, "version", "source", "legacy"])
async def test_both_managed_retry_entries_keep_failed_source_with_fresh_authority(
    world, httpx_client, entry, change
):
    member = await managed_task(world.db, world.project, world.actor)
    response = await submit(world, httpx_client)
    assert response.status_code == 200, response.text
    job = await world.db.get(AsyncJob, uuid.UUID(response.json()["job_id"]))
    binding = deepcopy(job.payload["prediction_authority"]["tasks"][0])
    failure = FailedPrediction(
        id=uuid.uuid4(),
        task_id=member.task.id,
        project_id=world.project.id,
        ml_backend_id=world.backend.id,
        error_type="Timeout",
        message="fixture",
        execution_snapshot_id=job.execution_snapshot_id,
        extra={} if change == "legacy" else {"original_input": binding},
    )
    world.db.add(failure)
    await world.db.flush()
    await jobs.mark_failed(
        world.db,
        job.id,
        error="fixture",
        result={"failed_prediction_ids": [str(failure.id)]},
    )
    if change == "version":
        member.task.version += 1
    elif change == "source":
        replacement = await managed_task(world.db, world.project, world.actor)
        member.task.dataset_item_id = replacement.item.id
        member.task.file_path = replacement.item.file_path
    await world.db.commit()
    url = (
        f"/api/v1/admin/failed-predictions/{failure.id}/retry"
        if entry == "single"
        else f"/api/v1/async-jobs/{job.id}/retry-failed"
    )
    response = await httpx_client.post(url, headers=world.headers)
    if change in {"source", "legacy"}:
        assert response.status_code == 409, response.text
        expected = (
            "content_binding_changed"
            if change == "source"
            else "prediction_original_requires_new_request"
        )
        assert response.json()["detail"]["code"] == expected
        if change == "legacy":
            assert "重新发起预标" in response.json()["detail"]["message"]
        world.retry_dispatch.assert_not_called()
        return
    assert response.status_code == 202, response.text
    options = world.retry_dispatch.call_args.kwargs
    retry_job = await world.db.get(
        AsyncJob, uuid.UUID(options["kwargs"]["async_job_id"])
    )
    retry_proof = retry_job.payload["prediction_authority"]
    assert retry_proof["schema"] == 2
    assert retry_proof["tasks"][0]["original"] == binding["original"]
    assert retry_proof["tasks"][0]["version"] == member.task.version
    assert retry_job.execution_snapshot_id == job.execution_snapshot_id
    assert (
        len(
            list(
                await world.db.scalars(
                    select(FailedPrediction).where(FailedPrediction.id == failure.id)
                )
            )
        )
        == 1
    )
