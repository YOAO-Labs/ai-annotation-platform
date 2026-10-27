"""Dedicated background receipts bind native results to owned reference inputs."""

from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
import uuid

from fastapi import HTTPException
import pytest
from aap_inference_contract.execution import ModelTaskResult, thaw_json
from sqlalchemy import delete, select
from training_contract.model_return import segmentation_identity

from app.config import settings
from app.db.models.async_job import AsyncJob
from app.db.models.prediction import FailedPrediction, Prediction, PredictionMeta
from app.db.models.prediction_execution import PredictionExecutionSnapshot
from app.db.models.trained_model import ModelRelease, TrainedModelVersion
from app.services import async_job as jobs, prediction_references as refs
from app.services import reference_native_predictions as native, trained_models
from app.services.content_snapshot import ContentError
from app.services.jobs_lifecycle_data import job_lock_statement
from aap_inference.backend_transport import PredictionResult
from app.services.prediction import PredictionService
from app.services.prediction_execution import execution_summary, load_snapshot
from app.services.prediction_inputs import PredictionInputs
from app.services.raster_mask_storage import build_rle_reference
from app.services.training_dataset_bridge import content_digest
from app.utils.raster_mask_rle import encode_coco_rle
from app.workers import predictions_retry as retry, tasks as worker
from tests.inference_execution_test_ports import (
    PredictionClientExecutionPort,
    run_batch_request,
    run_client_task_pipeline,
)
from tests.test_dataset_content import content_world as content_world, headers
from tests.test_dataset_references import reference_world as reference_world
from tests.test_prediction_inputs import input_world as input_world
from tests.test_prediction_jobs_worker import _passthrough_engine_and_factory
from tests.test_trained_models import ready_deployment, seed_source


def lineage_example(*, mode="geometry", ancestor=None):
    from tests.test_controlled_segmentation import example

    result, ref, _ = example()
    ref["registry_id"] = str(uuid.uuid4())
    root = {
        "stage": 7,
        "parent_stage": None,
        "ml_backend_id": ref["registry_id"],
        "model_id": f"trained:{ref['model_version_id']}",
        "frozen_model_ref": ref,
    }
    stages = [root]
    snapshot_id, task_id = str(uuid.uuid4()), uuid.uuid4()
    root_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{snapshot_id}:{task_id}:7:0:0"))
    lineage = [
        {
            "shape_id": root_id,
            "stage": 7,
            "parent_stage": None,
            "parent_shape_id": None,
            "transform": None,
        }
    ]
    parent_id, parent_stage = root_id, 7
    if ancestor:
        stages.append(
            {
                "stage": 9,
                "parent_stage": 7,
                "ml_backend_id": ref["registry_id"],
                "input": {"mode": "geometry"},
                "write": {"target": ancestor},
            }
        )
        parent_stage = 9
        if ancestor == "intermediate":
            parent_id = str(uuid.uuid4())
            lineage.append(
                {
                    "shape_id": parent_id,
                    "stage": 9,
                    "parent_stage": 7,
                    "parent_shape_id": root_id,
                    "transform": None,
                }
            )
    stages.append(
        {
            "stage": 11,
            "parent_stage": parent_stage,
            "ml_backend_id": ref["registry_id"],
            "input": {"mode": mode},
            "write": {"target": "geometry"},
        }
    )
    child_id = str(uuid.uuid4())
    lineage.append(
        {
            "shape_id": child_id,
            "stage": 11,
            "parent_stage": parent_stage,
            "parent_shape_id": parent_id,
            "transform": {"ox": 0.1, "oy": 0.1, "sx": 0.5, "sy": 0.5}
            if mode == "crop"
            else None,
        }
    )
    result.result = [
        {
            "pipeline_shape_id": child_id,
            "class_name": "car",
            "geometry": {"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
        }
    ]
    extra = {
        "execution_snapshot": {"id": snapshot_id},
        "pipeline": {
            "shape_lineage": lineage,
            "selections": [
                {
                    "stage": s["stage"],
                    "requested_registry_id": s["ml_backend_id"],
                    "model_id": s.get("model_id"),
                    "model_ref": s.get("frozen_model_ref"),
                    "class_metadata": None,
                    "executed": not (s["stage"] == 9 and ancestor == "attributes"),
                    "executed_registry_id": ref["registry_id"],
                }
                for s in stages
            ],
        },
    }
    return task_id, {"stages": stages}, extra, result


@pytest.mark.parametrize("mode", ["geometry", "crop"])
@pytest.mark.parametrize("ancestor", [None, "attributes", "intermediate"])
def test_vector_lineage_preserves_dropped_and_intermediate_ancestors(mode, ancestor):
    task_id, payload, extra, result = lineage_example(mode=mode, ancestor=ancestor)
    native._validate_payload(
        task_id, [3, 5], payload, extra, result.result, result.meta, 0
    )


@pytest.mark.parametrize(
    "fault",
    [
        "unexecuted",
        "duplicate",
        "parent_stage",
        "parent_shape",
        "cross_result",
        "transform",
        "cycle",
        "root_vector",
    ],
)
def test_vector_lineage_rejects_unexecuted_or_unbound_children(fault):
    task_id, payload, extra, result = lineage_example(ancestor="intermediate")
    pipeline = extra["pipeline"]
    origin = pipeline["shape_lineage"][-1]
    if fault == "unexecuted":
        pipeline["selections"][-1]["executed"] = False
    elif fault == "duplicate":
        pipeline["shape_lineage"].append(deepcopy(origin))
    elif fault == "parent_stage":
        origin["parent_stage"] = 99
    elif fault == "parent_shape":
        origin["parent_shape_id"] = str(uuid.uuid4())
    elif fault == "cross_result":
        root = pipeline["shape_lineage"][0]
        other = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"{extra['execution_snapshot']['id']}:{task_id}:7:1:0",
            )
        )
        pipeline["shape_lineage"][1]["parent_shape_id"] = other
        root["shape_id"] = other
    elif fault == "transform":
        origin["transform"] = {"broken": True}
    elif fault == "root_vector":
        result.result[0]["pipeline_shape_id"] = pipeline["shape_lineage"][0]["shape_id"]
    else:
        pipeline["shape_lineage"][1]["parent_shape_id"] = origin["shape_id"]
    with pytest.raises(ContentError):
        native._validate_payload(
            task_id, [3, 5], payload, extra, result.result, result.meta, 0
        )


@pytest.mark.parametrize(
    "transform",
    [
        None,
        {"ox": 0},
        {"ox": float("nan"), "oy": 0, "sx": 1, "sy": 1},
        {"ox": 0.5, "oy": 0, "sx": 1, "sy": 1},
    ],
)
def test_crop_lineage_requires_a_finite_image_transform(transform):
    task_id, payload, extra, result = lineage_example(mode="crop")
    extra["pipeline"]["shape_lineage"][-1]["transform"] = transform
    with pytest.raises(ContentError):
        native._validate_payload(
            task_id, [3, 5], payload, extra, result.result, result.meta, 0
        )


@pytest.fixture
async def native_world(db_session, reference_world, input_world, monkeypatch):
    _, project, task, content = reference_world
    monkeypatch.setattr(settings, "raster_mask_read_enabled", True)
    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)
    project.raster_mask_native_editing_enabled = True
    project.tool_bindings = {
        **project.tool_bindings,
        "region": {"enabled": True, "classes": [{"name": "car"}]},
    }
    _, run, attempt = await seed_source(db_session, content[2], project=project)
    identity = segmentation_identity("yolo11n-seg", "8.4.177")
    version = TrainedModelVersion(
        project_id=project.id,
        run_id=run.id,
        attempt_id=attempt.id,
        creator_id=content[2].id,
        request_id=uuid.uuid4(),
        request_digest="a" * 64,
        name="Native reference",
        framework="ultralytics",
        framework_version="8.4.177",
        artifact=attempt.artifacts["best.pt"],
        classes=["native"],
        bindings=["car"],
        binding_digest=content_digest(["car"]),
        provenance={
            "task": "segment",
            "base_model": {"id": "yolo11n-seg"},
            "model_return": identity,
        },
        evaluation={},
    )
    db_session.add(version)
    await db_session.flush()
    backend, deployment = await ready_deployment(db_session, project, version)
    db_session.add(
        ModelRelease(
            project_id=project.id,
            model_version_id=version.id,
            deployment_id=deployment.id,
            task="segment",
            creator_id=content[2].id,
            request_id=uuid.uuid4(),
            request_digest="b" * 64,
            generation=1,
            snapshot={},
        )
    )
    await db_session.flush()
    # A ring and disconnected island remain a single native instance.
    pixels = [
        int(
            (3 <= x < 18 and 3 <= y < 18 and not (7 <= x < 14 and 7 <= y < 14))
            or (35 <= x < 40 and 23 <= y < 28)
        )
        for y in range(32)
        for x in range(48)
    ]
    rle = encode_coco_rle(pixels, 48, 32)
    mask = build_rle_reference(rle)
    stored, puts = {}, []

    async def put(value, **kw):
        reference = build_rle_reference(value)
        stored[reference["sha256"]] = deepcopy(value)
        puts.append(reference["sha256"])
        return reference

    async def load(reference, **kw):
        return deepcopy(stored[reference["sha256"]])

    monkeypatch.setattr("app.services.raster_mask_storage.store_coco_rle", put)
    monkeypatch.setattr("app.services.raster_mask_storage.load_coco_rle", load)
    create_original = PredictionInputs.create.__func__

    async def create(cls, factory, **kw):
        return await create_original(cls, input_world.factory, **kw)

    monkeypatch.setattr(PredictionInputs, "create", classmethod(create))
    return SimpleNamespace(
        db=db_session,
        project=project,
        task=task,
        actor=content[2],
        content=content,
        backend=backend,
        deployment=deployment,
        version=version,
        ref=trained_models.frozen_reference(version, deployment),
        rle=rle,
        mask=mask,
        puts=puts,
        stored=stored,
        inputs=input_world,
    )


def response_for(w):
    return PredictionResult(
        task_id=str(w.task.id),
        result=[
            {
                "tool_unit_id": "region",
                "class_name": "native",
                "native_class_index": 0,
                "instance_index": 0,
                "score": 0.6,
                "confidence": 0.6,
                "geometry": {"type": "raster_mask", "mask": deepcopy(w.mask)},
            }
        ],
        score=0.6,
        inference_time_ms=12,
        meta={
            "trained_model": {
                k: w.ref[k] for k in ("model_version_id", "sha256", "classes_digest")
            },
            "mask_objects": {w.mask["sha256"]: deepcopy(w.rle)},
            "segmentation": {
                "output_contract": w.ref["output_contract"],
                "dimensions": [32, 48],
                "runtime_qualification": w.ref["runtime_qualification"],
                "decoder": w.ref["runtime_qualification"]["decoder"],
                "mask_processing": w.ref["runtime_qualification"]["mask_processing"],
            },
        },
    )


async def enqueue(w, client, admin, monkeypatch, *, scope="bulk"):
    if scope == "bulk":
        w.task.status, w.task.assignee_id = "pending", None
    dispatch = Mock(side_effect=lambda **kw: SimpleNamespace(id=kw["task_id"]))
    monkeypatch.setattr(worker.batch_predict, "apply_async", dispatch)
    result = await client.post(
        f"/api/v1/projects/{w.project.id}/preannotate",
        headers=headers(admin),
        json={
            "ml_backend_id": str(w.backend.id),
            "model_id": f"trained:{w.version.id}",
            "task": "segment",
            "task_ids": [str(w.task.id)],
            "execution_scope": scope,
            "predict_mode": "overwrite",
            "pipeline_stages": [
                {
                    "stage": 7,
                    "parent_stage": None,
                    "ml_backend_id": str(w.backend.id),
                    "model_id": f"trained:{w.version.id}",
                    "task": "segment",
                }
            ],
        },
    )
    assert result.status_code == 200, result.text
    return await w.db.get(AsyncJob, uuid.UUID(result.json()["job_id"])), dispatch


async def prepared(w, client, admin, monkeypatch):
    job, _ = await enqueue(w, client, admin, monkeypatch)
    await jobs.mark_running(w.db, job.id)
    await w.db.commit()
    payload, digest = await load_snapshot(w.db, job.execution_snapshot_id, w.project.id)
    owner = refs.ReferencePredictionJob(
        w.db,
        job.id,
        job.payload["prediction_authority"],
        project_id=w.project.id,
        actor_id=w.actor.id,
        task_ids=[w.task.id],
        batch_id=None,
        scope="bulk",
        predict_mode="overwrite",
        snapshot_id=job.execution_snapshot_id,
        snapshot_digest=digest,
        node_id="worker:content@test",
    )
    await owner.open_inputs(w.task, w.inputs.factory)
    result = response_for(w)
    mapped = trained_models.validate_and_map_results(
        (ModelTaskResult(**vars(result)),), w.ref
    )[0]
    result.result = thaw_json(mapped.result)
    result.model_version = mapped.model_version

    class Client:
        async def predict(self, *args, **kw):
            self.last_instance_id = w.backend.id
            return [result]

    # ready_deployment does not change the project's default pool.
    from app.db.models.ml_backend_registry import ProjectMLBackendPool

    pool_id = await w.db.scalar(
        select(ProjectMLBackendPool.pool_id).where(
            ProjectMLBackendPool.project_id == w.project.id
        )
    )
    results, extra, _ = await run_client_task_pipeline(
        w.task,
        payload["stages"],
        [Client()],
        [None],
        resolve_url=owner.resolve_url,
        before_dispatch=lambda stage_id, dispatch: owner.before_dispatch(
            w.task, dispatch, stage_id=stage_id
        ),
        execution_snapshot_id=job.execution_snapshot_id,
        instance_id=w.backend.id,
        pool_id=pool_id,
    )
    extra["execution_snapshot"] = {
        "id": str(job.execution_snapshot_id),
        "digest": digest,
        "stages": execution_summary(payload["stages"]),
    }
    await w.db.scalar(job_lock_statement(job.id, mode="update", populate_existing=True))
    from app.application.preannotation_execution import _preannotation_actor_revoked
    from app.services.project_write_guard import lock_actor_project_authority

    await lock_actor_project_authority(w.db, w.actor.id, w.project.id, nowait=False)
    assert not await _preannotation_actor_revoked(w.db, w.actor.id, w.project.id)
    anchor = await owner.final_receipt(w.task)
    writes = [
        dict(
            task_id=w.task.id,
            project_id=w.project.id,
            ml_backend_id=w.backend.id,
            ml_backend_pool_id=uuid.UUID(extra["pipeline"]["selections"][0]["pool_id"]),
            result=r.result,
            score=r.score,
            model_version=r.model_version,
            inference_time_ms=r.inference_time_ms,
            token_meta=r.meta,
            pipeline_extra=extra,
            model_provenance=w.ref,
        )
        for r in results
    ]
    return job, owner, anchor, writes


@pytest.mark.parametrize(
    "fault", ["shape", "objects", "runtime", "model", "lineage", "dimensions", "gate"]
)
async def test_whole_group_is_qualified_before_first_object_put(
    native_world, httpx_client, super_admin, monkeypatch, fault
):
    w = native_world
    task_id = w.task.id
    _, owner, anchor, writes = await prepared(w, httpx_client, super_admin, monkeypatch)
    bad = deepcopy(writes[0])
    if fault == "shape":
        bad["result"][0]["class_name"] = "foreign"
    elif fault == "objects":
        bad["token_meta"]["mask_objects"] = {}
    elif fault == "runtime":
        bad["token_meta"]["segmentation"]["decoder"] = "contours"
    elif fault == "model":
        w.deployment.status = "failed"
        await w.db.flush()
    elif fault == "lineage":
        bad["pipeline_extra"]["pipeline"]["shape_lineage"][0]["stage"] = 99
    elif fault == "dimensions":
        bad["token_meta"]["segmentation"]["dimensions"] = [48, 32]
    else:
        monkeypatch.setattr(settings, "raster_mask_create_enabled", False)
    # Result index 1 has its own deterministic lineage even in a valid group.
    ident = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL, f"{owner.proof['snapshot_id']}:{w.task.id}:7:1:0"
        )
    )
    bad["result"][0]["pipeline_shape_id"] = ident
    bad["pipeline_extra"]["pipeline"]["shape_lineage"][0]["shape_id"] = ident
    try:
        with pytest.raises((ValueError, HTTPException)):
            await owner.qualify_native_results(w.task, anchor, [writes[0], bad])
        assert not w.puts
        assert (
            await w.db.scalar(
                select(Prediction.id).where(Prediction.task_id == w.task.id)
            )
            is None
        )
    finally:
        await w.db.rollback()
        await owner.close_inputs(task_id)


@pytest.mark.parametrize(
    "fault",
    [
        "forged",
        "payload",
        "owner",
        "snapshot",
        "expired",
        "session",
        "nested",
        "transaction",
        "job",
    ],
)
async def test_private_receipt_rejects_changed_payload_owner_and_transaction(
    native_world, httpx_client, super_admin, monkeypatch, fault
):
    w = native_world
    job, owner, anchor, writes = await prepared(
        w, httpx_client, super_admin, monkeypatch
    )
    receipt = (await owner.qualify_native_results(w.task, anchor, writes))[0]
    write = deepcopy(writes[0])
    task_id = w.task.id
    try:
        if fault == "forged":
            receipt = {"qualification": receipt.qualification}
        elif fault == "payload":
            write["result"][0]["attributes"] = {"forged": True}
        elif fault == "owner":
            await owner.deliveries[task_id][0].close()
        elif fault == "snapshot":
            owner.deliveries[task_id][0]._snapshot.close()
        elif fault == "expired":
            # Keep the receipt's immutable deadlines; advance the actual clock
            # past them so the delivery expires through its real comparison.
            expired = owner.deliveries[task_id][0].record.expires_at + timedelta(
                seconds=1
            )

            class Advanced(datetime):
                @classmethod
                def now(cls, tz=None):
                    return expired

            monkeypatch.setattr(refs, "datetime", Advanced)
        elif fault == "transaction":
            await w.db.commit()
        elif fault == "job":
            job.payload = {**job.payload, "cancel_requested": True}
            await w.db.flush()
        if fault == "session":
            async with w.inputs.factory() as other:
                with pytest.raises(ContentError):
                    await native.validate_receipt(
                        other, task_id, w.project.id, receipt, write
                    )
        elif fault == "nested":
            async with w.db.begin_nested():
                with pytest.raises(ContentError):
                    await PredictionService(w.db).create_from_ml_result(
                        **write, background_native_reference_receipt=receipt
                    )
        else:
            with pytest.raises(ContentError):
                await PredictionService(w.db).create_from_ml_result(
                    **write, background_native_reference_receipt=receipt
                )
        assert not w.puts
    finally:
        await w.db.rollback()
        await owner.close_inputs(task_id)


@pytest.mark.parametrize(
    "mutation",
    [
        None,
        "result",
        "model",
        "lineage",
        "copy",
        "content",
        "gate",
        "history",
        "rejected",
        "superseded",
    ],
)
async def test_native_acceptance_checks_current_content_and_self_contained_proof(
    native_world, httpx_client, super_admin, monkeypatch, mutation
):
    w = native_world
    job, owner, anchor, writes = await prepared(
        w, httpx_client, super_admin, monkeypatch
    )
    task_id, project_id = w.task.id, w.project.id
    receipt = (await owner.qualify_native_results(w.task, anchor, writes))[0]
    prediction = await PredictionService(w.db).create_from_ml_result(
        **writes[0], background_native_reference_receipt=receipt
    )
    await w.db.commit()
    await owner.close_inputs(task_id)
    metadata = await w.db.scalar(
        select(PredictionMeta).where(PredictionMeta.prediction_id == prediction.id)
    )
    assert "mask_objects" not in metadata.extra
    assert metadata.extra[native.PROOF_KEY]["candidate_digest"]
    if mutation == "result":
        prediction.result = [{**prediction.result[0], "class_name": "other"}]
    elif mutation in {"model", "lineage"}:
        extra = deepcopy(metadata.extra)
        if mutation == "model":
            extra["trained_model"]["sha256"] = "f" * 64
        else:
            extra["pipeline"]["shape_lineage"][0]["stage"] = 9
        metadata.extra = extra
    elif mutation == "copy":
        copied = Prediction(
            id=uuid.uuid4(),
            task_id=task_id,
            project_id=project_id,
            result=deepcopy(prediction.result),
            source="ml_backend",
            tool_unit_id="region",
            model_version=prediction.model_version,
            score=prediction.score,
            ml_backend_id=prediction.ml_backend_id,
            ml_backend_pool_id=prediction.ml_backend_pool_id,
        )
        w.db.add(copied)
        await w.db.flush()
        w.db.add(
            PredictionMeta(
                prediction_id=copied.id,
                prediction_created_at=copied.created_at,
                extra=deepcopy(metadata.extra),
            )
        )
        prediction = copied
    elif mutation == "content":
        (w.content[0] / "图片 空格.png").write_bytes(b"changed")
    elif mutation == "gate":
        monkeypatch.setattr(settings, "raster_mask_create_enabled", False)
    elif mutation in {"rejected", "superseded"}:
        prediction.rejected_shape_indexes = [0]
        if mutation == "superseded":
            metadata.extra = {
                **metadata.extra,
                "superseded_by_prediction_id": str(uuid.uuid4()),
            }
    elif mutation == "history":
        snapshot_id = job.execution_snapshot_id
        await w.db.execute(delete(AsyncJob).where(AsyncJob.id == job.id))
        await w.db.execute(
            delete(PredictionExecutionSnapshot).where(
                PredictionExecutionSnapshot.id == snapshot_id
            )
        )
        w.deployment.status = "failed"
    await w.db.commit()
    response = await httpx_client.post(
        f"/api/v1/tasks/{task_id}/predictions/{prediction.id}/accept",
        headers=headers(super_admin),
    )
    if mutation in {None, "history"}:
        assert response.status_code == 200, response.text
        actual = response.json()[0]["geometry"]["mask"]
        assert {key: actual[key] for key in w.mask} == w.mask
    else:
        assert response.status_code in {403, 409}, response.text
    assert not w.inputs.objects


async def test_pruned_root_retains_original_noncontiguous_instance_index(
    native_world, httpx_client, super_admin, monkeypatch
):
    w = native_world
    _, owner, anchor, writes = await prepared(w, httpx_client, super_admin, monkeypatch)
    task_id = w.task.id
    write = writes[0]
    ident = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"{owner.proof['snapshot_id']}:{task_id}:7:0:1")
    )
    write["result"][0].update(instance_index=1, pipeline_shape_id=ident)
    write["pipeline_extra"]["pipeline"]["shape_lineage"].append(
        {
            "shape_id": ident,
            "stage": 7,
            "parent_stage": None,
            "parent_shape_id": None,
            "transform": None,
        }
    )
    try:
        receipt = (await owner.qualify_native_results(w.task, anchor, [write]))[0]
        prediction = await PredictionService(w.db).create_from_ml_result(
            **write, background_native_reference_receipt=receipt
        )
        assert prediction.result[0]["instance_index"] == 1
        assert prediction.result[0]["class_name"] == "car"
    finally:
        await w.db.rollback()
        await owner.close_inputs(task_id)


@pytest.mark.parametrize("scope", ["bulk", "workbench"])
async def test_api_to_batch_worker_persists_qualified_native_reference(
    native_world, httpx_client, super_admin, monkeypatch, scope
):
    w = native_world
    job, dispatch = await enqueue(
        w, httpx_client, super_admin, monkeypatch, scope=scope
    )

    class Client:
        def __init__(self, *args, **kw):
            pass

        async def predict(self, tasks, context=None):
            return trained_models.validate_and_map_results(
                (ModelTaskResult(**vars(response_for(w))),), w.ref
            )

    from app.db.models.ml_backend_registry import ProjectMLBackendPool

    pool_id = await w.db.scalar(
        select(ProjectMLBackendPool.pool_id).where(
            ProjectMLBackendPool.project_id == w.project.id
        )
    )
    monkeypatch.setattr(
        "app.application.preannotation_execution._model_execution_port",
        lambda *_args, **_kwargs: PredictionClientExecutionPort(
            Client(), instance_id=w.backend.id, pool_id=pool_id
        ),
    )
    engine, factory = _passthrough_engine_and_factory(w.db)
    monkeypatch.setattr("sqlalchemy.ext.asyncio.create_async_engine", engine)
    monkeypatch.setattr("sqlalchemy.ext.asyncio.async_sessionmaker", factory)
    monkeypatch.setattr(
        "app.services.prediction_progress.publish_progress", lambda *a, **kw: None
    )
    args = dispatch.call_args.kwargs["args"]
    kwargs = dict(dispatch.call_args.kwargs["kwargs"])
    await run_batch_request(
        project_id=args[0],
        ml_backend_id=args[1],
        task_ids=args[2],
        **kwargs,
        celery_task_id=str(job.id),
        worker_node_id="worker:content@test",
    )
    await w.db.refresh(job)
    failures = list(
        await w.db.scalars(
            select(FailedPrediction.message).where(
                FailedPrediction.project_id == w.project.id
            )
        )
    )
    assert job.status == "completed", failures
    prediction = await w.db.scalar(
        select(Prediction).where(Prediction.task_id == w.task.id)
    )
    metadata = await w.db.scalar(
        select(PredictionMeta).where(PredictionMeta.prediction_id == prediction.id)
    )
    assert metadata.extra[native.PROOF_KEY]["registry_id"] == str(w.backend.id)
    assert prediction.result[0]["class_name"] == "car"
    assert prediction.result[0]["geometry"]["mask"] == w.mask
    assert not w.inputs.objects and w.puts == [w.mask["sha256"]]


@pytest.mark.parametrize("entry", ["single", "job"])
@pytest.mark.parametrize("outcome", ["success", "bad_second"])
async def test_both_retry_apis_use_native_qualification_and_preserve_partial_on_failure(
    native_world, httpx_client, super_admin, monkeypatch, entry, outcome
):
    w = native_world
    original, owner, anchor, writes = await prepared(
        w, httpx_client, super_admin, monkeypatch
    )
    receipt = (await owner.qualify_native_results(w.task, anchor, writes))[0]
    previous = await PredictionService(w.db).create_from_ml_result(
        **writes[0], background_native_reference_receipt=receipt
    )
    previous_id = previous.id
    failure = FailedPrediction(
        project_id=w.project.id,
        task_id=w.task.id,
        ml_backend_id=w.backend.id,
        execution_snapshot_id=original.execution_snapshot_id,
        error_type="Timeout",
        message="fixture",
        extra={
            "reference_input": deepcopy(
                original.payload["prediction_authority"]["tasks"][0]
            ),
            "partial_prediction_ids": [str(previous.id)],
        },
    )
    w.db.add(failure)
    await w.db.flush()
    await jobs.mark_failed(
        w.db,
        original.id,
        error="fixture",
        result={"failed_prediction_ids": [str(failure.id)]},
    )
    await w.db.commit()
    await owner.close_inputs(w.task.id)
    w.puts.clear()
    dispatch = Mock()
    monkeypatch.setattr(retry.retry_failed_prediction, "apply_async", dispatch)
    url = (
        f"/api/v1/admin/failed-predictions/{failure.id}/retry"
        if entry == "single"
        else f"/api/v1/async-jobs/{original.id}/retry-failed"
    )
    response = await httpx_client.post(url, headers=headers(super_admin))
    assert response.status_code == 202, response.text
    options = dispatch.call_args.kwargs
    job_id = options["kwargs"]["async_job_id"]

    class Client:
        def __init__(self, *args, **kw):
            pass

        async def predict(self, tasks, context=None):
            results = [response_for(w)]
            if outcome == "bad_second":
                second = response_for(w)
                second.meta["mask_objects"] = {}
                results.append(second)
            return trained_models.validate_and_map_results(
                tuple(ModelTaskResult(**vars(result)) for result in results),
                w.ref,
            )

    monkeypatch.setattr(
        "app.application.durable_prediction_retry._model_execution_port",
        lambda *_args, **_kwargs: PredictionClientExecutionPort(Client()),
    )

    @asynccontextmanager
    async def factory():
        try:
            yield w.db
        except BaseException:
            await w.db.rollback()
            raise

    result = await retry._do_retry_with_factory(
        factory,
        str(failure.id),
        str(w.actor.id),
        celery_task_id=job_id,
        job_id=job_id,
    )
    previous = await w.db.scalar(
        select(Prediction)
        .where(Prediction.id == previous_id)
        .execution_options(populate_existing=True)
    )
    if outcome == "success":
        assert result["status"] == "succeeded", result
        assert previous.rejected_shape_indexes == [0]
        predictions = list(
            await w.db.scalars(
                select(Prediction).where(Prediction.task_id == w.task.id)
            )
        )
        assert len(predictions) == 2
        fresh = next(p for p in predictions if p.id != previous.id)
        response = await httpx_client.post(
            f"/api/v1/tasks/{w.task.id}/predictions/{fresh.id}/accept",
            headers=headers(super_admin),
        )
        assert response.status_code == 200, response.text
    else:
        assert result["status"] == "failed", result
        assert not w.puts and not previous.rejected_shape_indexes
    assert not w.inputs.objects
