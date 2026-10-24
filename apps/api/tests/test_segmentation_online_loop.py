"""Database/API loop with deterministic artifacts; this is not GPU qualification.

Only object transport and the remote ML client are substituted. Dataset freezing,
native package production, artifact identity, deployment, prediction persistence,
human acceptance/review, initialization serialization and learning use their owners.
"""

import copy
import hashlib
import io
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from botocore.exceptions import ClientError
from botocore.response import StreamingBody
from PIL import Image
import pytest
from sqlalchemy import select
from training_contract import definition
from training_contract.dataset import (
    INSTANCE_RLE_CONTRACT,
    INSTANCE_RLE_LIMITS,
    SEGMENTATION_CONTRACT,
)
from training_contract.initialization import (
    PIN_KEYS,
    POLICY_DIGEST,
    SEGMENTATION_POLICY_DIGEST,
)
from training_contract.model_return import SEGMENTATION_RUNTIME

from app.config import settings
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.learning_policy import LearningPolicy, LearningTrigger
from app.db.models.ml_backend_registry import ProjectMLBackendPool
from app.db.models.prediction import PredictionMeta
from app.db.models.trained_model import TrainedModelVersion
from app.db.models.training_dataset import TrainingDatasetVersion
from app.db.models.training_run import TrainingAttempt, TrainingRun
from app.schemas.training_run import TrainingRunCreate
from app.services import (
    learning_policies,
    trained_models,
    training_datasets,
    training_run_storage,
    training_runs,
)
from app.services.annotation import AnnotationService
from app.services.ml_client import MLBackendClient, PredictionResult
from app.services.prediction import PredictionService
from app.services.raster_mask_storage import build_rle_reference, store_coco_rle
from app.services.storage import storage_service as storage
from app.services.training_dataset_bridge import content_digest, semantic_partitions
from app.services.training_evaluations import SEMANTICS
from app.utils.raster_mask_rle import decode_coco_rle, encode_coco_rle
from app.workers import training_dataset as dataset_worker
from tests.conftest import create_registry_with_pool
from tests.factory import (
    build_tool_bindings,
    create_membership,
    create_project,
    create_task,
)
from tests.test_training_runs import ready_dataset
from tests.test_training_runs import training_config as training_config_fixture  # noqa: F401, F811

pytestmark = pytest.mark.usefixtures("training_config_fixture")


def auth(token):
    return {"Authorization": "Bearer " + token}


def configuration():
    schema = definition("ultralytics", "yolo11n-seg", version="8.4.177")
    return {
        "schema_id": schema["schema_id"],
        "schema_digest": schema["schema_digest"],
        "values": {"epochs": 3, "batch": 1, "imgsz": 128},
    }


@pytest.fixture
def memory_objects(monkeypatch, training_config_fixture):  # noqa: F811
    """Boto-shaped transport retains bytes/metadata, including conditional reads."""
    values = {}

    def put(*, Bucket, Key, Body, Metadata=None, **kwargs):
        raw = Body.read() if hasattr(Body, "read") else bytes(Body)
        values[Bucket, Key] = (raw, dict(Metadata or {}))
        return {"ETag": '"' + hashlib.sha256(raw).hexdigest() + '"'}

    def head(*, Bucket, Key, **kwargs):
        if (Bucket, Key) not in values:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "HeadObject")
        raw, metadata = values[Bucket, Key]
        return {
            "ContentLength": len(raw),
            "Metadata": dict(metadata),
            "ETag": '"' + hashlib.sha256(raw).hexdigest() + '"',
        }

    def get(*, Bucket, Key, IfMatch=None, **kwargs):
        result = head(Bucket=Bucket, Key=Key)
        if IfMatch is not None and IfMatch != result["ETag"]:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "GetObject")
        raw = values[Bucket, Key][0]
        if kwargs.get("Range"):
            raw = raw[: 1024 * 1024]
        return {**result, "Body": StreamingBody(io.BytesIO(raw), len(raw))}

    def upload(Filename, Bucket, Key, ExtraArgs=None):
        put(
            Bucket=Bucket,
            Key=Key,
            Body=Path(Filename).read_bytes(),
            **(ExtraArgs or {}),
        )

    def list_objects(*, Bucket, Prefix, **kwargs):
        return {
            "Contents": [
                {"Key": key}
                for bucket, key in values
                if bucket == Bucket and key.startswith(Prefix)
            ],
            "IsTruncated": False,
        }

    def delete_objects(*, Bucket, Delete):
        for row in Delete["Objects"]:
            values.pop((Bucket, row["Key"]), None)
        return {}

    monkeypatch.setattr(storage.client, "put_object", put)
    monkeypatch.setattr(storage.client, "head_object", head)
    monkeypatch.setattr(storage.client, "get_object", get)
    monkeypatch.setattr(storage.client, "upload_file", upload)
    monkeypatch.setattr(storage.client, "list_objects_v2", list_objects)
    monkeypatch.setattr(storage.client, "delete_objects", delete_objects)
    monkeypatch.setattr(
        storage.client,
        "delete_object",
        lambda *, Bucket, Key: values.pop((Bucket, Key), None),
    )
    monkeypatch.setattr(training_datasets, "dispatch_dataset", AsyncMock())
    monkeypatch.setattr(dataset_worker, "notify_job_terminal", AsyncMock())
    monkeypatch.setattr(settings, "raster_mask_read_enabled", True)
    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)
    catalog = copy.deepcopy(settings.training_catalog_json)
    catalog["ultralytics"]["base_models"]["yolo11n-seg"] = {
        "filename": "yolo11n-seg.pt",
        "sha256": "9" * 64,
    }
    monkeypatch.setattr(settings, "training_catalog_json", catalog)
    try:
        yield SimpleNamespace(values=values, put=put)
    finally:
        values.clear()


async def freeze(client, token, db, project, *, parent=None, name="Frozen masks"):
    path = f"/api/v1/projects/{project.id}/training-datasets"
    selection = {
        "task": "segment",
        "bridge": INSTANCE_RLE_CONTRACT["bridge"],
        **({"parent_id": str(parent.id)} if parent else {}),
    }
    preflight = await client.post(
        path + ":preflight", headers=auth(token), json=selection
    )
    assert preflight.status_code == 200 and preflight.json()["ready"], preflight.text
    response = await client.post(
        path,
        headers=auth(token),
        json={
            **selection,
            "name": name,
            "request_id": str(uuid.uuid4()),
            "preflight_digest": preflight.json()["digest"],
        },
    )
    assert response.status_code == 202, response.text
    version = await db.get(TrainingDatasetVersion, uuid.UUID(response.json()["id"]))
    assert {
        key: version.snapshot[key] for key in INSTANCE_RLE_CONTRACT
    } == INSTANCE_RLE_CONTRACT

    @asynccontextmanager
    async def sessions():
        yield db

    # Use the production worker body against the same SAVEPOINT-backed session.
    await dataset_worker._build_dataset(
        version.id, version.async_job_id, version.attempt_id, sessions
    )
    await db.refresh(version)
    assert version.status == "ready", (version.error_code, version.error_message)
    return version


def report_for(run, weights):
    metrics = {"metrics/mAP50-95(B)": 0.7, "metrics/mAP50-95(M)": 0.6}
    consumer = {
        "bridge": INSTANCE_RLE_CONTRACT["bridge"],
        "workers": 0,
        "prefetch": False,
        "limits": dict(INSTANCE_RLE_LIMITS),
        "loss_mask_processing": "truth-full-input-plane;prototype-bilinear-upsample-align-corners-false",
        "validator_mask_processing": "truth-bilinear-align-corners-false-threshold-0.5-at-input-quarter",
        "loss_source_sha256": "4" * 64,
        "validator_source_sha256": "5" * 64,
    }
    return {
        "validation_digest": run.config["validation_digest"],
        "evaluation": {
            "framework": "ultralytics",
            "version": "8.4.177",
            "task": "instance-segmentation",
        },
        "summary": {**metrics, "fitness": 1.3},
        "per_class": [
            {
                "index": 0,
                "name": "shape",
                **{
                    key.removeprefix("metrics/"): value
                    for key, value in metrics.items()
                },
            }
        ],
        "comparison": {
            "revision": 3,
            "config_digest": run.config_digest,
            "candidate": {
                "weights_sha256": weights["sha256"],
                "selection": "native_best",
                "best_epoch": 1,
            },
            "benchmark": {
                "validation_digest": run.config["validation_digest"],
                "classes": ["shape"],
                "class_space": "frozen_native",
            },
            "evaluator": {
                "framework": "ultralytics",
                "version": "8.4.177",
                "task": "instance-segmentation",
                "implementation": "training_executor.instance_rle.InstanceRLEValidator",
                "source_sha256": "6" * 64,
                "options": {
                    "split": "val",
                    "imgsz": 128,
                    "batch": 1,
                    "conf": 0.001,
                    "iou": 0.7,
                    "max_det": 300,
                    "half": False,
                    "augment": False,
                    "device": "cpu",
                    "workers": 0,
                    "rect": False,
                    "single_cls": False,
                    "classes": None,
                    "agnostic_nms": False,
                    "overlap_mask": False,
                    "mask_ratio": 1,
                    "mask_processing": "ultralytics.utils.ops.process_mask",
                    "data_contract": dict(INSTANCE_RLE_CONTRACT),
                    "mask_consumer": consumer,
                },
            },
            "semantics": {key: dict(SEMANTICS) for key in metrics},
        },
    }


def artifact(objects, run, attempt, name, role, raw):
    owner = training_run_storage.resolve(run)
    key = owner.key(f"attempts/{attempt.id}/{name}")
    objects.put(
        Bucket=owner.bucket,
        Key=key,
        Body=raw,
        Metadata={"attempt-id": str(attempt.id)},
    )
    return {
        "name": name,
        "role": role,
        "key": key,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
    }


async def seed_run(db, owner, project, dataset, objects, *, segment=True):
    body = TrainingRunCreate(
        name="Deterministic artifact source",
        dataset_id=dataset.id,
        request_id=uuid.uuid4(),
        **(
            {"base_model": "yolo11n-seg", "configuration": configuration()}
            if segment
            else {}
        ),
    )
    config = await training_runs.frozen_config(db, project.id, body)
    run = TrainingRun(
        project_id=project.id,
        creator_id=owner.id,
        dataset_id=dataset.id,
        request_id=uuid.uuid4(),
        request_digest="a" * 64,
        name=body.name,
        status="succeeded",
        config=config,
        config_digest=content_digest(config),
    )
    db.add(run)
    await db.flush()
    await training_run_storage.allocate(db, run)
    attempt = TrainingAttempt(
        run_id=run.id,
        ordinal=1,
        status="succeeded",
        runtime={
            "python": "3.12.3",
            "torch": "2.14.1+cu132",
            "torchvision": "0.29.1+cu132",
            "cuda": "13.2",
            "image_digest": config["image_digest"],
            "frameworks": {"ultralytics": "8.4.177"},
        },
    )
    db.add(attempt)
    await db.flush()
    weights = artifact(
        objects,
        run,
        attempt,
        "best.pt",
        "weights",
        b"deterministic native-shaped weights fixture",
    )
    report = (
        report_for(run, weights)
        if segment
        else {
            "summary": {"metrics/mAP50": 0.3},
            "validation_digest": config["validation_digest"],
        }
    )
    metrics = artifact(
        objects, run, attempt, "metrics.json", "metrics", json.dumps(report).encode()
    )
    attempt.artifacts = {"best.pt": weights, "metrics.json": metrics}
    run.current_attempt_id = attempt.id
    await db.flush()
    return run, attempt, report


@pytest.fixture
async def loop_source(db_session, project_admin, httpx_client, memory_objects):
    owner, token = project_admin
    project = await create_project(db_session, owner_id=owner.id)
    project.status, project.ai_enabled, project.raster_mask_native_editing_enabled = (
        "in_progress",
        True,
        True,
    )
    project.tool_bindings = {
        **build_tool_bindings(["bbox-only"]),
        **build_tool_bindings(["shape"], unit="region"),
    }
    dataset = Dataset(
        display_id="D-LOOP-" + uuid.uuid4().hex[:8],
        name="Original pixels",
        created_by=owner.id,
    )
    db_session.add(dataset)
    await db_session.flush()
    pixels = [1, 1, 1, 0, 0, 1, 0, 1, 0, 0, 1, 1, 1, 0, 1]
    rle = encode_coco_rle(pixels, 5, 3)
    reference = await store_coco_rle(rle)
    tasks, annotations = [], {}
    for index in range(3):
        task = await create_task(db_session, project_id=project.id)
        raw = io.BytesIO()
        Image.new("RGB", (5, 3), (index * 40, 60, 90)).save(raw, format="PNG")
        memory_objects.put(
            Bucket=storage.datasets_bucket, Key=task.file_path, Body=raw.getvalue()
        )
        item = DatasetItem(
            dataset_id=dataset.id,
            file_name=task.file_name,
            file_path=task.file_path,
            file_type="image",
            width=5,
            height=3,
        )
        db_session.add(item)
        await db_session.flush()
        task.dataset_item_id = item.id
        annotation = await AnnotationService(db_session).create(
            task.id,
            owner.id,
            "raster_mask",
            "shape",
            {"type": "raster_mask", "mask": reference},
            tool_unit_id="region",
        )
        task.status, task.reviewed_at, task.is_labeled = (
            "completed",
            datetime.now(timezone.utc),
            True,
        )
        tasks.append(task)
        annotations[str(task.id)] = annotation
    await db_session.flush()
    parent = await freeze(httpx_client, token, db_session, project)
    run, attempt, report = await seed_run(
        db_session, owner, project, parent, memory_objects
    )
    return SimpleNamespace(
        project=project,
        parent=parent,
        run=run,
        attempt=attempt,
        report=report,
        tasks=tasks,
        annotations=annotations,
        rle=rle,
        pixels=pixels,
        owner=owner,
        token=token,
        objects=memory_objects,
    )


async def register(client, source, *, bindings=None):
    response = await client.post(
        f"/api/v1/projects/{source.project.id}/trained-models",
        headers=auth(source.token),
        json={
            "request_id": str(uuid.uuid4()),
            "name": "Loop candidate",
            "run_id": str(source.run.id),
            "attempt_id": str(source.attempt.id),
            "bindings": bindings or ["shape"],
        },
    )
    return response


def prediction(ref, task_id, rle):
    reference = build_rle_reference(rle)
    return PredictionResult(
        task_id=str(task_id),
        score=0.8,
        result=[
            {
                "tool_unit_id": "region",
                "class_name": ref["classes"][0],
                "native_class_index": 0,
                "instance_index": 0,
                "score": 0.8,
                "confidence": 0.8,
                "geometry": {"type": "raster_mask", "mask": reference},
            }
        ],
        meta={
            "trained_model": {
                key: ref[key]
                for key in ("model_version_id", "sha256", "classes_digest")
            },
            "mask_objects": {reference["sha256"]: rle},
            "segmentation": {
                "output_contract": ref["output_contract"],
                "dimensions": rle["size"],
                "runtime_qualification": SEGMENTATION_RUNTIME,
                "mask_processing": SEGMENTATION_RUNTIME["mask_processing"],
                "decoder": SEGMENTATION_RUNTIME["decoder"],
            },
        },
    )


async def test_mask_release_human_review_second_freeze_and_learning_baseline(
    db_session,
    httpx_client,
    loop_source,
    reviewer,
    monkeypatch,
):
    s = loop_source
    registered = await register(httpx_client, s)
    assert registered.status_code == 201, registered.text
    version = await db_session.get(
        TrainedModelVersion, uuid.UUID(registered.json()["id"])
    )
    assert registered.json()["task"] == "segment"
    backend, pool = await create_registry_with_pool(
        db_session,
        name="mask-loop-" + uuid.uuid4().hex[:6],
        enabled_pool=True,
        auth_method="token",
        auth_token="x" * 40,
    )
    db_session.add(
        ProjectMLBackendPool(project_id=s.project.id, pool_id=pool.id, enabled=True)
    )
    await db_session.flush()
    # Routing/HTTP availability are external; trial validation and storage remain real.
    monkeypatch.setattr(trained_models, "assert_routable_backend", AsyncMock())
    receipt = trained_models.expected_receipt(version)
    monkeypatch.setattr(
        MLBackendClient, "install_trained_model", AsyncMock(return_value=receipt)
    )
    monkeypatch.setattr(
        MLBackendClient, "trained_model_receipt", AsyncMock(return_value=receipt)
    )
    val_sample = next(
        row for row in s.parent.manifest["samples"] if row["split"] == "val"
    )
    wrong = prediction(receipt, val_sample["task_id"], encode_coco_rle([1] * 15, 3, 5))
    predict = AsyncMock(return_value=[wrong])
    monkeypatch.setattr(MLBackendClient, "predict", predict)
    deploy_path = f"/api/v1/projects/{s.project.id}/trained-models/{version.id}/deploy"
    response = await httpx_client.post(
        deploy_path, headers=auth(s.token), json={"registry_id": str(backend.id)}
    )
    assert response.status_code == 200 and response.json()["status"] == "failed", (
        response.text
    )
    predict.return_value = [prediction(receipt, val_sample["task_id"], s.rle)]
    deployed = await httpx_client.post(
        deploy_path, headers=auth(s.token), json={"registry_id": str(backend.id)}
    )
    assert deployed.status_code == 200 and deployed.json()["status"] == "ready", (
        deployed.text
    )
    deployment = deployed.json()
    trial = deployment["trial"]
    assert trial["task_id"] == val_sample["task_id"] and trial["segmentation"][
        "dimensions"
    ] == [3, 5]
    assert trial["result"][0]["tool_unit_id"] == "region"
    assert "counts" not in trial["result"][0]["geometry"]["mask"]
    content = await httpx_client.get(
        f"/api/v1/projects/{s.project.id}/trained-models/{version.id}/deployments/{deployment['id']}/mask-content/0",
        headers=auth(s.token),
    )
    assert (
        content.status_code == 200 and list(decode_coco_rle(content.json())) == s.pixels
    )
    release_body = {
        "request_id": str(uuid.uuid4()),
        "expected_generation": 0,
        "task": "segment",
        "model_version_id": str(version.id),
        "deployment_id": deployment["id"],
        "sample_confirmed": True,
    }
    releases = f"/api/v1/projects/{s.project.id}/model-releases"
    denied = await httpx_client.post(
        releases, headers=auth(s.token), json={**release_body, "task": "detect"}
    )
    assert denied.status_code == 422
    published = await httpx_client.post(
        releases, headers=auth(s.token), json=release_body
    )
    assert published.status_code == 201, published.text
    ref = published.json()["snapshot"]
    assert ref["task"] == "segmentation" and ref["release_generation"] == 1
    assert (
        await trained_models.validate_frozen_reference(db_session, s.project.id, ref)
        == backend.id
    )

    # Reconcile an actually retained successful source run into the task baseline.
    policy_path = f"/api/v1/projects/{s.project.id}/learning-policy"
    saved = await httpx_client.put(
        policy_path,
        headers=auth(s.token),
        json={
            "expected_revision": 0,
            "task": "segment",
            "enabled": True,
            "parent_id": str(s.parent.id),
            "base_model": "yolo11n-seg",
            "configuration": configuration(),
            "min_changed_samples": 1,
            "check_interval_minutes": 1,
            "cooldown_minutes": 0,
        },
    )
    assert saved.status_code == 200, saved.text
    policy = await db_session.get(LearningPolicy, uuid.UUID(saved.json()["id"]))
    trigger = LearningTrigger(
        policy_id=policy.id,
        project_id=s.project.id,
        policy_revision=policy.revision,
        dataset_id=s.parent.id,
        source_digest=s.parent.snapshot_digest,
        status="running",
    )
    db_session.add(trigger)
    await db_session.flush()
    s.run.auto_trigger_id = trigger.id
    await db_session.flush()
    await learning_policies.check_policy(db_session, policy.id, force=True)
    assert policy.baseline_dataset_id == s.parent.id
    assert policy.baseline == semantic_partitions(s.parent.manifest)
    assert policy.config["bridge"] == INSTANCE_RLE_CONTRACT["bridge"]

    train_sample = next(
        row for row in s.parent.manifest["samples"] if row["split"] == "train"
    )
    annotation = s.annotations[train_sample["task_id"]]
    previous_version = annotation.version
    await AnnotationService(db_session).update(
        annotation.id, attributes={"human_note": "same pixels"}
    )
    noise = await freeze(
        httpx_client,
        s.token,
        db_session,
        s.project,
        parent=s.parent,
        name="Version noise",
    )
    assert annotation.version > previous_version
    noise_sample = next(
        row
        for row in noise.manifest["samples"]
        if row["task_id"] == train_sample["task_id"]
    )
    assert noise_sample["label_sha256"] != train_sample["label_sha256"]
    assert (
        noise_sample["semantic_label_sha256"] == train_sample["semantic_label_sha256"]
    )
    assert (
        learning_policies.growth(semantic_partitions(noise.manifest), policy.baseline)
        == 0
    )

    task = next(task for task in s.tasks if str(task.id) == train_sample["task_id"])
    reopened = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/reopen", headers=auth(s.token)
    )
    assert reopened.status_code == 200, reopened.text
    await AnnotationService(db_session).delete(annotation.id)
    changed_pixels = list(s.pixels)
    changed_pixels[1] = 0
    result = prediction(ref, task.id, encode_coco_rle(changed_pixels, 5, 3))
    trained_models.validate_and_map_results([result], ref)
    persisted = await PredictionService(db_session).create_from_ml_result(
        task.id,
        s.project.id,
        backend.id,
        result.result,
        token_meta=result.meta,
        model_provenance=ref,
    )
    meta = await db_session.scalar(
        select(PredictionMeta).where(PredictionMeta.prediction_id == persisted.id)
    )
    assert meta.extra["trained_model"] == ref
    accepted = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/predictions/{persisted.id}/accept",
        headers=auth(s.token),
    )
    assert accepted.status_code == 200, accepted.text
    accepted_id = accepted.json()[0]["id"]
    assert accepted.json()[0]["tool_unit_id"] == "region"
    assert accepted.json()[0]["parent_prediction_id"] == str(persisted.id)
    submitted = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/submit", headers=auth(s.token)
    )
    assert submitted.status_code == 200, submitted.text
    review_user, review_token = reviewer
    await create_membership(
        db_session, project_id=s.project.id, user_id=review_user.id, role="reviewer"
    )
    # Unbatched tasks require explicit reviewer assignment before they are visible.
    task.reviewer_id, task.reviewer_is_override = review_user.id, True
    await db_session.flush()
    for action in ("claim", "approve"):
        reviewed = await httpx_client.post(
            f"/api/v1/tasks/{task.id}/review/{action}", headers=auth(review_token)
        )
        assert reviewed.status_code == 200, reviewed.text
    second = await freeze(
        httpx_client,
        s.token,
        db_session,
        s.project,
        parent=s.parent,
        name="Human accepted round two",
    )
    frozen_task = next(
        row for row in second.snapshot["samples"] if row["task_id"] == str(task.id)
    )
    assert [row["id"] for row in frozen_task["annotations"]] == [accepted_id]
    assert frozen_task["eligibility"]["reviewed"] is True
    independent_key = (
        f"projects/{s.project.id}/datasets/{second.id}/masks/{task.id}.json"
    )
    independent = json.loads(
        s.objects.values[second.storage_ref["bucket"], independent_key][0]
    )
    assert independent["instances"][0]["annotation_id"] == accepted_id
    assert list(decode_coco_rle(independent["instances"][0]["rle"])) == changed_pixels
    assert (
        learning_policies.growth(semantic_partitions(second.manifest), policy.baseline)
        == 1
    )
    assert {row["task_id"]: row["split"] for row in second.manifest["samples"]} == {
        row["task_id"]: row["split"] for row in s.parent.manifest["samples"]
    }
    # A stale baseline of another bridge is rejected before any candidate build.
    saved_parent = copy.deepcopy(s.parent.manifest)
    s.parent.manifest = {**saved_parent, **SEGMENTATION_CONTRACT}
    await db_session.flush()
    await learning_policies.check_policy(db_session, policy.id, force=True)
    assert policy.state == "suspended" and not policy.enabled


async def test_registration_report_identity_and_task_specific_initialization_serialization(
    db_session,
    httpx_client,
    loop_source,
):
    s = loop_source
    wrong_binding = await register(httpx_client, s, bindings=["bbox-only"])
    assert wrong_binding.status_code == 422
    valid_report = copy.deepcopy(s.report)
    for field in ("weights_sha256", "mask_ratio", "missing_comparison"):
        report = copy.deepcopy(valid_report)
        if field == "weights_sha256":
            report["comparison"]["candidate"][field] = "f" * 64
        elif field == "missing_comparison":
            report.pop("comparison")
        else:
            report["comparison"]["evaluator"]["options"][field] = 4
        metrics = artifact(
            s.objects,
            s.run,
            s.attempt,
            "metrics.json",
            "metrics",
            json.dumps(report).encode(),
        )
        s.attempt.artifacts = {**s.attempt.artifacts, "metrics.json": metrics}
        await db_session.flush()
        rejected = await register(httpx_client, s)
        assert rejected.status_code == 422, rejected.text
    metrics = artifact(
        s.objects,
        s.run,
        s.attempt,
        "metrics.json",
        "metrics",
        json.dumps(valid_report).encode(),
    )
    s.attempt.artifacts = {**s.attempt.artifacts, "metrics.json": metrics}
    await db_session.flush()
    registered = await register(httpx_client, s)
    assert registered.status_code == 201, registered.text
    model = registered.json()
    assert model["evaluation"]["comparison_identity_status"] == "verified"
    response = await httpx_client.get(
        f"/api/v1/projects/{s.project.id}/training-runs/initialization-sources/{model['id']}",
        headers=auth(s.token),
        params={
            "framework": "ultralytics",
            "base_model": "yolo11n-seg",
            "dataset_id": str(s.parent.id),
        },
    )
    assert response.status_code == 200 and response.json()["eligible"], response.text
    pin = response.json()["pin"]
    assert set(pin) == PIN_KEYS | {"data_contract"}
    assert (
        pin["task"] == "segment" and pin["policy_digest"] == SEGMENTATION_POLICY_DIGEST
    )
    assert pin["data_contract"] == INSTANCE_RLE_CONTRACT
    assert pin["evaluation"]["report_digest"] == content_digest(valid_report)
    assert pin["evaluation"]["normalized_model_digest"] == content_digest(
        model["evaluation"]
    )
    assert (
        pin["evaluation"]["report_digest"]
        != pin["evaluation"]["normalized_model_digest"]
    )
    assert pin["evaluation"]["descriptor_digest"] == content_digest(
        valid_report["comparison"]
    )
    assert pin["evaluation"]["weights_sha256"] == model["artifact"]["sha256"]
    project, dataset = await ready_dataset(db_session, s.owner)
    run, attempt, _ = await seed_run(
        db_session, s.owner, project, dataset, s.objects, segment=False
    )
    detect = await register(
        httpx_client,
        SimpleNamespace(project=project, token=s.token, run=run, attempt=attempt),
        bindings=["car"],
    )
    assert detect.status_code == 201, detect.text
    legacy = await httpx_client.get(
        f"/api/v1/projects/{project.id}/training-runs/initialization-sources/{detect.json()['id']}",
        headers=auth(s.token),
        params={
            "framework": "ultralytics",
            "base_model": "yolo11n",
            "dataset_id": str(dataset.id),
        },
    )
    assert legacy.status_code == 200 and legacy.json()["eligible"], legacy.text
    old_pin = legacy.json()["pin"]
    assert set(old_pin) == PIN_KEYS and old_pin["policy_digest"] == POLICY_DIGEST
    assert set(old_pin["evaluation"]) == {
        "report_sha256",
        "report_digest",
        "validation_digest",
    }
