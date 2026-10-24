"""Committed video request scopes and final-write races through the real Worker.

Only Celery publication, provider transport and the controlled ML transport are
substituted. Canonical resolution, credentials, model declarations, scope checks,
durable input owners and prediction transactions use the migrated SQL schema.
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import tempfile
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock
import uuid

from fastapi import HTTPException
import httpx
import pytest
from sqlalchemy import func, select

from app.config import settings
from app.core.security import hash_password
from app.db.models.annotation import Annotation
from app.db.models.api_key import ApiKey
from app.db.models.async_job import AsyncJob
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.dataset_asset import StorageBinding
from app.db.models.ml_backend_registry import MLBackendRegistry, ProjectMLBackendPool
from app.db.models.prediction import FailedPrediction, Prediction, PredictionMeta
from app.db.models.project import Project
from app.db.models.storage_maintenance import StorageOperation
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.db.models.task_lock import TaskLock
from app.deps import get_db
from app.services.annotation import AnnotationService
from app.services import managed_original_creation as creation
from app.services import prediction_inputs, task_originals
from app.services import video_prediction_originals as originals
from app.services.dataset_assets import register_item_asset
from app.services.ml_client import MLBackendClient, PredictionResult
from app.services.prediction_execution import load_snapshot
from app.workers import tasks as worker
from tests.conftest import create_registry_with_pool
from tests.factory import create_batch, create_membership, create_project, create_task
from tests.managed_image_inference_fixtures import (
    db_session as db_session,
    sync_world as sync_world,
)
from tests.managed_prediction_worker_fixtures import worker_engine
from tests.test_export_media_bundle_models import _migration
from tests.test_managed_original_creation import Client
from tests.test_prediction_inputs import MemoryStorage, input_world as input_world
from tests.test_video_prediction_inputs import real_video as real_video

pytestmark = pytest.mark.asyncio


class VideoObjectStorage(MemoryStorage):
    """Use the input_world version store, with truthful video MIME and streaming."""

    def _head(self, bucket, key, version=None):
        return {
            **super()._head(bucket, key, version),
            "ContentType": "application/octet-stream",
        }

    def put_object(self, **params):
        assert params["ContentType"] == "application/octet-stream"
        assert not isinstance(params["Body"], (bytes, io.BytesIO))
        blocks = []
        while block := params["Body"].read(prediction_inputs.CHUNK_BYTES):
            blocks.append(block)
        return super().put_object(**{**params, "Body": io.BytesIO(b"".join(blocks))})


def capabilities():
    return {
        "task_contract_revision": 1,
        "models": [
            {
                "id": "track",
                "task": "tracker",
                "supported_inputs": ["video"],
                "supported_geometric_outputs": ["bbox"],
                "resource_profile": {"device": "cpu", "batchable": True},
            }
        ],
    }


@pytest.fixture
async def video_world(
    sync_world,
    input_world,
    db_session,
    super_admin,
    annotator,
    reviewer,
    real_video,
    monkeypatch,
):
    """All immutable facts live in the existing disposable committed namespace."""
    async with input_world.factory.kw["bind"].begin() as connection:
        for name in (
            "0240_secondary_prediction_input_owner",
            "0241_video_prediction_input_owner",
        ):
            await connection.run_sync(
                lambda sync, name=name: _migration(sync, name).upgrade()
            )
    monkeypatch.setattr(
        prediction_inputs, "make_storage", lambda: VideoObjectStorage(input_world)
    )
    monkeypatch.setattr(settings, "ml_backend_router_mode", "off")
    original, probe = real_video
    original.file.seek(0)
    raw = original.file.read()
    original.file.seek(0)
    actor, token = super_admin
    member, member_token = annotator
    reviewer_user, _ = reviewer
    project = await create_project(
        db_session,
        owner_id=actor.id,
        type_key="video-track",
        type_label="Video tracking",
    )
    project.data_type, project.ai_enabled = "video", True
    project.ai_interactive_enabled = False
    await create_membership(
        db_session, project_id=project.id, user_id=member.id, role="annotator"
    )
    await create_membership(
        db_session, project_id=project.id, user_id=reviewer_user.id, role="reviewer"
    )
    backend, pool = await create_registry_with_pool(
        db_session,
        name="video-routes-" + uuid.uuid4().hex[:12],
        enabled_pool=True,
        is_interactive=False,
        health_meta={"capabilities": capabilities()},
    )
    project.ml_backend_pool_id = pool.id
    db_session.add(
        ProjectMLBackendPool(project_id=project.id, pool_id=pool.id, enabled=True)
    )
    batch = await create_batch(db_session, project_id=project.id, status="draft")
    await db_session.commit()
    published = Mock(side_effect=lambda **kw: SimpleNamespace(id=kw["task_id"]))
    monkeypatch.setattr(worker.batch_predict, "apply_async", published)
    monkeypatch.setattr(worker, "_publish_progress", lambda *a, **kw: None)
    world = SimpleNamespace(
        factory=sync_world.factory,
        monkeypatch=monkeypatch,
        source_bytes=sync_world.source_bytes,
        captured=sync_world.captured,
        source_files=sync_world.source_files,
        db=db_session,
        project=project.id,
        batch=batch.id,
        backend=backend.id,
        actor=actor.id,
        member=member.id,
        reviewer=reviewer_user.id,
        headers={"Authorization": "Bearer " + token},
        member_headers={"Authorization": "Bearer " + member_token},
        published=published,
        raw=raw,
        probe=probe,
        calls=[],
        mutations=[],
        setup_calls=[],
        during_model=None,
        during_setup=None,
        shapes=1,
    )

    def capture(spec, maximum, seconds, deadline, stop, release, **kwargs):
        value = world.source_bytes[spec.bucket, spec.object_key]
        assert len(value) == spec.size_bytes <= maximum
        assert hashlib.sha256(value).hexdigest() == spec.sha256
        assert not stop.is_set()
        stream = tempfile.TemporaryFile("w+b")
        stream.write(value)
        stream.flush()
        stream.seek(0)
        world.source_files.append(stream)
        world.captured.append(spec)
        return originals.reader.BinaryStage(
            stream,
            spec.sha256,
            len(value),
            {"size": len(value)},
            _release=release,
            _retain_on_close_error=True,
        )

    async def setup(client):
        world.setup_calls.append(client.backend_id)
        if world.during_setup:
            await world.during_setup()
        return capabilities()

    async def predict(client, tasks, context=None, *, max_response_bytes=None):
        assert client.backend_id == str(world.backend)
        assert len(tasks) == 1 and max_response_bytes == 64 * 1024**2
        query = parse_qs(urlsplit(tasks[0]["file_path"]).query)
        assert query["versionId"][0] not in {"null", ""}
        assert "/prediction-inputs/" in tasks[0]["file_path"]
        assert context["model_id"] == "track"
        world.calls.append(deepcopy(tasks))
        if world.during_model:
            await world.during_model()
        return [
            PredictionResult(
                task_id=tasks[0]["id"],
                result=[
                    {
                        "type": "video_track_bbox",
                        "track_id": index + 17,
                        "class_name": "car",
                        "score": 0.9,
                        "keyframes": [
                            {
                                "frame_index": frame,
                                "bbox": {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
                                "score": 0.9,
                            }
                            for frame in (0, probe["video"]["frame_count"] - 1)
                        ],
                    }
                    for index in range(world.shapes)
                ],
                model_version="controlled",
                meta={"total_cost": 0.25},
            )
        ]

    monkeypatch.setattr(originals.reader, "_capture", capture)
    # Keep RoutedMLBackendClient and its actual selected-transport callback real.
    monkeypatch.setattr(MLBackendClient, "setup", setup)
    monkeypatch.setattr(MLBackendClient, "predict", predict)
    yield world


@pytest.fixture
async def video_client(video_world, app_module):
    """Use real per-request commit/rollback, not the shared SAVEPOINT dependency."""
    missing = object()
    previous = app_module.dependency_overrides.get(get_db, missing)

    async def database():
        async with video_world.factory() as db:
            try:
                yield db
            finally:
                await db.rollback()

    app_module.dependency_overrides[get_db] = database
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_module), base_url="http://test"
        ) as client:
            yield client
    finally:
        if previous is missing:
            app_module.dependency_overrides.pop(get_db, None)
        else:
            app_module.dependency_overrides[get_db] = previous


async def source_task(world, *, origin="direct", status="pending"):
    db = world.db
    project = await db.get(Project, world.project)
    task = await create_task(db, project_id=world.project, status=status)
    task.batch_id, task.file_type = world.batch, "video"
    task.file_name = "same-name.avi"
    if origin == "task":
        task.status = "uploading"
        await db.flush()
        await db.commit()
        allocation = await creation.allocate_task(
            db,
            project_id=project.id,
            task_id=task.id,
            logical_path=task.file_name,
            max_bytes=len(world.raw),
            requested_by=world.actor,
            content_type="video/x-msvideo",
        )
        client = Client()
        client.store(allocation["staging_bucket"], allocation["staging_key"], world.raw)
        with world.monkeypatch.context() as patch:
            patch.setattr(creation, "create_client", lambda **_: client)
            created = await creation.complete_direct(db, allocation)
        task.file_path = allocation["object_key"]
        await task_originals.publish(db, task, allocation, created)
        task.status = status
    else:
        dataset = Dataset(
            display_id="video-" + uuid.uuid4().hex[:16],
            name="Committed video source",
            created_by=world.actor,
        )
        db.add(dataset)
        await db.flush()
        item = DatasetItem(
            dataset_id=dataset.id,
            file_name=task.file_name,
            file_path="video-routes/" + uuid.uuid4().hex + ".avi",
            file_type="video",
            file_size=len(world.raw),
            width=1,
            height=2,
            metadata_={"video": {"width": 1, "height": 2, "frame_count": 99999}},
        )
        db.add(item)
        await db.flush()
        await register_item_asset(
            db,
            item,
            sha256=hashlib.sha256(world.raw).hexdigest(),
            size_bytes=len(world.raw),
            binding=await db.get(StorageBinding, "annotations"),
        )
        task.file_path = item.file_path
        if origin == "direct":
            task.dataset_item_id = item.id
        else:
            assert origin == "primary"
            db.add(
                TaskDatasetItemLink(
                    task_id=task.id,
                    dataset_item_id=item.id,
                    role="primary_lidar",
                    sensor_name="primary",
                )
            )
    await db.flush()
    source = await originals.resolve(db, task.id)
    world.source_bytes[source.bucket, source.object_key] = world.raw
    current = await db.get(StorageBinding, "datasets")
    assert current.bucket != source.bucket
    world.source_bytes[current.bucket, source.object_key] = b"current bucket trap"
    await db.commit()
    return task.id


async def key_headers(world, *, scopes):
    token = "ak_" + uuid.uuid4().hex
    key = ApiKey(
        user_id=world.actor,
        name="Video request",
        key_prefix=token[:12],
        key_hash=hash_password(token),
        scopes=list(scopes),
    )
    world.db.add(key)
    await world.db.commit()
    return key.id, {"Authorization": "Bearer " + token}


async def submit(world, client, task_id, *, scope="bulk", headers=None, key=None):
    auth = dict(headers or world.headers)
    if key:
        auth["Idempotency-Key"] = key
    return await client.post(
        f"/api/v1/projects/{world.project}/preannotate",
        headers=auth,
        json={
            "ml_backend_id": str(world.backend),
            "model_id": "track",
            "task_ids": [str(task_id)],
            "execution_scope": scope,
            "predict_mode": "overwrite",
            "output_mode": "box",
            "pipeline_stages": [
                {
                    "stage": 7,
                    "parent_stage": None,
                    "ml_backend_id": str(world.backend),
                    "model_id": "track",
                    "task": "tracker",
                    "source": {"execution_unit": "video"},
                }
            ],
        },
    )


async def run(world, *, publish_index=-1, delivery=None):
    published = world.published.call_args_list[publish_index].kwargs
    arguments = deepcopy(published["kwargs"])
    arguments["celery_task_id"] = delivery or published["task_id"]
    arguments["worker_node_id"] = "worker:video-route-regression@test"
    async with worker_engine(world):
        await worker._run_batch(*published["args"], **arguments)


async def rows(world, task_id):
    async with world.factory() as db:
        predictions = list(
            (
                await db.scalars(
                    select(Prediction).where(Prediction.task_id == task_id)
                )
            ).all()
        )
        failures = list(
            (
                await db.scalars(
                    select(FailedPrediction).where(FailedPrediction.task_id == task_id)
                )
            ).all()
        )
        return predictions, failures


def lock(task_id, user_id):
    return TaskLock(
        task_id=task_id,
        user_id=user_id,
        expire_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )


@pytest.mark.parametrize("origin", ["task", "direct", "primary"])
async def test_unassigned_draft_bulk_freezes_real_video_and_keeps_predictions_after_assignment(
    video_world, video_client, origin
):
    world = video_world
    task_id = await source_task(world, origin=origin)
    response = await submit(world, video_client, task_id)
    assert response.status_code == 200, response.text
    job_id = uuid.UUID(response.json()["job_id"])
    async with world.factory() as db:
        job = await db.get(AsyncJob, job_id)
        proof = job.payload["video_prediction_authority"]
        snapshot, digest = await load_snapshot(
            db, job.execution_snapshot_id, world.project
        )
        assert proof["snapshot_digest"] == digest
        assert snapshot["stages"][0]["stage"] == 7
        assert proof["tasks"][0]["original"]["schema"] == 3
        assert (
            proof["tasks"][0]["original"]["sha256"]
            == hashlib.sha256(world.raw).hexdigest()
        )
    assert world.published.call_args.kwargs["queue"] == "ml.cpu"
    await run(world)
    predictions, failures = await rows(world, task_id)
    assert len(predictions) == 1 and not failures
    async with world.factory() as db:
        job, batch = (
            await db.get(AsyncJob, job_id),
            await db.get(TaskBatch, world.batch),
        )
        assert job.status == "completed" and batch.status == "draft"
        meta = await db.scalar(
            select(PredictionMeta).where(
                PredictionMeta.prediction_id == predictions[0].id
            )
        )
        assert meta.extra["original_video_input"]["probe"] == world.probe
        batch.annotator_id, batch.status = world.member, "active"
        await db.commit()
    assert [row.id for row in (await rows(world, task_id))[0]] == [predictions[0].id]
    assert len(world.calls) == len(world.captured) == 1
    assert all(stream.closed for stream in world.source_files)


@pytest.mark.parametrize(
    "problem", ["assigned_draft", "foreign_lock", "review", "completed", "admin_lock"]
)
@pytest.mark.parametrize("scope", ["bulk", "workbench"])
async def test_video_request_enforces_each_scope_before_publication(
    video_world, video_client, problem, scope
):
    world = video_world
    task_id = await source_task(world)
    task = await world.db.get(Task, task_id)
    batch = await world.db.get(TaskBatch, world.batch)
    if problem == "assigned_draft":
        batch.annotator_id = world.member
    elif problem == "foreign_lock":
        # The caller's own lock must not hide another member's active lock.
        world.db.add_all([lock(task_id, world.actor), lock(task_id, world.member)])
    elif problem == "admin_lock":
        batch.admin_locked = True
    else:
        task.status = problem
    await world.db.commit()
    response = await submit(world, video_client, task_id, scope=scope)
    expected = 200 if problem == "assigned_draft" and scope == "workbench" else 409
    assert response.status_code == expected, response.text
    assert world.published.call_count == int(expected == 200)
    assert not world.captured and not world.calls
    assert not (await rows(world, task_id))[0]


async def test_video_workbench_member_needs_effective_assignment_and_cannot_bulk(
    video_world, video_client
):
    world = video_world
    task_id = await source_task(world, status="in_progress")
    task = await world.db.get(Task, task_id)
    task.assignee_id = world.member
    world.db.add(lock(task_id, world.member))
    await world.db.commit()
    allowed = await submit(
        world, video_client, task_id, scope="workbench", headers=world.member_headers
    )
    assert allowed.status_code == 200, allowed.text
    denied = await submit(world, video_client, task_id, headers=world.member_headers)
    assert denied.status_code == 403, denied.text
    task.assignee_id = world.actor
    await world.db.commit()
    changed = await submit(
        world, video_client, task_id, scope="workbench", headers=world.member_headers
    )
    assert changed.status_code == 403, changed.text
    assert world.published.call_count == 1


async def test_pending_historical_video_republish_requires_a_fresh_request(
    video_world, video_client
):
    world = video_world
    task_id = await source_task(world)
    response = await submit(world, video_client, task_id, key="video-proof-required")
    assert response.status_code == 200, response.text
    job_id = uuid.UUID(response.json()["job_id"])
    async with world.factory() as db:
        job = await db.get(AsyncJob, job_id)
        job.payload = {
            name: value
            for name, value in job.payload.items()
            if name != "video_prediction_authority"
        }
        await db.commit()
    rejected = await submit(world, video_client, task_id, key="video-proof-required")
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["detail"]["code"] == "prediction_video_requires_new_request"
    assert world.published.call_count == 1
    assert not world.captured and not world.calls


async def test_model_change_during_actual_selected_transport_setup_blocks_predict(
    video_world, video_client
):
    world = video_world
    task_id = await source_task(world)
    response = await submit(world, video_client, task_id)
    assert response.status_code == 200, response.text

    async def change_model():
        async with world.factory() as db:
            backend = await db.get(MLBackendRegistry, world.backend)
            changed = capabilities()
            changed["models"][0]["resource_profile"]["batchable"] = False
            backend.health_meta = {"capabilities": changed}
            await db.commit()

    world.during_setup = change_model
    await run(world)
    predictions, failures = await rows(world, task_id)
    assert not predictions and len(failures) == 1
    assert failures[0].error_type == "ContentError"
    assert "model_changed" in failures[0].message
    assert world.setup_calls == [str(world.backend)]
    assert len(world.captured) == 1 and not world.calls
    assert all(stream.closed for stream in world.source_files)


@pytest.mark.parametrize(
    "scope,change",
    [
        ("bulk", "annotator"),
        ("bulk", "reviewer"),
        ("bulk", "foreign_lock"),
        ("bulk", "terminal"),
        ("workbench", "assignee"),
        ("workbench", "foreign_lock"),
        ("workbench", "terminal"),
        ("workbench", "admin_lock"),
        ("workbench", "model"),
        ("workbench", "key_scope"),
        ("workbench", "key_revoke"),
    ],
)
async def test_changes_after_actual_video_dispatch_preserve_old_candidates_atomically(
    video_world, video_client, scope, change
):
    world = video_world
    task_id = await source_task(world)
    # Use the real member policy for the effective-assignee race.
    auth = world.headers
    if change == "assignee":
        task = await world.db.get(Task, task_id)
        task.assignee_id = world.member
        auth = world.member_headers
    key_id = None
    if change.startswith("key_"):
        key_id, auth = await key_headers(world, scopes=["tasks:write"])
    old = Prediction(
        task_id=task_id,
        project_id=world.project,
        result=[],
        source="batch",
        model_version="previous",
    )
    world.db.add(old)
    await world.db.flush()
    old_id = old.id
    task = await world.db.get(Task, task_id)
    task.total_predictions = 1
    await world.db.commit()
    response = await submit(world, video_client, task_id, scope=scope, headers=auth)
    assert response.status_code == 200, response.text
    job_id = uuid.UUID(response.json()["job_id"])

    async def mutate():
        async with world.factory() as db:
            task, batch = (
                await db.get(Task, task_id),
                await db.get(TaskBatch, world.batch),
            )
            assert (
                await db.scalar(select(Prediction.id).where(Prediction.id == old_id))
                == old_id
            )
            if change in {"annotator", "reviewer"}:
                setattr(
                    batch,
                    change + "_id",
                    world.reviewer if change == "reviewer" else world.member,
                )
            elif change == "foreign_lock":
                db.add(lock(task_id, world.member))
            elif change == "assignee":
                task.assignee_id = world.actor
            elif change == "terminal":
                task.status = "review"
            elif change == "admin_lock":
                batch.admin_locked = True
            elif change == "model":
                backend = await db.get(MLBackendRegistry, world.backend)
                caps = capabilities()
                caps["models"][0]["resource_profile"]["batchable"] = False
                backend.health_meta = {"capabilities": caps}
            elif change == "key_scope":
                (await db.get(ApiKey, key_id)).scopes = ["tasks:read"]
            else:
                (await db.get(ApiKey, key_id)).revoked_at = datetime.now(timezone.utc)
            await db.commit()
            world.mutations.append(change)

    world.during_model = mutate
    await run(world)
    predictions, failures = await rows(world, task_id)
    assert [row.id for row in predictions] == [old_id]
    assert len(failures) == 1
    assert world.mutations == [change]
    assert failures[0].error_type in {"ContentError", "HTTPException"}
    if change == "model":
        assert "model_changed" in failures[0].message
    elif change.startswith("key_"):
        assert "authority_revoked" in failures[0].message
    assert len(world.calls) == len(world.captured) == 1
    async with world.factory() as db:
        job, task = await db.get(AsyncJob, job_id), await db.get(Task, task_id)
        assert job.status == "failed"
        assert job.result["success_count"] == 0 and job.result["failed_count"] == 1
        assert task.total_predictions == 1 and task.annotation_contributor_ids == []
        assert (
            await db.scalar(
                select(func.count())
                .select_from(PredictionMeta)
                .where(PredictionMeta.prediction_id == old_id)
            )
            == 0
        )
        assert await db.scalar(select(StorageOperation.id).limit(1)) is None
    assert all(stream.closed for stream in world.source_files)


async def test_video_second_shape_failure_rolls_back_the_whole_acceptance(
    video_world, video_client, monkeypatch
):
    world = video_world
    task_id = await source_task(world, origin="primary")
    response = await submit(world, video_client, task_id)
    assert response.status_code == 200, response.text
    world.shapes = 2
    await run(world)
    predictions, failures = await rows(world, task_id)
    assert len(predictions) == 1 and not failures
    prediction = predictions[0]
    assert len(prediction.result) == 2
    original = AnnotationService._validate_class_name
    validations = []

    async def validate(service, *args, **kwargs):
        validations.append(args)
        if len(validations) == 2:
            # Flush and observe the first shape inside the real transaction;
            # the failed request must roll this actual INSERT back as well.
            assert (
                await service.db.scalar(
                    select(func.count())
                    .select_from(Annotation)
                    .where(Annotation.task_id == task_id)
                )
                == 1
            )
            raise HTTPException(422, "controlled second shape failure")
        return await original(service, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(AnnotationService, "_validate_class_name", validate)
        rejected = await video_client.post(
            f"/api/v1/tasks/{task_id}/predictions/{prediction.id}/accept",
            headers=world.headers,
        )
    assert rejected.status_code == 422, rejected.text
    assert len(validations) == 2
    async with world.factory() as db:
        task = await db.get(Task, task_id)
        assert (
            await db.scalar(select(Annotation.id).where(Annotation.task_id == task_id))
            is None
        )
        assert task.total_annotations == 0 and not task.is_labeled
        current = await db.scalar(
            select(Prediction).where(Prediction.id == prediction.id)
        )
        assert current.result == prediction.result
    accepted = await video_client.post(
        f"/api/v1/tasks/{task_id}/predictions/{prediction.id}/accept",
        headers=world.headers,
    )
    assert accepted.status_code == 200, accepted.text
    assert len(accepted.json()) == 2


async def test_video_old_delivery_and_completed_redelivery_never_start_another_input(
    video_world, video_client
):
    world = video_world
    task_id = await source_task(world)
    response = await submit(world, video_client, task_id)
    assert response.status_code == 200, response.text
    job_id = uuid.UUID(response.json()["job_id"])
    await run(world, delivery=str(uuid.uuid4()))
    assert not world.calls and not world.captured
    async with world.factory() as db:
        job = await db.get(AsyncJob, job_id)
        assert job.status == "pending" and job.celery_task_id == str(job_id)
    await run(world)
    assert len(world.calls) == len(world.captured) == 1
    before = [row.id for row in (await rows(world, task_id))[0]]
    await run(world)
    assert [row.id for row in (await rows(world, task_id))[0]] == before
    assert len(world.calls) == len(world.captured) == 1
