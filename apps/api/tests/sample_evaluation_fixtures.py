"""Small real frozen inputs and an in-memory verified object store for tests."""

import copy
import hashlib
import io
import uuid
from types import SimpleNamespace

from botocore.exceptions import ClientError
from PIL import Image

from app.db.models.sample_evaluation import SampleEvaluationAttempt as Attempt
from app.db.models.trained_model import TrainedModelVersion
from app.db.models.training_dataset import TrainingDatasetVersion
from app.schemas.sample_evaluation import SampleEvaluationCreate
from app.services import sample_evaluations as service
from aap_inference.backend_transport import PredictionResult
from app.services.training_dataset_bridge import content_digest
from tests.test_trained_models import ready_deployment, seed_source
from tests.test_training_dataset_analysis import inputs
from tests.trained_model_reference_fixtures import build_reference


class Body(io.BytesIO):
    def iter_chunks(self, chunk_size):
        while chunk := self.read(chunk_size):
            yield chunk


class Objects:
    def __init__(self):
        self.objects = {}
        self.deleted = []

    def put_object(
        self, *, Bucket, Key, Body, Metadata=None, ContentType="application/json", **_
    ):
        raw = Body if isinstance(Body, bytes) else Body.read()
        self.objects[Key] = {
            "raw": raw,
            "Metadata": Metadata or {},
            "ContentType": ContentType,
            "ETag": '"' + hashlib.md5(raw).hexdigest() + '"',
        }

    def head_object(self, *, Key, **_):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "HeadObject")
        value = self.objects[Key]
        return {k: copy.deepcopy(v) for k, v in value.items() if k != "raw"} | {
            "ContentLength": len(value["raw"])
        }

    def get_object(self, *, Key, IfMatch=None, **_):
        head = self.head_object(Key=Key)
        if IfMatch and head["ETag"] != IfMatch:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "GetObject")
        return {**head, "Body": Body(self.objects[Key]["raw"])}

    def delete_object(self, *, Key, **_):
        self.deleted.append(Key)
        self.objects.pop(Key, None)

    def delete_prefix(self, prefix, **_):
        for key in list(self.objects):
            if key.startswith(prefix):
                self.delete_object(Key=key)


def profile():
    value = {
        "schema": 1,
        "implementation": "aap-ultralytics-detection",
        "revision": 1,
        "ultralytics_version": "8.4.177",
        "torch_version": "2.14.1+cu132",
        "pillow_version": "12.2.0",
        "source_digest": "d" * 64,
        "storage_identity": str(uuid.uuid4()),
        "effective_device": "cpu",
        "options": {
            "imgsz": 640,
            "rect": False,
            "quantize": 32,
            "augment": False,
            "agnostic_nms": False,
            "max_det": 100,
            "batch": 1,
            "classes": None,
            "compile": False,
            "channels_last": False,
            "dnn": False,
            "nms": None,
            "save": False,
            "save_txt": False,
            "save_crop": False,
            "save_conf": False,
            "show": False,
            "visualize": False,
            "embed": None,
            "verbose": False,
            "stream": False,
        },
    }
    return {**value, "profile_digest": content_digest(value)}


async def seed(db, owner, objects, *, count=2):
    project, native_run, native_attempt = await seed_source(db, owner)
    dataset = await db.get(TrainingDatasetVersion, native_run.dataset_id)
    snapshot, manifest = inputs(
        [
            {
                "task": str(uuid.uuid4()),
                "content": str(uuid.uuid4()),
                "split": "val",
                "labels": ["car"] if i % 2 == 0 else [],
            }
            for i in range(count)
        ],
        classes=["car", "person"],
    )
    raw_images = []
    for i, member in enumerate(manifest["samples"]):
        output = io.BytesIO()
        Image.new("RGB", (16, 12), (i * 20 + 10, 30, 70)).save(output, format="PNG")
        raw = output.getvalue()
        raw_images.append(raw)
        member.update(sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw))
    manifest["validation_digest"] = content_digest(manifest["samples"])
    manifest["content_digest"] = content_digest(
        {k: v for k, v in manifest.items() if k != "content_digest"}
    )
    dataset.snapshot, dataset.snapshot_digest, dataset.manifest = (
        snapshot,
        content_digest(snapshot),
        manifest,
    )
    model = TrainedModelVersion(
        project_id=project.id,
        creator_id=owner.id,
        run_id=native_run.id,
        attempt_id=native_attempt.id,
        request_id=uuid.uuid4(),
        request_digest="1" * 64,
        name="Candidate",
        framework="ultralytics",
        framework_version="8.4.177",
        artifact={"sha256": "2" * 64, "size_bytes": 100, "key": "owned/best.pt"},
        classes=["person", "car"],
        bindings=["person", "car"],
        binding_digest=content_digest(
            {"classes": ["person", "car"], "bindings": ["person", "car"]}
        ),
        provenance={
            "dataset_id": str(dataset.id),
            "run_id": str(native_run.id),
            "attempt_id": str(native_attempt.id),
        },
    )
    db.add(model)
    await db.flush()
    backend, deployment = await ready_deployment(db, project, model)
    ref = build_reference(model, deployment)
    qualified = profile()
    body = SampleEvaluationCreate(
        request_id=uuid.uuid4(),
        name="Sample evaluation",
        model_version_id=model.id,
        deployment_id=deployment.id,
        dataset_id=dataset.id,
        sample_task_ids=[uuid.UUID(m["task_id"]) for m in manifest["samples"]],
    )
    for member, raw in zip(manifest["samples"], raw_images, strict=True):
        objects.put_object(
            Bucket=service.storage.training_bucket,
            Key=f"projects/{project.id}/datasets/{dataset.id}/media/{member['task_id']}",
            Body=raw,
            Metadata={
                "snapshot-digest": dataset.snapshot_digest,
                "sha256": member["sha256"],
            },
            ContentType="image/png",
        )
    await db.flush()
    return SimpleNamespace(
        project=project,
        dataset=dataset,
        model=model,
        deployment=deployment,
        backend=backend,
        ref=ref,
        profile=qualified,
        body=body,
        raw_images=raw_images,
    )


async def evaluation(db, owner, world):
    body = world.body.model_copy(
        update={"name": "Evaluation", "request_id": uuid.uuid4()}
    )
    identity = uuid.uuid4()
    spec = await service.frozen_spec(
        db, world.project, body, identity, world.ref, world.profile
    )
    run = await service.create_run(
        db,
        world.project,
        owner,
        body,
        evaluation_id=identity,
        request_digest=content_digest(body.model_dump(mode="json")),
        spec=spec,
    )
    await db.flush()
    return run, await db.get(Attempt, run.current_attempt_id)


def prediction(run, row, *, boxes=None):
    frozen = run.spec["samples"][row.ordinal]
    request = row.request["evaluation"]
    inference = {
        **request,
        "width": frozen["width"],
        "height": frozen["height"],
        "effective_device": run.spec["profile"]["effective_device"],
        "options": run.spec["profile"]["options"],
        "conf": run.spec["confidence"],
        "nms_iou": run.spec["nms_iou"],
        "end2end": False,
    }
    return PredictionResult(
        task_id=frozen["task_id"],
        result=[] if boxes is None else boxes,
        meta={
            "trained_model": {
                k: run.spec["model_ref"][k]
                for k in ("model_version_id", "sha256", "classes_digest")
            },
            "sample_evaluation": inference,
        },
    )


async def complete_rows(db, run, attempt, world):
    for row in await service.sample_rows(db, run):
        _png, preprocessing = service.prepare_input(
            run, world.dataset, run.spec["samples"][row.ordinal]
        )
        row.request = service.request_identity(run.spec, row, preprocessing)
        row.evidence = service.successful_evidence(
            run, attempt, row, prediction(run, row)
        )
        row.status, row.produced_attempt_id = "succeeded", attempt.id
    await db.flush()
