"""Run artifact consumers preserve source ownership independently of defaults."""

from copy import deepcopy
import hashlib
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from fastapi import HTTPException
import pytest

from app.config import settings
from app.db.models.trained_model import TrainedModelVersion
from app.db.models.training_dataset import TrainingDatasetVersion
from app.db.models.training_run import TrainingAttempt, TrainingRun
from app.services import trained_models as models
from app.services import training_artifacts as artifacts
from app.services import training_evaluations as evaluations
from app.services import training_initialization as initialization
from app.services import training_run_storage as owners
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.managed_original_retirement import managed_storage_identity
from app.services.storage import storage_service as storage
from app.services.training_dataset_bridge import content_digest


class Objects:
    def __init__(self, endpoint):
        self.meta = SimpleNamespace(endpoint_url=endpoint)
        self.values, self.calls, self.bodies = {}, [], []

    def head_object(self, *, Bucket, Key):
        self.calls.append(("head", Bucket, Key))
        data, attempt_id = self.values[Bucket, Key]
        return {
            "ContentLength": len(data),
            "Metadata": {
                "attempt-id": str(attempt_id),
                "sha256": hashlib.sha256(data).hexdigest(),
            },
        }

    def get_object(self, *, Bucket, Key):
        head = self.head_object(Bucket=Bucket, Key=Key)
        self.calls.append(("get", Bucket, Key))
        body = io.BytesIO(self.values[Bucket, Key][0])
        self.bodies.append(body)
        return {**head, "Body": body}

    def generate_presigned_url(self, operation, *, Params, ExpiresIn):
        self.calls.append(("sign", Params["Bucket"], Params["Key"]))
        return f"https://storage.invalid/{Params['Bucket']}/{Params['Key']}"


class Database:
    def __init__(self, world):
        self.world = world
        self.added = []
        self.flush = AsyncMock()

    async def scalar(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        return self.world.run if entity is TrainingRun else None

    async def get(self, model, identifier):
        rows = {
            TrainingRun: self.world.run,
            TrainingAttempt: self.world.attempt,
            TrainedModelVersion: self.world.version,
            TrainingDatasetVersion: self.world.dataset,
        }
        row = rows.get(model)
        return row if row is not None and str(row.id) == str(identifier) else None

    def add(self, row):
        self.added.append(row)


def locator(project_id, identifier, bucket, *, kind="runs"):
    endpoint = (
        f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}"
    )
    return {
        "schema": 1,
        "purpose": "training-run" if kind == "runs" else "training-dataset",
        "location_id": str(LEGACY_LOCATION_ID),
        "config_revision": 1,
        "binding_generation": 1,
        "endpoint_url": endpoint,
        "bucket": bucket,
        "prefix": f"projects/{project_id}/{kind}/{identifier}/",
        "storage_identity": managed_storage_identity(endpoint, bucket),
        "origin": "new",
        "evidence": None,
    }


@pytest.fixture
def world(monkeypatch):
    project_id, run_id, attempt_id, dataset_id = (uuid.uuid4() for _ in range(4))
    ref = locator(project_id, run_id, "source-run-artifacts")
    client = Objects(ref["endpoint_url"])
    values = {}
    report = {"summary": {"mAP50": 0.7}}
    for name, role, data in [
        ("best.pt", "weights", b"retained source weights"),
        ("metrics.json", "metrics", json.dumps(report).encode()),
    ]:
        key = ref["prefix"] + f"attempts/{attempt_id}/{name}"
        values[name] = {
            "key": key,
            "role": role,
            "sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
            "epoch": None,
        }
        client.values[ref["bucket"], key] = data, attempt_id
        client.values["new-current-training", key] = (
            b"wrong same-key content",
            attempt_id,
        )
    run = SimpleNamespace(
        id=run_id,
        project_id=project_id,
        dataset_id=dataset_id,
        storage_ref=ref,
        config={
            "framework": "ultralytics",
            "framework_version": "test-version",
            "classes": ["car"],
            "base_model": {"id": "yolo11n"},
            "dataset_digest": "d" * 64,
            "validation_digest": "e" * 64,
        },
        config_digest="f" * 64,
    )
    attempt = SimpleNamespace(
        id=attempt_id,
        run_id=run_id,
        status="succeeded",
        artifacts=values,
        runtime={},
    )
    version = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=project_id,
        run_id=run_id,
        attempt_id=attempt_id,
        artifact=values["best.pt"],
        provenance={"dataset_id": str(dataset_id), "validation_digest": "e" * 64},
    )
    dataset = SimpleNamespace(
        id=dataset_id,
        project_id=project_id,
        storage_ref=locator(project_id, dataset_id, "frozen-inputs", kind="datasets"),
        manifest={"samples": [{"task_id": str(uuid.uuid4()), "split": "val"}]},
    )
    pin = {
        "project_id": str(project_id),
        "run_id": str(run_id),
        "attempt_id": str(attempt_id),
        "model_version_id": str(version.id),
        "source_digest": "1" * 64,
        "artifact": {
            key: version.artifact[key]
            for key in ("key", "role", "sha256", "size_bytes")
        },
        "task": "detect",
        "evaluation": {"report_digest": content_digest(report)},
    }
    value = SimpleNamespace(
        run=run,
        attempt=attempt,
        version=version,
        dataset=dataset,
        pin=pin,
        report=report,
        client=client,
        bucket=ref["bucket"],
    )
    value.db = Database(value)
    monkeypatch.setattr(storage, "client", client)
    monkeypatch.setattr(storage, "training_bucket", "new-current-training")
    return value


def test_json_reader_explicit_bucket_and_cache_hit_still_verify_saved_bytes(world):
    spec = world.attempt.artifacts["metrics.json"]
    assert (
        artifacts.read_json_artifact(spec, world.attempt.id, bucket=world.bucket)
        == world.report
    )
    assert (
        artifacts.read_json_artifact(spec, world.attempt.id, bucket=world.bucket)
        == world.report
    )
    world.client.values[world.bucket, spec["key"]] = b"corrupted", world.attempt.id
    with pytest.raises(ValueError, match="identity"):
        artifacts.read_json_artifact(spec, world.attempt.id, bucket=world.bucket)
    assert {call[1] for call in world.client.calls} == {world.bucket}
    assert all(body.closed for body in world.client.bodies)


async def test_model_and_frozen_initialization_resolve_same_source_owner_without_payload_changes(
    world,
):
    before = deepcopy(
        (world.pin, world.version.artifact, world.version.provenance, world.run.config)
    )
    model_owner = await models.owner_for_model(world.db, world.version)
    init_owner = await initialization.source_storage_owner(world.db, world.pin)
    assert model_owner.ref == init_owner.ref == world.run.storage_ref
    assert before == (
        world.pin,
        world.version.artifact,
        world.version.provenance,
        world.run.config,
    )
    assert not world.client.calls


@pytest.mark.parametrize(
    "field", ["project_id", "run_id", "attempt_id", "model_version_id", "artifact"]
)
async def test_initialization_cannot_substitute_another_frozen_source(world, field):
    pin = deepcopy(world.pin)
    pin[field] = (
        {**pin[field], "sha256": "0" * 64} if field == "artifact" else str(uuid.uuid4())
    )
    with pytest.raises(HTTPException) as failure:
        await initialization.source_storage_owner(world.db, pin)
    assert failure.value.status_code == 409 and not world.client.calls


@pytest.mark.parametrize(
    "change", ["null", "endpoint", "other-attempt", "other-run", "missing-publication"]
)
async def test_model_unknown_owner_or_foreign_key_never_signs_or_reads(world, change):
    if change == "null":
        world.run.storage_ref = None
    elif change == "endpoint":
        world.run.storage_ref = {
            **world.run.storage_ref,
            "endpoint_url": "http://unknown.invalid:9000",
        }
    elif change == "other-attempt":
        world.attempt.run_id = uuid.uuid4()
    elif change == "other-run":
        world.version.artifact = {
            **world.version.artifact,
            "key": world.version.artifact["key"].replace(
                str(world.run.id), str(uuid.uuid4())
            ),
        }
        world.attempt.artifacts = {"best.pt": world.version.artifact}
    else:
        world.attempt.artifacts = {}
    with pytest.raises(HTTPException) as failure:
        await models.owner_for_model(world.db, world.version)
    assert failure.value.status_code == 409 and not world.client.calls


@pytest.mark.parametrize("full", [False, True])
def test_initialization_verifies_source_owner_not_current_or_target(
    world, monkeypatch, full
):
    from app.services import training_runs

    def verify(key, spec, attempt_id, *, bucket):
        # The run-flow owner separately tests the whole streaming verifier.
        response = world.client.get_object(Bucket=bucket, Key=key)
        try:
            raw = response["Body"].read()
            assert hashlib.sha256(raw).hexdigest() == spec["sha256"]
            assert response["Metadata"]["attempt-id"] == str(attempt_id)
        finally:
            response["Body"].close()

    monkeypatch.setattr(training_runs, "verify_artifact", verify)
    before = deepcopy(world.pin)
    initialization.verify_storage(
        world.pin,
        world.attempt.artifacts["metrics.json"],
        owner=owners.resolve(world.run),
        full=full,
    )
    assert {call[1] for call in world.client.calls} == {world.bucket}
    assert all(body.closed for body in world.client.bodies)
    assert world.pin == before


async def test_accepted_initialization_reuse_keeps_frozen_pin_and_source_storage(
    world, monkeypatch
):
    monkeypatch.setattr(
        initialization,
        "source_pin",
        AsyncMock(return_value=(world.pin, world.attempt.artifacts["metrics.json"])),
    )
    monkeypatch.setattr(initialization, "compatible_pin", lambda *args, **kwargs: [])
    before = deepcopy(world.pin)
    result = await initialization.resolve(
        world.db,
        world.run.project_id,
        SimpleNamespace(
            kind="trained_model",
            model_version_id=world.version.id,
            source_digest=world.pin["source_digest"],
        ),
        "ultralytics",
        "yolo11n",
        full=False,
        framework_version="test-version",
    )
    assert result == before == world.pin
    assert {call[1] for call in world.client.calls} == {world.bucket}


async def test_evaluation_reads_source_run_and_rejects_unpinned_before_io(
    world, monkeypatch
):
    monkeypatch.setattr(evaluations, "parse_evaluation", lambda report, *args: report)
    assert (
        await evaluations.read_evaluation(world.run, world.attempt, 0, 20)
        == world.report
    )
    assert {call[1] for call in world.client.calls} == {world.bucket}
    world.client.calls.clear()
    world.run.storage_ref = None
    with pytest.raises(HTTPException) as failure:
        await evaluations.read_evaluation(world.run, world.attempt, 0, 20)
    assert failure.value.status_code == 409 and not world.client.calls


async def test_model_download_signs_source_run_owner(world, monkeypatch):
    from app.api.v1 import trained_models as api

    monkeypatch.setattr(api, "lock_owner", AsyncMock())
    monkeypatch.setattr(
        api.service, "load_version", AsyncMock(return_value=world.version)
    )
    signed = []

    def sign(key, *, bucket, **kwargs):
        signed.append((key, bucket))
        return "https://saved.invalid/model"

    monkeypatch.setattr(storage, "generate_download_url", sign)
    result = await api.download_model(
        world.version.id,
        project=SimpleNamespace(id=world.run.project_id),
        user=SimpleNamespace(),
        db=world.db,
    )
    assert result["sha256"] == world.version.artifact["sha256"]
    assert signed == [(world.version.artifact["key"], world.bucket)]


async def test_model_install_weights_and_trial_inputs_keep_distinct_owners(
    world, monkeypatch
):
    sample = world.dataset.manifest["samples"][0]
    installed, predicted = [], []

    class Backend:
        async def install_trained_model(self, payload):
            installed.append(payload)
            return {}

        async def predict(self, tasks, **kwargs):
            predicted.extend(tasks)
            return [
                SimpleNamespace(
                    task_id=sample["task_id"], inference_time_ms=1, result=[], score=0
                )
            ]

    monkeypatch.setattr(models, "enabled_backend", AsyncMock(return_value=object()))
    monkeypatch.setattr(models, "assert_routable_backend", AsyncMock())
    monkeypatch.setattr(models, "MLBackendClient", lambda _: Backend())
    monkeypatch.setattr(models, "expected_receipt", lambda _: {})
    monkeypatch.setattr(models, "check_receipt", lambda *args: None)
    monkeypatch.setattr(models, "frozen_reference", lambda *args, **kwargs: {})
    monkeypatch.setattr(models, "wire_context", lambda *args: {})
    monkeypatch.setattr(models, "validate_and_map_results", lambda *args: None)
    monkeypatch.setattr(models, "model_task", lambda _: "detect")
    before = deepcopy((world.version.artifact, world.version.provenance))
    await models.install_and_trial(
        world.db,
        SimpleNamespace(id=world.run.project_id),
        world.version,
        SimpleNamespace(registry_id=uuid.uuid4()),
    )
    assert installed[0]["download_url"].startswith(
        "https://storage.invalid/source-run-artifacts/"
    )
    assert predicted[0]["file_path"].startswith(
        "https://storage.invalid/frozen-inputs/"
    )
    assert before == (world.version.artifact, world.version.provenance)
    assert {call[1] for call in world.client.calls} == {world.bucket, "frozen-inputs"}


async def test_registration_checks_weights_and_metrics_in_the_run_bucket(
    world, monkeypatch
):
    monkeypatch.setattr(
        models,
        "registration_eligibility",
        lambda _: {"qualified": True, "qualification": None},
    )

    def verify(key, spec, attempt_id, *, bucket):
        assert bucket == world.bucket
        response = world.client.get_object(Bucket=bucket, Key=key)
        try:
            assert hashlib.sha256(response["Body"].read()).hexdigest() == spec["sha256"]
        finally:
            response["Body"].close()

    monkeypatch.setattr(models, "verify_artifact", verify)
    body = SimpleNamespace(
        run_id=world.run.id,
        attempt_id=world.attempt.id,
        bindings=["car"],
        request_id=uuid.uuid4(),
        name="Retained weights",
        model_dump=lambda **kwargs: {"name": "Retained weights"},
    )
    project = SimpleNamespace(
        id=world.run.project_id,
        tool_bindings={"bbox": {"enabled": True, "classes": ["car"]}},
    )
    version = await models.register(
        world.db, project, SimpleNamespace(id=uuid.uuid4()), body
    )
    assert version.artifact == world.version.artifact
    assert "storage_ref" not in version.provenance
    assert {call[1] for call in world.client.calls} == {world.bucket}
    assert all(body.closed for body in world.client.bodies)
