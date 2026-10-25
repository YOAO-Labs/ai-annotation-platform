"""Run outputs remain under their durable owner while the current bucket changes."""

from copy import deepcopy
import hashlib
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from botocore.exceptions import ClientError
from botocore.response import StreamingBody
from fastapi import HTTPException
import pytest

from app.config import settings
from app.db.models.training_dataset import TrainingDatasetVersion
from app.db.models.training_run import TrainingRun
from app.schemas.training_run import ExecutorFinish, TrainingArtifactSpec
from app.services import training_dataset_storage, training_run_storage
from app.services import training_runs as runs, training_observability as observations
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.managed_original_retirement import managed_storage_identity
from app.services.storage import storage_service
from app.services.training_dataset_bridge import content_digest


@pytest.fixture(autouse=True)
def legacy_transport_boundary(monkeypatch):
    # These pre-existing unit cases isolate business ownership from admission.
    from app.services import training_storage_io
    from app.services.storage import storage_service

    async def run(db, owner, purpose, callback, *args, **kwargs):
        return callback(storage_service, *args, **kwargs)

    monkeypatch.setattr(training_storage_io, "run", run)


class Objects:
    def __init__(self, endpoint):
        self.meta = SimpleNamespace(endpoint_url=endpoint)
        self.objects, self.metadata, self.calls = {}, {}, []

    def head_object(self, *, Bucket, Key):
        self.calls.append(("head", Bucket, Key))
        if (Bucket, Key) not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "HeadObject")
        data = self.objects[Bucket, Key]
        return {"ContentLength": len(data), "Metadata": self.metadata[Bucket, Key]}

    def get_object(self, *, Bucket, Key):
        self.calls.append(("get", Bucket, Key))
        if (Bucket, Key) not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        data = self.objects[Bucket, Key]
        return {
            "ContentLength": len(data),
            "Metadata": self.metadata[Bucket, Key],
            "Body": StreamingBody(io.BytesIO(data), len(data)),
        }

    def put_object(self, *, Bucket, Key, Body, Metadata, **kwargs):
        self.calls.append(("put", Bucket, Key))
        self.objects[Bucket, Key] = bytes(Body)
        self.metadata[Bucket, Key] = Metadata

    def generate_presigned_url(self, operation, *, Params, ExpiresIn):
        self.calls.append(("sign-" + operation, Params["Bucket"], Params["Key"]))
        return f"https://owned-storage/{Params['Bucket']}/{Params['Key']}"


def reference(project_id, record_id, *, purpose, bucket):
    endpoint = (
        f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}"
    )
    prefix_for = (
        training_run_storage.prefix_for
        if purpose == "training-run"
        else training_dataset_storage.prefix_for
    )
    return {
        "schema": 1,
        "purpose": purpose,
        "location_id": str(LEGACY_LOCATION_ID),
        "config_revision": 1,
        "binding_generation": 1,
        "endpoint_url": endpoint,
        "bucket": bucket,
        "prefix": prefix_for(project_id, record_id),
        "storage_identity": managed_storage_identity(endpoint, bucket),
        "origin": "new",
        "evidence": None,
    }


@pytest.fixture
def owned(monkeypatch):
    project_id, run_id, dataset_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    run = SimpleNamespace(
        id=run_id,
        project_id=project_id,
        dataset_id=dataset_id,
        storage_ref=reference(
            project_id, run_id, purpose="training-run", bucket="old-run-bucket"
        ),
        config={"parameters": {"epochs": 10}},
        config_digest="a" * 64,
        status="training",
        async_job_id=uuid.uuid4(),
        error_message=None,
    )
    attempt = SimpleNamespace(
        id=uuid.uuid4(),
        run_id=run_id,
        status="training",
        native_budget=None,
        upload_inventory={},
        artifacts={},
        resume_from_id=None,
        finish_request_digest=None,
    )
    run.current_attempt_id = attempt.id
    dataset = SimpleNamespace(
        id=dataset_id,
        project_id=project_id,
        storage_ref=reference(
            project_id,
            dataset_id,
            purpose="training-dataset",
            bucket="dataset-input-bucket",
        ),
    )
    dataset.package_key = (
        dataset.storage_ref["prefix"] + "attempts/" + str(uuid.uuid4()) + "/dataset.zip"
    )
    client = Objects(run.storage_ref["endpoint_url"])
    monkeypatch.setattr(storage_service, "client", client)
    monkeypatch.setattr(storage_service, "training_bucket", "new-current-bucket")
    return SimpleNamespace(
        run=run,
        attempt=attempt,
        dataset=dataset,
        client=client,
        bucket="old-run-bucket",
    )


def artifact(owned, name="best.pt", role="weights", raw=b"frozen output"):
    key = owned.run.storage_ref["prefix"] + f"attempts/{owned.attempt.id}/{name}"
    spec = {
        "name": name,
        "role": role,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
    }
    owned.client.objects[owned.bucket, key] = raw
    owned.client.metadata[owned.bucket, key] = {
        "attempt-id": str(owned.attempt.id),
        "sha256": spec["sha256"],
    }
    owned.client.objects["new-current-bucket", key] = b"same-key trap"
    owned.client.metadata["new-current-bucket", key] = {}
    return {**spec, "key": key}


def finish_body(status):
    return ExecutorFinish(
        executor_id="flow-executor",
        boot_id=uuid.uuid4(),
        lease_token="exact-fenced-token-32-characters-long",
        status=status,
        process_exited=True,
    )


async def test_upload_authorization_signs_the_saved_run_bucket(owned, monkeypatch):
    value = artifact(owned)
    owned.attempt.upload_inventory = {}
    monkeypatch.setattr(
        runs, "fenced", AsyncMock(return_value=(None, owned.run, owned.attempt))
    )
    monkeypatch.setattr(runs.jobs, "update_progress", AsyncMock())
    body = SimpleNamespace(
        artifacts=[
            TrainingArtifactSpec(**{k: v for k, v in value.items() if k != "key"})
        ]
    )
    result = await runs.authorize_upload(
        SimpleNamespace(commit=AsyncMock()), owned.attempt.id, body
    )
    assert result["best.pt"]["url"].startswith(f"https://owned-storage/{owned.bucket}/")
    assert owned.client.calls == [("sign-put_object", owned.bucket, value["key"])]


@pytest.mark.parametrize("invalid", ["missing-pin", "wrong-origin", "foreign-attempt"])
async def test_upload_authorization_rejects_unknown_owner_before_signing(
    owned, monkeypatch, invalid
):
    value = artifact(owned)
    if invalid == "missing-pin":
        owned.run.storage_ref = None
    elif invalid == "wrong-origin":
        owned.client.meta.endpoint_url = "https://other-origin.invalid"
    else:
        owned.attempt.run_id = uuid.uuid4()
    monkeypatch.setattr(
        runs, "fenced", AsyncMock(return_value=(None, owned.run, owned.attempt))
    )
    body = SimpleNamespace(
        artifacts=[
            TrainingArtifactSpec(**{k: v for k, v in value.items() if k != "key"})
        ]
    )
    with pytest.raises(HTTPException) as error:
        await runs.authorize_upload(
            SimpleNamespace(commit=AsyncMock()), owned.attempt.id, body
        )
    assert error.value.status_code == 409
    assert owned.client.calls == [] and owned.attempt.upload_inventory == {}


async def test_new_attempt_never_implicitly_pins_a_historical_run(owned, monkeypatch):
    owned.run.storage_ref = None
    allocate = AsyncMock(side_effect=AssertionError("retry must not allocate"))
    monkeypatch.setattr(training_run_storage, "allocate", allocate)
    db = SimpleNamespace(
        scalar=AsyncMock(side_effect=AssertionError("must reject first"))
    )
    with pytest.raises(HTTPException) as error:
        await runs.new_attempt(db, owned.run)
    assert error.value.status_code == 409
    allocate.assert_not_awaited()
    db.scalar.assert_not_awaited()


def test_artifact_verification_requires_and_uses_explicit_bucket(owned):
    value = artifact(owned)
    runs.verify_artifact(value["key"], value, owned.attempt.id, bucket=owned.bucket)
    assert owned.client.calls == [("get", owned.bucket, value["key"])]
    with pytest.raises(TypeError):
        runs.verify_artifact(value["key"], value, owned.attempt.id)


def test_full_artifact_digest_still_rejects_changed_saved_content(owned):
    value = artifact(owned)
    owned.client.objects[owned.bucket, value["key"]] = b"mutated bytes"
    with pytest.raises(ValueError):
        runs.verify_artifact(value["key"], value, owned.attempt.id, bucket=owned.bucket)
    assert {call[1] for call in owned.client.calls} == {owned.bucket}


def test_redacted_log_reads_and_writes_only_the_saved_run_prefix(owned, monkeypatch):
    token = "synthetic-private-executor-token"
    monkeypatch.setattr(settings, "training_executor_token", token)
    value = artifact(
        owned, "train.log", "log", f"Authorization: Bearer {token}\n".encode()
    )
    result = observations.redacted_log(owned.run, owned.attempt, "train.log", value)
    assert result["key"].startswith(
        owned.run.storage_ref["prefix"] + f"attempts/{owned.attempt.id}/redacted-logs/"
    )
    output = owned.client.objects[owned.bucket, result["key"]]
    assert token.encode() not in output
    assert result["source_sha256"] == value["sha256"]
    assert {call[1] for call in owned.client.calls} == {owned.bucket}


@pytest.mark.parametrize("invalid", ["missing-pin", "foreign-key", "foreign-attempt"])
def test_log_redaction_refuses_unproven_storage_without_io(owned, invalid):
    value = artifact(owned, "train.log", "log", b"safe log\n")
    if invalid == "missing-pin":
        owned.run.storage_ref = None
    elif invalid == "foreign-key":
        value["key"] = (
            owned.run.storage_ref["prefix"] + f"attempts/{uuid.uuid4()}/train.log"
        )
    else:
        owned.attempt.run_id = uuid.uuid4()
    with pytest.raises(HTTPException) as error:
        observations.redacted_log(owned.run, owned.attempt, "train.log", value)
    assert error.value.status_code == 409 and owned.client.calls == []


def test_summary_and_checkpoint_use_exact_attempt_in_saved_bucket(owned):
    raw = json.dumps(
        {
            "protocol": 1,
            "run_id": str(owned.run.id),
            "attempt_id": str(owned.attempt.id),
            "config_digest": owned.run.config_digest,
        }
    ).encode()
    summary = artifact(owned, "telemetry.json", "telemetry", raw)
    observations.verify_summary(owned.run, owned.attempt, summary)
    checkpoint = artifact(owned, "last.pt", "checkpoint")
    assert observations.checkpoint_available(owned.run, owned.attempt, checkpoint)
    assert {call[1] for call in owned.client.calls} == {owned.bucket}


@pytest.mark.parametrize("invalid", ["missing-pin", "foreign-key", "foreign-attempt"])
def test_checkpoint_probe_does_not_guess_another_storage_owner(owned, invalid):
    checkpoint = artifact(owned, "last.pt", "checkpoint")
    if invalid == "missing-pin":
        owned.run.storage_ref = None
    elif invalid == "foreign-key":
        checkpoint["key"] = "foreign-run/last.pt"
    else:
        owned.attempt.run_id = uuid.uuid4()
    assert not observations.checkpoint_available(owned.run, owned.attempt, checkpoint)
    assert owned.client.calls == []


def test_native_configuration_observation_uses_saved_owner(owned, monkeypatch):
    proof = {"qualified": True}
    raw = json.dumps(
        {
            "config_digest": owned.run.config_digest,
            "runtime_options": {"resume": False},
            "observed": {},
            "verification": proof,
        }
    ).encode()
    receipt = artifact(owned, "native-config.json", "config", raw)
    monkeypatch.setattr(runs, "verify_native", lambda *args: proof)
    runs.verify_native_artifact(
        owned.run, owned.attempt, {"native-config.json": receipt}
    )
    assert owned.client.calls == [("get", owned.bucket, receipt["key"])]


@pytest.mark.parametrize("status", ["failed", "cancelled"])
async def test_unpinned_stopped_receipt_can_settle_without_storage_resolution(
    owned, monkeypatch, status
):
    value = artifact(owned)
    owned.attempt.upload_inventory = {
        "best.pt": {k: v for k, v in value.items() if k != "key"}
    }
    before = deepcopy(owned.attempt.upload_inventory)
    owned.run.storage_ref = None
    lease = SimpleNamespace(attempt_id=owned.attempt.id)
    monkeypatch.setattr(
        runs, "fenced", AsyncMock(return_value=(lease, owned.run, owned.attempt))
    )
    monkeypatch.setattr(
        runs,
        "storage_owner",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("no storage resolution on stop")
        ),
    )
    terminal = AsyncMock()
    monkeypatch.setattr(runs, "mark_terminal", terminal)
    result = await runs.finish(
        SimpleNamespace(commit=AsyncMock()), owned.attempt.id, finish_body(status)
    )
    assert result is owned.attempt and lease.attempt_id is None
    assert owned.attempt.upload_inventory == before
    assert owned.attempt.artifacts == {} and owned.client.calls == []
    assert terminal.await_args.args[3] == status


async def test_success_cannot_release_a_lease_when_storage_is_unknown(
    owned, monkeypatch
):
    owned.run.storage_ref = None
    lease = SimpleNamespace(attempt_id=owned.attempt.id)
    monkeypatch.setattr(
        runs, "fenced", AsyncMock(return_value=(lease, owned.run, owned.attempt))
    )
    monkeypatch.setattr(runs, "authority", AsyncMock())
    terminal = AsyncMock()
    monkeypatch.setattr(runs, "mark_terminal", terminal)
    with pytest.raises(HTTPException) as error:
        await runs.finish(
            SimpleNamespace(commit=AsyncMock()),
            owned.attempt.id,
            finish_body("succeeded"),
        )
    assert error.value.status_code == 409
    assert lease.attempt_id == owned.attempt.id and owned.client.calls == []
    terminal.assert_not_awaited()


async def test_failed_receipt_for_foreign_attempt_cannot_release_gpu(
    owned, monkeypatch
):
    owned.run.storage_ref = None
    owned.attempt.run_id = uuid.uuid4()
    lease = SimpleNamespace(attempt_id=owned.attempt.id)
    monkeypatch.setattr(
        runs, "fenced", AsyncMock(return_value=(lease, owned.run, owned.attempt))
    )
    with pytest.raises(HTTPException) as error:
        await runs.finish(
            SimpleNamespace(commit=AsyncMock()), owned.attempt.id, finish_body("failed")
        )
    assert error.value.status_code == 409 and lease.attempt_id == owned.attempt.id
    assert owned.client.calls == []


async def test_successful_finish_verifies_every_artifact_in_saved_bucket(
    owned, monkeypatch
):
    roles = {
        "best.pt": "weights",
        "last.pt": "checkpoint",
        "metrics.json": "metrics",
        "config.json": "config",
        "train.log": "log",
    }
    for name, role in roles.items():
        value = artifact(owned, name, role, (name + " frozen bytes").encode())
        owned.attempt.upload_inventory[name] = {
            k: v for k, v in value.items() if k != "key"
        }
    lease = SimpleNamespace(attempt_id=owned.attempt.id)
    monkeypatch.setattr(
        runs, "fenced", AsyncMock(return_value=(lease, owned.run, owned.attempt))
    )
    monkeypatch.setattr(runs, "authority", AsyncMock())
    terminal = AsyncMock()
    monkeypatch.setattr(runs, "mark_terminal", terminal)
    await runs.finish(
        SimpleNamespace(commit=AsyncMock()), owned.attempt.id, finish_body("succeeded")
    )
    assert set(owned.attempt.artifacts) == set(roles)
    assert len(owned.client.calls) == len(roles)
    assert {call[1] for call in owned.client.calls} == {owned.bucket}
    assert lease.attempt_id is None and terminal.await_args.args[3] == "succeeded"


@pytest.mark.parametrize("log", [False, True])
async def test_api_artifact_download_uses_the_saved_run_bucket(owned, monkeypatch, log):
    from app.api.v1 import training_runs as api

    value = artifact(
        owned,
        "train.log" if log else "best.pt",
        "log" if log else "weights",
        b"safe frozen output\n",
    )
    owned.attempt.status = "succeeded"
    owned.attempt.artifacts[value["name"]] = value
    monkeypatch.setattr(api, "lock_owner", AsyncMock())
    db = SimpleNamespace(
        scalar=AsyncMock(return_value=owned.run),
        get=AsyncMock(return_value=owned.attempt),
    )
    result = await api.download_artifact(
        owned.run.id,
        owned.attempt.id,
        value["name"],
        project=SimpleNamespace(id=owned.run.project_id),
        user=SimpleNamespace(),
        db=db,
    )
    assert result["url"]
    assert {call[1] for call in owned.client.calls} == {owned.bucket}
    signed = [c for c in owned.client.calls if c[0] == "sign-get_object"]
    assert len(signed) == 1 and signed[0][2].startswith(owned.run.storage_ref["prefix"])


async def test_run_creation_allocates_before_its_first_attempt(owned, monkeypatch):
    from app.api.v1 import training_runs as api

    events = []
    added = []
    project, user = (
        SimpleNamespace(id=owned.run.project_id),
        SimpleNamespace(id=uuid.uuid4()),
    )
    config = {"dataset_id": str(owned.dataset.id)}
    body = SimpleNamespace(
        request_id=uuid.uuid4(),
        request_payload=lambda: {"name": "new run"},
        dataset_id=owned.dataset.id,
        preflight_digest=content_digest(config),
        origin=None,
        name="Pinned before attempt",
    )

    class Session:
        execute = AsyncMock()
        scalar = AsyncMock(return_value=None)
        commit = AsyncMock()

        def add(self, run):
            added.append(run)

        async def flush(self):
            added[-1].id = uuid.uuid4()

    async def allocate(db, run):
        assert isinstance(run, TrainingRun) and run.id
        run.storage_ref = reference(
            project.id, run.id, purpose="training-run", bucket=owned.bucket
        )
        events.append("allocate")

    async def attempt(db, run):
        assert run.storage_ref is not None
        events.append("attempt")

    monkeypatch.setattr(api, "lock_owner", AsyncMock())
    monkeypatch.setattr(api.service, "frozen_config", AsyncMock(return_value=config))
    monkeypatch.setattr(api.recipes, "origin_snapshot", AsyncMock(return_value=None))
    monkeypatch.setattr(api.service, "allocate_storage", allocate)
    monkeypatch.setattr(api.service, "new_attempt", attempt)
    result = await api.create_run(
        body,
        request=SimpleNamespace(state=SimpleNamespace()),
        project=project,
        user=user,
        db=Session(),
    )
    assert result is added[0] and events == ["allocate", "attempt"]


@pytest.mark.parametrize("resume", [False, True])
async def test_assignment_keeps_dataset_and_run_sources_separate(owned, resume):
    parent = SimpleNamespace(id=uuid.uuid4(), run_id=owned.run.id)
    key = owned.run.storage_ref["prefix"] + f"attempts/{parent.id}/last.pt"
    parent.artifacts = {"last.pt": {"key": key, "role": "checkpoint"}}
    if resume:
        owned.attempt.resume_from_id = parent.id

    async def get(model, identifier):
        return owned.dataset if model is TrainingDatasetVersion else parent

    before = deepcopy(owned.run.config)
    result = await runs.assignment(
        SimpleNamespace(get=get, commit=AsyncMock()), owned.run, owned.attempt, "token"
    )
    assert result["dataset_url"].startswith(
        "https://owned-storage/dataset-input-bucket/"
    )
    if resume:
        assert result["resume"]["url"].startswith(
            f"https://owned-storage/{owned.bucket}/"
        )
    else:
        assert result["resume"] is None
    assert owned.run.config == before
    assert all(call[1] != "new-current-bucket" for call in owned.client.calls)


@pytest.mark.parametrize("invalid", ["foreign-run", "foreign-key", "no-checkpoint"])
async def test_resume_cannot_sign_another_attempt_owner(owned, invalid):
    parent = SimpleNamespace(id=uuid.uuid4(), run_id=owned.run.id)
    key = owned.run.storage_ref["prefix"] + f"attempts/{parent.id}/last.pt"
    if invalid == "foreign-run":
        parent.run_id = uuid.uuid4()
    elif invalid == "foreign-key":
        key = owned.run.storage_ref["prefix"] + f"attempts/{uuid.uuid4()}/last.pt"
    parent.artifacts = {"last.pt": {"key": key, "role": "checkpoint"}}
    if invalid == "no-checkpoint":
        parent.artifacts = {}
    owned.attempt.resume_from_id = parent.id

    async def get(model, identifier):
        return owned.dataset if model is TrainingDatasetVersion else parent

    with pytest.raises(HTTPException) as error:
        await runs.assignment(
            SimpleNamespace(get=get, commit=AsyncMock()),
            owned.run,
            owned.attempt,
            "token",
        )
    assert error.value.status_code == 409
    assert not any(call[1] == owned.bucket for call in owned.client.calls)


@pytest.mark.parametrize("foreign_attempt", [False, True])
async def test_assignment_initialization_uses_its_source_run_owner(
    owned, monkeypatch, foreign_attempt
):
    source_run_id, source_attempt_id = uuid.uuid4(), uuid.uuid4()
    source_ref = reference(
        owned.run.project_id,
        source_run_id,
        purpose="training-run",
        bucket="source-model-bucket",
    )
    source_owner = training_run_storage.resolve_ref(source_ref)
    key_attempt = uuid.uuid4() if foreign_attempt else source_attempt_id
    pin = {
        "run_id": str(source_run_id),
        "attempt_id": str(source_attempt_id),
        "source_digest": "b" * 64,
        "artifact": {
            "key": source_owner.key(f"attempts/{key_attempt}/best.pt"),
            "role": "weights",
            "size_bytes": 12,
            "sha256": "c" * 64,
        },
    }
    owned.run.config["initialization"] = pin
    before = deepcopy(owned.run.config)
    source_resolver = AsyncMock(return_value=source_owner)
    monkeypatch.setattr(runs.initialization, "source_storage_owner", source_resolver)
    db = SimpleNamespace(get=AsyncMock(return_value=owned.dataset), commit=AsyncMock())
    if foreign_attempt:
        with pytest.raises(HTTPException) as error:
            await runs.assignment(db, owned.run, owned.attempt, "token")
        assert error.value.status_code == 409
        assert not any(call[1] == "source-model-bucket" for call in owned.client.calls)
    else:
        result = await runs.assignment(db, owned.run, owned.attempt, "token")
        assert result["initialization"]["url"].startswith(
            "https://owned-storage/source-model-bucket/"
        )
        assert result["dataset_url"].startswith(
            "https://owned-storage/dataset-input-bucket/"
        )
        assert {call[1] for call in owned.client.calls} == {
            "dataset-input-bucket",
            "source-model-bucket",
        }
    source_resolver.assert_awaited_once_with(db, pin)
    assert owned.run.config == before


async def test_unknown_active_claim_replay_retains_its_gpu_lease(owned, monkeypatch):
    body = SimpleNamespace(
        resource_id="owned-gpu",
        runtime={"gpu_uuid": settings.training_gpu_uuid},
        executor_id=uuid.uuid4(),
        boot_id=uuid.uuid4(),
        claim_id=uuid.uuid4(),
        recover_only=True,
    )
    owned.run.storage_ref = None
    owned.attempt.executor_id, owned.attempt.boot_id, owned.attempt.claim_id = (
        body.executor_id,
        body.boot_id,
        body.claim_id,
    )
    owned.attempt.lease_digest = hashlib.sha256(
        runs.claim_token(owned.attempt, body.claim_id).encode()
    ).hexdigest()
    lease = SimpleNamespace(attempt_id=owned.attempt.id)

    async def get(model, identifier):
        return owned.run if model is TrainingRun else owned.attempt

    monkeypatch.setattr(runs, "catalog", lambda: {"enabled": True})
    monkeypatch.setattr(runs, "resource_id", lambda: "owned-gpu")
    monkeypatch.setattr(runs, "lock_resource", AsyncMock(return_value=lease))
    monkeypatch.setattr(runs, "observe_lost", AsyncMock())
    terminal = AsyncMock()
    monkeypatch.setattr(runs, "mark_terminal", terminal)
    with pytest.raises(HTTPException) as error:
        await runs.claim(SimpleNamespace(get=get), body)
    assert error.value.status_code == 409
    assert lease.attempt_id == owned.attempt.id and owned.client.calls == []
    terminal.assert_not_awaited()


@pytest.mark.parametrize("foreign_attempt", [False, True])
async def test_unpinned_queued_run_cannot_acquire_gpu(
    owned, monkeypatch, foreign_attempt
):
    owned.run.storage_ref = None
    owned.run.config["resource_id"] = "owned-gpu"
    owned.run.status = owned.attempt.status = "queued"
    lease = SimpleNamespace(attempt_id=None)
    body = SimpleNamespace(
        resource_id="owned-gpu",
        runtime={"gpu_uuid": settings.training_gpu_uuid},
        executor_id=uuid.uuid4(),
        boot_id=uuid.uuid4(),
        recover_only=False,
    )
    monkeypatch.setattr(runs, "catalog", lambda: {"enabled": True})
    monkeypatch.setattr(runs, "resource_id", lambda: "owned-gpu")
    monkeypatch.setattr(runs, "runtime_compatible", lambda *args: True)
    monkeypatch.setattr(runs, "lock_resource", AsyncMock(return_value=lease))
    monkeypatch.setattr(runs, "observe_lost", AsyncMock())
    terminal = AsyncMock()
    monkeypatch.setattr(runs, "mark_terminal", terminal)
    db = SimpleNamespace(
        scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [owned.run])),
        get=AsyncMock(return_value=owned.attempt),
    )
    if foreign_attempt:
        owned.attempt.run_id = uuid.uuid4()
        with pytest.raises(HTTPException) as error:
            await runs.claim(db, body)
        assert error.value.status_code == 409
        terminal.assert_not_awaited()
        assert lease.attempt_id is None and owned.client.calls == []
        return
    assert await runs.claim(db, body) is None
    assert lease.attempt_id is None and owned.client.calls == []
    assert terminal.await_args.args[3] == "failed"
