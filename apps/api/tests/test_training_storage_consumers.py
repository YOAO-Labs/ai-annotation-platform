"""Frozen dataset consumers keep their pinned bucket when the default drifts."""

from contextlib import asynccontextmanager
from copy import deepcopy
import hashlib
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from botocore.response import StreamingBody
from botocore.exceptions import ClientError
from fastapi import HTTPException
from PIL import Image
import pytest

from app.config import settings
from app.db.models.training_dataset import TrainingDatasetVersion
from app.services import training_dataset_analysis as analysis
from app.services import training_dataset_storage as owners
from app.services import training_previews, training_runs, trained_models
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.managed_original_retirement import managed_storage_identity
from app.services.storage import storage_service
from app.services.training_dataset_bridge import TrainingDatasetError
from app.workers import training_dataset as worker
from training_contract.dataset import instance_rle_evidence


class Objects:
    def __init__(self, endpoint):
        self.meta = SimpleNamespace(endpoint_url=endpoint)
        self.objects = {}
        self.metadata = {}
        self.calls = []

    def head_object(self, *, Bucket, Key):
        self.calls.append(("head", Bucket, Key))
        if (Bucket, Key) not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "HeadObject")
        data = self.objects[Bucket, Key]
        return {
            "ContentLength": len(data),
            "Metadata": self.metadata.get((Bucket, Key), {}),
            "ETag": '"' + hashlib.md5(data).hexdigest() + '"',
            "ContentType": "image/png",
        }

    def get_object(self, *, Bucket, Key, **kwargs):
        self.calls.append(("get", Bucket, Key))
        data = self.objects[Bucket, Key]
        return {"Body": StreamingBody(io.BytesIO(data), len(data))}

    def upload_file(self, filename, bucket, key, **kwargs):
        from pathlib import Path

        self.calls.append(("upload", bucket, key))
        self.objects[bucket, key] = Path(filename).read_bytes()

    def generate_presigned_url(self, operation, *, Params, ExpiresIn):
        self.calls.append(("sign", Params["Bucket"], Params["Key"]))
        return f"https://owned-storage/{Params['Bucket']}/{Params['Key']}"


def image_bytes(color):
    content = io.BytesIO()
    Image.new("RGB", (8, 6), color).save(content, format="PNG")
    return content.getvalue()


@pytest.fixture
def frozen(monkeypatch):
    project_id, version_id, task_id = uuid.uuid4(), uuid.uuid4(), str(uuid.uuid4())
    endpoint = (
        f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}"
    )
    client = Objects(endpoint)
    bucket = "retained-version-bucket"
    ref = {
        "schema": 1,
        "purpose": "training-dataset",
        "location_id": str(LEGACY_LOCATION_ID),
        "config_revision": 1,
        "binding_generation": 1,
        "endpoint_url": endpoint,
        "bucket": bucket,
        "prefix": owners.prefix_for(project_id, version_id),
        "storage_identity": managed_storage_identity(endpoint, bucket),
        "origin": "new",
        "evidence": None,
    }
    version = SimpleNamespace(
        id=version_id,
        project_id=project_id,
        storage_ref=ref,
        snapshot_digest="d" * 64,
        package_sha256="e" * 64,
        manifest={"samples": [{"task_id": task_id, "split": "val"}]},
    )
    version.package_key = (
        ref["prefix"] + "attempts/" + str(uuid.uuid4()) + "/dataset.zip"
    )
    raw = image_bytes("red")
    sample = SimpleNamespace(
        task_id=task_id,
        sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw),
        width=8,
        height=6,
        included=True,
        label_sha256=None,
    )
    key = ref["prefix"] + "media/" + task_id
    client.objects[bucket, key] = raw
    client.objects["new-current-bucket", key] = image_bytes("blue")
    client.metadata[bucket, key] = {
        "snapshot-digest": version.snapshot_digest,
        "sha256": sample.sha256,
    }
    monkeypatch.setattr(storage_service, "client", client)
    monkeypatch.setattr(storage_service, "training_bucket", "new-current-bucket")
    return SimpleNamespace(
        version=version, sample=sample, client=client, raw=raw, bucket=bucket, key=key
    )


def source_sample(frozen):
    return {
        "task_id": frozen.sample.task_id,
        "source_key": "must-not-reread-source",
        "source_bucket": "source-bucket",
        "media": {"etag": "original", "size_bytes": len(frozen.raw)},
    }


def test_materialize_image_requires_an_explicit_retained_bucket(frozen, tmp_path):
    result = worker.materialize_image(
        source_sample(frozen),
        frozen.version.storage_ref["prefix"].rstrip("/"),
        frozen.version.snapshot_digest,
        tmp_path,
        bucket=frozen.bucket,
    )
    assert result["sha256"] == frozen.sample.sha256
    assert {call[1] for call in frozen.client.calls} == {frozen.bucket}


@pytest.mark.parametrize("raster_contract", [False, True])
def test_new_retained_media_and_mask_writes_do_not_follow_current_bucket(
    frozen, tmp_path, raster_contract
):
    del frozen.client.objects[frozen.bucket, frozen.key]
    sample = {
        **source_sample(frozen),
        "raster_contract": raster_contract,
        "classes": ["shape"],
        "annotations": [],
    }
    frozen.client.objects[sample["source_bucket"], sample["source_key"]] = frozen.raw
    result = worker.materialize_image(
        sample,
        frozen.version.storage_ref["prefix"].rstrip("/"),
        frozen.version.snapshot_digest,
        tmp_path,
        bucket=frozen.bucket,
    )
    assert result["sha256"] == frozen.sample.sha256
    assert frozen.client.objects[frozen.bucket, frozen.key] == frozen.raw
    assert frozen.client.objects["new-current-bucket", frozen.key] != frozen.raw
    writes = [call for call in frozen.client.calls if call[0] == "upload"]
    assert len(writes) == (2 if raster_contract else 1)
    assert {call[1] for call in writes} == {frozen.bucket}
    if raster_contract:
        mask_key = (
            frozen.version.storage_ref["prefix"] + f"masks/{frozen.sample.task_id}.json"
        )
        assert (
            json.loads(frozen.client.objects[frozen.bucket, mask_key])["instances"]
            == []
        )


async def test_materialize_sample_uses_the_version_owner(frozen, tmp_path):
    result = await worker.materialize_sample(
        None,
        frozen.version,
        source_sample(frozen),
        frozen.version.storage_ref["prefix"].rstrip("/"),
        frozen.version.snapshot_digest,
        tmp_path,
        None,
    )
    assert result["sha256"] == frozen.sample.sha256
    assert {call[1] for call in frozen.client.calls} == {frozen.bucket}


@pytest.mark.parametrize("invalid", ["missing-pin", "other-prefix"])
async def test_materialization_rejects_unresolved_ownership_before_io(
    frozen, tmp_path, invalid
):
    prefix = frozen.version.storage_ref["prefix"].rstrip("/")
    if invalid == "missing-pin":
        frozen.version.storage_ref = None
    else:
        prefix += "/other"
    with pytest.raises(TrainingDatasetError):
        await worker.materialize_sample(
            None,
            frozen.version,
            source_sample(frozen),
            prefix,
            frozen.version.snapshot_digest,
            tmp_path,
            None,
        )
    assert frozen.client.calls == []


def test_analysis_reads_and_verifies_only_the_pinned_media(frozen):
    source, content_type = analysis.read_media(
        frozen.version.project_id, frozen.version, frozen.sample
    )
    try:
        assert source.read() == frozen.raw
        assert content_type == "image/png"
    finally:
        source.close()
    assert {call[1] for call in frozen.client.calls} == {frozen.bucket}


def test_analysis_mask_read_keeps_the_dataset_bucket(frozen):
    annotation_id = str(uuid.uuid4())
    rle = {"encoding": "coco_rle", "size": [6, 8], "counts": [0, 48]}
    evidence = instance_rle_evidence(rle)
    payload = json.dumps(
        {
            "instances": [
                {
                    "annotation_id": annotation_id,
                    "annotation_version": 3,
                    "rle": rle,
                    **evidence,
                }
            ]
        }
    ).encode()
    key = (
        frozen.version.storage_ref["prefix"]
        + "masks/"
        + frozen.sample.task_id
        + ".json"
    )
    frozen.client.objects[frozen.bucket, key] = payload
    frozen.client.objects["new-current-bucket", key] = b"same-key trap"
    frozen.sample.label_sha256 = hashlib.sha256(payload).hexdigest()
    result = analysis.read_instance_mask(
        frozen.version.project_id, frozen.version, frozen.sample, annotation_id
    )
    assert result["annotation_version"] == 3
    assert result["pixel_sha256"] == evidence["pixel_sha256"]
    assert {call[1] for call in frozen.client.calls} == {frozen.bucket}


@pytest.mark.parametrize("invalid", ["missing-pin", "other-project", "other-endpoint"])
def test_media_identity_failure_never_reads_the_current_bucket(frozen, invalid):
    project = frozen.version.project_id
    if invalid == "missing-pin":
        frozen.version.storage_ref = None
    elif invalid == "other-project":
        project = uuid.uuid4()
    else:
        frozen.client.meta.endpoint_url = "https://another-origin.invalid"
    with pytest.raises(TrainingDatasetError):
        analysis.read_media(project, frozen.version, frozen.sample)
    assert frozen.client.calls == []


@pytest.mark.parametrize("extra", [None, "resume", "initialization"])
async def test_run_assignment_pins_dataset_but_keeps_other_artifact_owners(
    frozen, extra
):
    run = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=frozen.version.project_id,
        dataset_id=frozen.version.id,
        config={},
        config_digest="f" * 64,
    )
    attempt = SimpleNamespace(id=uuid.uuid4(), native_budget=None, resume_from_id=None)
    checkpoint_key = "projects/run/checkpoint.pt"
    parent = SimpleNamespace(
        artifacts={"checkpoint": {"role": "checkpoint", "key": checkpoint_key}}
    )
    if extra == "resume":
        attempt.resume_from_id = uuid.uuid4()
    elif extra == "initialization":
        run.config["initialization"] = {
            "artifact": {"key": checkpoint_key},
            "source_digest": "a" * 64,
        }

    async def get(model, identifier):
        return frozen.version if model is TrainingDatasetVersion else parent

    result = await training_runs.assignment(
        SimpleNamespace(get=get), run, attempt, "token"
    )
    assert result["dataset_url"].startswith(f"https://owned-storage/{frozen.bucket}/")
    assert frozen.client.calls[0][1:] == (frozen.bucket, frozen.version.package_key)
    if extra:
        assert frozen.client.calls[1][1:] == ("new-current-bucket", checkpoint_key)


@pytest.mark.parametrize("invalid", ["missing-pin", "foreign-package", "other-project"])
async def test_run_assignment_does_not_sign_an_unresolved_dataset(frozen, invalid):
    run = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=frozen.version.project_id,
        dataset_id=frozen.version.id,
    )
    if invalid == "missing-pin":
        frozen.version.storage_ref = None
    elif invalid == "foreign-package":
        frozen.version.package_key = "another-version/dataset.zip"
    else:
        run.project_id = uuid.uuid4()
    db = SimpleNamespace(get=AsyncMock(return_value=frozen.version))
    with pytest.raises(HTTPException) as error:
        await training_runs.assignment(db, run, SimpleNamespace(), "token")
    assert error.value.status_code == 409
    assert frozen.client.calls == []


def preview_for(frozen):
    return SimpleNamespace(
        id=uuid.uuid4(),
        project_id=frozen.version.project_id,
        dataset_id=frozen.version.id,
        claim_id=uuid.uuid4(),
        input_digest="b" * 64,
        spec={"sources": [{"task_id": frozen.sample.task_id, "media_key": frozen.key}]},
    )


async def test_preview_assignment_resolves_owner_without_rewriting_spec(frozen):
    preview = preview_for(frozen)
    original_spec = deepcopy(preview.spec)
    db = SimpleNamespace(get=AsyncMock(return_value=frozen.version))
    result = await training_previews.assignment(db, preview)
    assert result["source_urls"][frozen.sample.task_id].startswith(
        f"https://owned-storage/{frozen.bucket}/"
    )
    assert preview.spec == original_spec and preview.input_digest == "b" * 64
    assert {call[1] for call in frozen.client.calls} == {frozen.bucket}


@pytest.mark.parametrize(
    "invalid", ["missing-pin", "other-project", "foreign-source", "wrong-kind"]
)
async def test_preview_cannot_sign_a_source_outside_its_frozen_sample(frozen, invalid):
    preview = preview_for(frozen)
    if invalid == "missing-pin":
        frozen.version.storage_ref = None
    elif invalid == "other-project":
        preview.project_id = uuid.uuid4()
    elif invalid == "foreign-source":
        preview.spec["sources"][0]["media_key"] = "another-version/media/image"
    else:
        preview.spec["sources"][0]["media_key"] = frozen.version.package_key
    db = SimpleNamespace(get=AsyncMock(return_value=frozen.version))
    with pytest.raises(HTTPException) as error:
        await training_previews.assignment(db, preview)
    assert error.value.status_code == 409
    assert frozen.client.calls == []


async def test_preview_stop_does_not_resolve_missing_dataset_storage(frozen):
    preview = preview_for(frozen)
    db = SimpleNamespace(
        get=AsyncMock(side_effect=AssertionError("stop must not load"))
    )
    result = await training_previews.assignment(db, preview, stop=True)
    assert result["stop"] and result["source_urls"] == {}
    db.get.assert_not_awaited()
    assert frozen.client.calls == []


def test_model_trial_sample_uses_the_dataset_owner(frozen):
    owner, key = trained_models.frozen_sample_location(
        frozen.version.project_id, frozen.version, frozen.sample.task_id
    )
    assert owner.bucket == frozen.bucket and key == frozen.key
    assert frozen.client.calls == []


@pytest.mark.parametrize("invalid", ["missing-pin", "other-project", "invalid-key"])
def test_model_trial_sample_refuses_unknown_or_foreign_storage(frozen, invalid):
    project, task_id = frozen.version.project_id, frozen.sample.task_id
    if invalid == "missing-pin":
        frozen.version.storage_ref = None
    elif invalid == "other-project":
        project = uuid.uuid4()
    else:
        task_id = "../../foreign"
    with pytest.raises(HTTPException) as error:
        trained_models.frozen_sample_location(project, frozen.version, task_id)
    assert error.value.status_code == 409
    assert frozen.client.calls == []


async def test_model_install_keeps_weights_separate_from_the_trial_input(
    frozen, monkeypatch
):
    project = SimpleNamespace(id=frozen.version.project_id)
    model = SimpleNamespace(
        provenance={
            "dataset_id": str(frozen.version.id),
            "validation_digest": "c" * 64,
        },
        artifact={"key": "projects/run/weights.pt", "size_bytes": 12},
    )
    payloads = []

    async def install(payload):
        payloads.append(payload)
        return {}

    async def predict(payload, **kwargs):
        payloads.append(payload)
        return [
            SimpleNamespace(
                task_id=frozen.sample.task_id, inference_time_ms=1, result=[], score=0.5
            )
        ]

    monkeypatch.setattr(
        trained_models, "enabled_backend", AsyncMock(return_value=object())
    )
    monkeypatch.setattr(trained_models, "assert_routable_backend", AsyncMock())
    monkeypatch.setattr(
        trained_models,
        "MLBackendClient",
        lambda _: SimpleNamespace(install_trained_model=install, predict=predict),
    )
    monkeypatch.setattr(trained_models, "expected_receipt", lambda _: {})
    monkeypatch.setattr(trained_models, "check_receipt", lambda *args: None)
    monkeypatch.setattr(trained_models, "frozen_reference", lambda *args, **kwargs: {})
    monkeypatch.setattr(trained_models, "wire_context", lambda *args: {})
    monkeypatch.setattr(trained_models, "validate_and_map_results", lambda *args: None)
    monkeypatch.setattr(trained_models, "model_task", lambda _: "detect")
    monkeypatch.setattr(storage_service, "rewrite_host_for_ml_backend", lambda url: url)
    db = SimpleNamespace(get=AsyncMock(return_value=frozen.version))
    await trained_models.install_and_trial(
        db, project, model, SimpleNamespace(registry_id=uuid.uuid4())
    )
    assert payloads[0]["download_url"].startswith(
        "https://owned-storage/new-current-bucket/"
    )
    assert payloads[1][0]["file_path"].startswith(
        f"https://owned-storage/{frozen.bucket}/"
    )


@pytest.mark.parametrize("outcome", ["ready", "failed", "published-ack-lost"])
async def test_worker_package_and_attempt_cleanup_keep_the_saved_owner(
    frozen, monkeypatch, outcome
):
    version = frozen.version
    version.snapshot = {"samples": []}
    version.parent_id = None
    version.status = "pending"
    version.async_job_id, version.attempt_id = uuid.uuid4(), uuid.uuid4()
    version.package_key = None
    published_key = None
    deletions = []

    class Session:
        async def get(self, model, identifier):
            return version

        async def execute(self, statement):
            return SimpleNamespace(
                scalar_one=lambda: version, scalar_one_or_none=lambda: version
            )

        async def commit(self):
            nonlocal published_key
            if version.status == "ready":
                published_key = version.package_key
                if outcome == "published-ack-lost":
                    raise ConnectionError("publication acknowledgement lost")

        async def rollback(self):
            pass

        async def scalar(self, statement):
            return published_key

    @asynccontextmanager
    async def sessions():
        yield Session()

    def package(snapshot, media, destination, **kwargs):
        destination.write_bytes(b"immutable frozen package")
        return {"summary": {}}

    authority = (
        [None, TrainingDatasetError("test_interrupted", "interrupted")]
        if outcome == "failed"
        else None
    )
    monkeypatch.setattr(
        worker, "assert_dataset_authority", AsyncMock(side_effect=authority)
    )
    monkeypatch.setattr(worker, "build_training_package", package)
    monkeypatch.setattr(worker, "notify_job_terminal", AsyncMock())
    for name in ["mark_running", "mark_complete", "mark_failed"]:
        monkeypatch.setattr(worker.jobs, name, AsyncMock())
    monkeypatch.setattr(
        storage_service,
        "delete_prefix",
        lambda prefix, *, bucket: deletions.append((bucket, prefix)),
    )
    await worker._build_dataset(
        version.id, version.async_job_id, version.attempt_id, sessions
    )
    uploads = [call for call in frozen.client.calls if call[0] == "upload"]
    assert len(uploads) == 1 and uploads[0][1] == frozen.bucket
    if outcome == "failed":
        assert version.status == "failed"
        assert deletions == [
            (
                frozen.bucket,
                version.storage_ref["prefix"] + f"attempts/{version.attempt_id}/",
            )
        ]
    else:
        assert version.status == "ready" and deletions == []
        assert published_key == uploads[0][2]


async def test_model_detail_signs_the_trial_image_from_the_pinned_bucket(
    frozen, monkeypatch
):
    from app.api.v1 import trained_models as api

    model = SimpleNamespace(
        id=uuid.uuid4(), provenance={"dataset_id": str(frozen.version.id)}
    )
    deployment = SimpleNamespace(
        id=uuid.uuid4(), status="ready", trial={"task_id": frozen.sample.task_id}
    )

    def rendered(value):
        return SimpleNamespace(
            model_dump=lambda **kwargs: {
                "id": str(value.id),
                **({"trial": value.trial} if hasattr(value, "trial") else {}),
            }
        )

    monkeypatch.setattr(api, "lock_owner", AsyncMock())
    monkeypatch.setattr(api.service, "load_version", AsyncMock(return_value=model))
    monkeypatch.setattr(
        api.lineage, "model_metadata", AsyncMock(return_value={model.id: {}})
    )
    monkeypatch.setattr(api.ModelDeploymentOut, "model_validate", rendered)
    monkeypatch.setattr(api.TrainedModelOut, "model_validate", rendered)
    signed = []

    def download(key, **kwargs):
        signed.append((kwargs["bucket"], key))
        return f"https://owned-storage/{kwargs['bucket']}/{key}"

    monkeypatch.setattr(storage_service, "generate_download_url", download)
    db = SimpleNamespace(
        get=AsyncMock(return_value=frozen.version),
        scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [deployment])),
    )
    result = await api.model_detail(
        model.id,
        project=SimpleNamespace(id=frozen.version.project_id),
        db=db,
        user=SimpleNamespace(),
    )
    assert signed == [(frozen.bucket, frozen.key)]
    assert result["deployments"][0]["trial"]["sample_url"].startswith(
        f"https://owned-storage/{frozen.bucket}/"
    )
