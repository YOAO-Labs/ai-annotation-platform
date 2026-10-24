"""Reference retries and acceptance retain content, authority and commit scope."""

from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlsplit
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy import select, func

from app.db.models.annotation import Annotation
from app.db.models.api_key import ApiKey
from app.db.models.async_job import AsyncJob
from app.db.models.prediction import Prediction, PredictionMeta, FailedPrediction
from app.services import async_job as jobs, prediction_acceptance as acceptance
from app.services.annotation import AnnotationService
from app.services.content_snapshot import ContentError
from app.services.ml_client import PredictionResult
from app.services.prediction_inputs import PredictionInputs
from app.services.prediction_retries import (
    RetryDispatch,
    release_unpublished_retry,
    finish_retry_attempt,
)
from app.workers import predictions_retry as retry
from tests.factory import create_task, create_batch
from tests.test_dataset_content import content_world as content_world, headers
from tests.test_dataset_references import reference_world as reference_world
from tests.test_prediction_inputs import input_world as input_world
from tests.test_prediction_reference_delivery import enqueue, rectangle


async def candidate_world(db, client, world, admin, monkeypatch, *, stages=False):
    _, project, task, content = world
    original, backend, _, _ = await enqueue(
        db, client, world, admin, monkeypatch, stages=stages
    )
    binding = deepcopy(original.payload["prediction_authority"]["tasks"][0])
    candidate = Prediction(
        id=uuid.uuid4(),
        task_id=task.id,
        project_id=project.id,
        ml_backend_id=backend.id,
        result=[rectangle(), rectangle()],
        source="ml_backend",
    )
    db.add(candidate)
    await db.flush()
    evidence = {
        **binding["reference"],
        "item_id": binding["item_id"],
        "schema": 1,
        "task_version": binding["version"],
    }
    metadata = PredictionMeta(
        prediction_id=candidate.id,
        prediction_created_at=candidate.created_at,
        extra={
            "reference_input": evidence,
            "execution_snapshot": {"id": str(original.execution_snapshot_id)},
        },
    )
    failure = FailedPrediction(
        project_id=project.id,
        task_id=task.id,
        ml_backend_id=backend.id,
        error_type="Timeout",
        message="fixture",
        execution_snapshot_id=original.execution_snapshot_id,
        extra={
            "reference_input": binding,
            "partial_prediction_ids": [str(candidate.id)],
        },
    )
    db.add_all([metadata, failure])
    task.total_predictions = 1
    await db.flush()
    await jobs.mark_failed(
        db,
        original.id,
        error="fixture",
        result={"failed_prediction_ids": [str(failure.id)]},
    )
    await db.commit()
    return SimpleNamespace(
        original=original,
        backend=backend,
        task=task,
        project=project,
        content=content,
        candidate=candidate,
        metadata=metadata,
        failure=failure,
    )


async def api_key(client, actor, scopes):
    response = await client.post(
        "/api/v1/me/api-keys",
        headers=headers(actor),
        json={"name": "Reference action", "scopes": scopes},
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["id"]), {
        "Authorization": "Bearer " + response.json()["plaintext"]
    }


@pytest.mark.parametrize("entry", ["single", "job"])
@pytest.mark.parametrize(
    "outcome", ["success", "key", "version", "dismiss", "partial", "bad_metadata"]
)
async def test_retry_new_authority_original_snapshot_and_atomic_candidates(
    db_session,
    httpx_client,
    reference_world,
    super_admin,
    monkeypatch,
    input_world,
    entry,
    outcome,
):
    world = await candidate_world(
        db_session,
        httpx_client,
        reference_world,
        super_admin,
        monkeypatch,
        stages=outcome not in {"bad_metadata"},
    )
    world.task.version += (
        2  # A new retry freezes current legal state, not the old version.
    )
    key_id, auth = await api_key(httpx_client, super_admin, ["*"])
    dispatch = Mock()
    monkeypatch.setattr(retry.retry_failed_prediction, "apply_async", dispatch)
    url = (
        f"/api/v1/admin/failed-predictions/{world.failure.id}/retry"
        if entry == "single"
        else f"/api/v1/async-jobs/{world.original.id}/retry-failed"
    )
    response = await httpx_client.post(url, headers=auth)
    assert response.status_code == 202, response.text
    options = dispatch.call_args.kwargs
    job_id = uuid.UUID(options["kwargs"]["async_job_id"])
    job = await db_session.get(AsyncJob, job_id)
    proof = deepcopy(job.payload["prediction_authority"])
    assert (
        proof["api_key_id"] == str(key_id) and proof["job_kind"] == "prediction_retry"
    )
    assert proof["tasks"][0]["version"] == world.task.version
    assert job.execution_snapshot_id == world.original.execution_snapshot_id
    assert (
        proof["tasks"][0]["reference"]["sha256"]
        == world.failure.extra["reference_input"]["reference"]["sha256"]
    )
    original_create = PredictionInputs.create.__func__

    async def create(cls, factory, **kwargs):
        return await original_create(cls, input_world.factory, **kwargs)

    monkeypatch.setattr(PredictionInputs, "create", classmethod(create))
    calls = []

    class Client:
        def __init__(self, *a, **kw):
            pass

        async def predict(self, tasks, context=None):
            for payload in tasks:
                bucket, key = (
                    urlsplit(payload["file_path"]).path.lstrip("/").split("/", 1)
                )
                raw = input_world.objects[bucket, key]
                if "/original/" in key:
                    assert raw == world.content[1]
                calls.append(key)
            if outcome == "partial" and len(calls) == 2:
                raise RuntimeError("model unavailable")
            if outcome == "key":
                key = await db_session.get(ApiKey, key_id)
                key.revoked_at = datetime.now(timezone.utc)
            elif outcome == "version":
                world.task.version += 1
            elif outcome == "dismiss":
                world.failure.dismissed_at = datetime.now(timezone.utc)
            await db_session.commit()
            if "/crops/" in calls[-1]:
                result = [{"attributes": {"color": "red"}}]
            else:
                shape = rectangle()
                if tasks[0].get("prompts"):
                    shape["parent_box_idx"] = 0
                result = [shape]
            results = [PredictionResult(task_id=tasks[0]["id"], result=result)]
            if outcome == "bad_metadata":
                results.append(
                    PredictionResult(
                        task_id=tasks[0]["id"],
                        result=result,
                        meta={"total_cost": "invalid"},
                    )
                )
            return results

    monkeypatch.setattr("app.services.ml_client.MLBackendClient", Client)

    @asynccontextmanager
    async def factory():
        try:
            yield db_session
        except BaseException:
            await db_session.rollback()
            raise

    task_id = world.task.id
    fid, actor_id, candidate_id = (
        world.failure.id,
        super_admin[0].id,
        world.candidate.id,
    )
    result = await retry._do_retry_with_factory(
        factory,
        str(fid),
        str(actor_id),
        celery_task_id=str(job_id),
        async_job_id=str(job_id),
        worker_node_id="worker:content@test",
    )
    job = await db_session.get(AsyncJob, job_id, populate_existing=True)
    predictions = list(
        await db_session.scalars(
            select(Prediction).where(Prediction.task_id == task_id)
        )
    )
    previous = next(p for p in predictions if p.id == candidate_id)
    if outcome == "success":
        assert result["status"] == "succeeded", result
        assert job.status == "completed" and len(predictions) == 2
        assert previous.rejected_shape_indexes == [0, 1]
        assert (
            await db_session.get(FailedPrediction, fid, populate_existing=True) is None
        )
    else:
        assert result["status"] == "failed", result
        assert job.status == "failed" and len(predictions) == 1
        assert not previous.rejected_shape_indexes
        failure = await db_session.get(FailedPrediction, fid, populate_existing=True)
        assert failure.retry_count == 1
        assert not failure.extra.get("retry_pending") and not failure.extra.get(
            "retry_job_id"
        )
    assert not input_world.objects
    assert set(input_world.deletions) == {(input_world.bucket, key) for key in calls}
    assert all(body.closed for body in input_world.bodies)
    assert all(storage.closed for storage in input_world.storages)
    count = len(calls)
    await retry._do_retry_with_factory(
        factory,
        str(fid),
        str(actor_id),
        celery_task_id=str(job_id),
        async_job_id=str(job_id),
        worker_node_id="worker:content@test",
    )
    assert len(calls) == count


@pytest.mark.parametrize("change", ["legacy", "identity", "no_read_scope"])
async def test_retry_rejects_missing_original_content_or_new_read_authority(
    db_session, httpx_client, reference_world, super_admin, monkeypatch, change
):
    world = await candidate_world(
        db_session, httpx_client, reference_world, super_admin, monkeypatch
    )
    if change == "legacy":
        world.failure.extra = {}
    elif change == "identity":
        data = deepcopy(world.failure.extra)
        data["reference_input"]["reference"]["sha256"] = "0" * 64
        world.failure.extra = data
    auth = headers(super_admin)
    if change == "no_read_scope":
        _, auth = await api_key(httpx_client, super_admin, ["annotations:write"])
    dispatch = Mock()
    monkeypatch.setattr(retry.retry_failed_prediction, "apply_async", dispatch)
    response = await httpx_client.post(
        f"/api/v1/admin/failed-predictions/{world.failure.id}/retry", headers=auth
    )
    assert response.status_code in {403, 409}, response.text
    dispatch.assert_not_called()


async def test_retry_signal_and_response_loss_keep_attempt_ownership(
    db_session, httpx_client, reference_world, super_admin, monkeypatch
):
    world = await candidate_world(
        db_session, httpx_client, reference_world, super_admin, monkeypatch
    )
    dispatch = Mock()
    monkeypatch.setattr(retry.retry_failed_prediction, "apply_async", dispatch)
    response = await httpx_client.post(
        f"/api/v1/admin/failed-predictions/{world.failure.id}/retry",
        headers=headers(super_admin),
    )
    assert response.status_code == 202
    job_id = uuid.UUID(dispatch.call_args.kwargs["kwargs"]["async_job_id"])
    await jobs.mark_running(db_session, job_id)
    await db_session.commit()
    await release_unpublished_retry(
        db_session, RetryDispatch(world.failure.id, "unused", job_id)
    )
    assert world.failure.extra["retry_job_id"] == str(job_id)
    from app.workers import signals
    from tests.test_prediction_jobs_worker import _passthrough_engine_and_factory

    engine, factory = _passthrough_engine_and_factory(db_session)
    monkeypatch.setattr(signals, "create_async_engine", engine)
    monkeypatch.setattr(signals, "async_sessionmaker", factory)
    for _ in range(2):
        await signals._mark_cancelled(str(job_id))
    assert world.failure.retry_count == 1
    assert not world.failure.extra.get("retry_pending")
    world.failure.extra = {
        **world.failure.extra,
        "retry_pending": True,
        "retry_job_id": str(uuid.uuid4()),
    }
    await finish_retry_attempt(db_session, job_id, reason="late failure")
    assert world.failure.extra["retry_pending"]
    from app.services.prediction import release_failed_prediction_retry_claim

    await release_failed_prediction_retry_claim(db_session, world.failure.id)
    await retry._bump_retry_counter(db_session, world.failure.id)
    assert world.failure.extra["retry_pending"] and world.failure.retry_count == 1


@pytest.mark.parametrize("entry", ["single", "job"])
@pytest.mark.parametrize("qualified", [False, True])
async def test_cpu_retry_keeps_queue_and_rejects_unqualified_image_history(
    db_session,
    httpx_client,
    reference_world,
    super_admin,
    monkeypatch,
    entry,
    qualified,
):
    from app.config import settings

    world = await candidate_world(
        db_session, httpx_client, reference_world, super_admin, monkeypatch
    )
    if not qualified:
        world.failure.execution_snapshot_id = None
        world.failure.extra = {}
        managed = await create_task(db_session, project_id=world.project.id)
        world.failure.task_id = managed.id
    world.backend.health_meta = {
        "capabilities": {"resource_profile": {"device": "cpu"}}
    }
    await db_session.commit()
    dispatch = Mock()
    monkeypatch.setattr(retry.retry_failed_prediction, "apply_async", dispatch)
    path = (
        f"/api/v1/admin/failed-predictions/{world.failure.id}/retry"
        if entry == "single"
        else f"/api/v1/async-jobs/{world.original.id}/retry-failed"
    )
    response = await httpx_client.post(path, headers=headers(super_admin))
    if qualified:
        assert response.status_code == 202, response.text
        assert dispatch.call_args.kwargs["queue"] == settings.preannotate_cpu_queue
    else:
        assert response.status_code == 409, response.text
        assert (
            response.json()["detail"]["code"]
            == "prediction_original_requires_new_request"
        )
        dispatch.assert_not_called()
        assert await db_session.get(FailedPrediction, world.failure.id) is not None


@pytest.mark.parametrize("single", [False, True])
async def test_accept_after_draft_assignment_and_version_change(
    db_session,
    httpx_client,
    reference_world,
    super_admin,
    annotator,
    monkeypatch,
    single,
):
    world = await candidate_world(
        db_session, httpx_client, reference_world, super_admin, monkeypatch
    )
    batch = await create_batch(db_session, project_id=world.project.id, status="draft")
    world.task.batch_id = batch.id
    await db_session.commit()
    batch.status = "active"
    batch.annotator_id = annotator[0].id
    world.task.status = "in_progress"
    world.task.assignee_id = annotator[0].id
    world.task.version += 3
    await db_session.commit()
    url = f"/api/v1/tasks/{world.task.id}/predictions/{world.candidate.id}/accept"
    response = await httpx_client.post(
        url, params={"shape_index": 0} if single else {}, headers=headers(annotator)
    )
    assert response.status_code == 200, response.text
    assert len(response.json()) == (1 if single else 2)
    assert all(row["task_id"] == str(world.task.id) for row in response.json())
    assert world.task.total_annotations == len(response.json())


@pytest.mark.parametrize(
    "change",
    [
        "original",
        "source",
        "provenance",
        "identity",
        "mask",
        "invalid_geometry",
        "uuid",
        "foreign",
        "foreign_managed",
    ],
)
async def test_accept_requires_current_original_and_bound_geometric_candidate(
    db_session, httpx_client, reference_world, super_admin, monkeypatch, change
):
    world = await candidate_world(
        db_session, httpx_client, reference_world, super_admin, monkeypatch
    )
    world.task.status = "in_progress"
    prediction_id = str(world.candidate.id)
    task_id = world.task.id
    if change == "original":
        (world.content[0] / "图片 空格.png").write_bytes(b"replaced")
    elif change == "source":
        world.content[5].enabled = False
    elif change == "provenance":
        world.metadata.extra = {}
    elif change == "identity":
        extra = deepcopy(world.metadata.extra)
        extra["reference_input"]["asset_id"] = str(uuid.uuid4())
        world.metadata.extra = extra
    elif change in {"mask", "invalid_geometry"}:
        world.candidate.result = [
            {
                "class_name": "car",
                "geometry": {"type": "raster_mask"} if change == "mask" else None,
            }
        ]
    elif change == "uuid":
        prediction_id = "not-a-uuid"
    elif change in {"foreign", "foreign_managed"}:
        other = await create_task(db_session, project_id=world.project.id)
        world.candidate.task_id = other.id
        if change == "foreign_managed":
            task_id = (await create_task(db_session, project_id=world.project.id)).id
    await db_session.commit()
    response = await httpx_client.post(
        f"/api/v1/tasks/{task_id}/predictions/{prediction_id}/accept",
        headers=headers(super_admin),
    )
    assert response.status_code == (
        422
        if change == "uuid"
        else 404
        if change in {"foreign", "foreign_managed"}
        else 409
    ), response.text
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Annotation)
            .where(Annotation.project_id == world.project.id)
        )
        == 0
    )


@pytest.mark.parametrize(
    "scopes, allowed",
    [
        (["tasks:read", "annotations:write"], True),
        (["tasks:read"], False),
        (["annotations:write"], False),
    ],
)
async def test_accept_key_scopes_are_read_and_annotation_write(
    db_session, httpx_client, reference_world, super_admin, monkeypatch, scopes, allowed
):
    world = await candidate_world(
        db_session, httpx_client, reference_world, super_admin, monkeypatch
    )
    _, auth = await api_key(httpx_client, super_admin, scopes)
    response = await httpx_client.post(
        f"/api/v1/tasks/{world.task.id}/predictions/{world.candidate.id}/accept",
        headers=auth,
    )
    assert response.status_code == (200 if allowed else 403), response.text


async def test_accept_receipt_actor_candidate_session_and_transaction_binding(
    db_session,
    httpx_client,
    reference_world,
    super_admin,
    annotator,
    monkeypatch,
    input_world,
):
    world = await candidate_world(
        db_session, httpx_client, reference_world, super_admin, monkeypatch
    )
    receipt = await acceptance.qualify_acceptance(
        db_session,
        world.task,
        super_admin[0],
        SimpleNamespace(state=SimpleNamespace()),
        world.candidate.id,
    )
    for fake in [True, {}, object()]:
        with pytest.raises(ContentError):
            await acceptance.validate_acceptance(
                db_session, world.candidate, super_admin[0].id, fake
            )
    with pytest.raises(ContentError):
        await acceptance.validate_acceptance(
            db_session, world.candidate, annotator[0].id, receipt
        )
    async with input_world.factory() as other:
        with pytest.raises(ContentError):
            await acceptance.validate_acceptance(
                other, world.candidate, super_admin[0].id, receipt
            )
    async with db_session.begin_nested():
        with pytest.raises(ContentError):
            await acceptance.validate_acceptance(
                db_session, world.candidate, super_admin[0].id, receipt
            )
    await acceptance.validate_acceptance(
        db_session, world.candidate, super_admin[0].id, receipt
    )
    await db_session.commit()
    with pytest.raises(ContentError):
        await acceptance.validate_acceptance(
            db_session, world.candidate, super_admin[0].id, receipt
        )
    with pytest.raises(HTTPException):
        await AnnotationService(db_session).accept_prediction(
            world.candidate.id, super_admin[0].id
        )


@pytest.mark.parametrize("change", ["key", "source", "version", "candidate", "account"])
async def test_accept_rechecks_after_strict_capture(
    db_session, httpx_client, reference_world, super_admin, monkeypatch, change
):
    world = await candidate_world(
        db_session, httpx_client, reference_world, super_admin, monkeypatch
    )
    key_id, auth = await api_key(
        httpx_client, super_admin, ["tasks:read", "annotations:write"]
    )
    capture = acceptance.reference_snapshot
    snapshots = []
    project_id, task_id, prediction_id = (
        world.project.id,
        world.task.id,
        world.candidate.id,
    )

    async def changed(*args, **kwargs):
        snapshot = await capture(*args, **kwargs)
        snapshots.append(snapshot)
        if change == "key":
            key = await db_session.get(ApiKey, key_id)
            key.key_hash = "replacement"
        elif change == "source":
            world.content[5].enabled = False
        elif change == "version":
            world.task.version += 1
        elif change == "candidate":
            world.candidate.result = [rectangle()]
        elif change == "account":
            super_admin[0].is_active = False
        await db_session.flush()
        return snapshot

    monkeypatch.setattr(acceptance, "reference_snapshot", changed)
    response = await httpx_client.post(
        f"/api/v1/tasks/{task_id}/predictions/{prediction_id}/accept", headers=auth
    )
    assert response.status_code in {403, 409}, response.text
    assert snapshots and snapshots[0].file.closed
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Annotation)
            .where(Annotation.project_id == project_id)
        )
        == 0
    )


@pytest.mark.parametrize("change", ["assignment", "status"])
async def test_accept_refreshes_cached_batch_under_final_lock(
    db_session,
    httpx_client,
    reference_world,
    super_admin,
    annotator,
    monkeypatch,
    change,
):
    from sqlalchemy import update
    from app.db.models.task_batch import TaskBatch

    world = await candidate_world(
        db_session, httpx_client, reference_world, super_admin, monkeypatch
    )
    batch = await create_batch(db_session, project_id=world.project.id, status="active")
    batch.annotator_id = annotator[0].id
    world.task.batch_id = batch.id
    world.task.assignee_id = None
    world.task.status = "in_progress"
    await db_session.commit()
    capture = acceptance.reference_snapshot

    async def changed(*args, **kwargs):
        snapshot = await capture(*args, **kwargs)
        values = (
            {"annotator_id": super_admin[0].id}
            if change == "assignment"
            else {"status": "completed"}
        )
        await db_session.execute(
            update(TaskBatch)
            .where(TaskBatch.id == batch.id)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        assert batch.status == "active" and batch.annotator_id == annotator[0].id
        return snapshot

    monkeypatch.setattr(acceptance, "reference_snapshot", changed)
    response = await httpx_client.post(
        f"/api/v1/tasks/{world.task.id}/predictions/{world.candidate.id}/accept",
        headers=headers(annotator),
    )
    assert response.status_code in {403, 404, 409}, response.text
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Annotation)
            .where(Annotation.task_id == world.task.id)
        )
        == 0
    )


@pytest.mark.parametrize("native", ["objects", "geometry", "brush"])
async def test_reference_writer_keeps_native_mask_gate_before_materialization(
    db_session, reference_world, input_world, monkeypatch, native
):
    from app.services.prediction import PredictionService
    from app.services.prediction_execution import validate_task_scope
    from app.workers.tasks import _hold_preannotation_authority
    from tests.test_prediction_reference_delivery import prepared

    job, owner, task, project, actor = await prepared(db_session, reference_world)
    task_id = task.id
    await owner.open_inputs(task, input_world.factory)
    try:
        await jobs.lock_job(db_session, job.id)
        assert await _hold_preannotation_authority(db_session, actor, project.id)
        await owner.lock_key()
        await validate_task_scope(db_session, project, task, actor, "bulk", lock=True)
        receipt = await owner.final_receipt(task)
        materialize = Mock(
            side_effect=AssertionError("native materialization was reached")
        )
        monkeypatch.setattr(
            "app.services.controlled_segmentation.materialize_masks", materialize
        )
        result = (
            [{"type": "raster_mask", "geometry": {"type": "raster_mask", "mask": {}}}]
            if native == "geometry"
            else [{"type": "brushlabels"}]
            if native == "brush"
            else []
        )
        with pytest.raises(
            ContentError, match="reference_prediction_geometry_not_supported"
        ):
            await PredictionService(db_session).create_from_ml_result(
                task.id,
                project.id,
                None,
                result,
                token_meta={"mask_objects": {}} if native == "objects" else None,
                reference_receipt=receipt,
            )
        materialize.assert_not_called()
        assert (
            await db_session.scalar(
                select(func.count())
                .select_from(Prediction)
                .where(Prediction.task_id == task_id)
            )
            == 0
        )
    finally:
        await db_session.rollback()
        await owner.close_inputs(task_id)
