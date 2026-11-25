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
from app.db.models.trained_model import TrainedModelVersion
from app.db.models.training_run import (
    TrainingAttempt,
    TrainingResourceLease,
    TrainingRun,
)
from app.schemas.training_run import ExecutorFinish, ExecutorUpload
from app.services import training_dataset_storage, training_run_storage
from app.services import training_runs as runs, training_observability as observations
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.managed_original_retirement import managed_storage_identity
from app.services.storage import storage_service
from app.services.training_dataset_bridge import content_digest
from app.services.training_datasets import bind_training_authority
from tests.factory import create_project
from tests.test_training_runs import (
    create_run,
    executor_claim,
    ready_dataset,
    training_config as training_config,
)
from tests.training_consumer_fixtures import mock_legacy_transport


@pytest.fixture(autouse=True)
def legacy_transport_boundary(monkeypatch):
    mock_legacy_transport(monkeypatch)


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
        package_sha256=None,
        size_bytes=None,
        manifest={"samples": []},
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


@pytest.fixture
async def saved_dataset(db_session, project_admin):
    owner, _ = project_admin
    project = await create_project(db_session, owner_id=owner.id)
    version_id, build_id = uuid.uuid4(), uuid.uuid4()
    ref = reference(
        project.id,
        version_id,
        purpose="training-dataset",
        bucket="dataset-input-bucket",
    )
    dataset = TrainingDatasetVersion(
        id=version_id,
        project_id=project.id,
        creator_id=owner.id,
        request_id=uuid.uuid4(),
        request_digest="a" * 64,
        snapshot={},
        snapshot_digest="b" * 64,
        name="Frozen inputs",
        status="ready",
        attempt_id=build_id,
        storage_ref=ref,
        package_key=f"{ref['prefix']}attempts/{build_id}/dataset.zip",
        package_sha256="c" * 64,
        size_bytes=32,
    )
    db_session.add(dataset)
    await db_session.flush()
    return dataset


async def historical_run(db, dataset, *, bucket, status, config):
    run_id = uuid.uuid4()
    run = TrainingRun(
        id=run_id,
        project_id=dataset.project_id,
        creator_id=dataset.creator_id,
        dataset_id=dataset.id,
        request_id=uuid.uuid4(),
        request_digest=content_digest(config),
        config=config,
        config_digest=content_digest(config),
        name="Historical run",
        status=status,
        storage_ref=(
            reference(dataset.project_id, run_id, purpose="training-run", bucket=bucket)
            if bucket is not None
            else None
        ),
    )
    db.add(run)
    await db.flush()
    return run


@pytest.fixture
def make_flow(db_session, saved_dataset, owned):
    async def make(*, pinned=True, status="training", initialization=None):
        claim = executor_claim()
        config = {
            "framework": "ultralytics",
            "framework_version": claim.runtime["frameworks"]["ultralytics"],
            "image_digest": claim.runtime["image_digest"],
            "resource_id": claim.resource_id,
        }
        if initialization is not None:
            config["initialization"] = initialization
        run = await historical_run(
            db_session,
            saved_dataset,
            bucket=owned.bucket if pinned else None,
            status=status,
            config=config,
        )
        attempt = TrainingAttempt(run_id=run.id, ordinal=1, status=status)
        db_session.add(attempt)
        await db_session.flush()
        job = await runs.jobs.create_job(
            db_session,
            kind="training_run",
            project_id=run.project_id,
            user_id=run.creator_id,
            payload={
                "run_id": str(run.id),
                "attempt_id": str(attempt.id),
                "name": run.name,
            },
        )
        run.current_attempt_id, run.async_job_id = attempt.id, job.id
        token = runs.claim_token(attempt, claim.claim_id)
        if status == "training":
            attempt.executor_id, attempt.boot_id = claim.executor_id, claim.boot_id
            attempt.resource_id, attempt.claim_id = claim.resource_id, claim.claim_id
            attempt.lease_digest = hashlib.sha256(token.encode()).hexdigest()
            await runs.lock_resource(db_session, claim.resource_id)
            lease = await db_session.get(TrainingResourceLease, claim.resource_id)
            lease.attempt_id = attempt.id
            lease.executor_id, lease.boot_id = claim.executor_id, claim.boot_id
        await db_session.flush()
        return SimpleNamespace(
            run=run,
            attempt=attempt,
            dataset=saved_dataset,
            client=owned.client,
            bucket=owned.bucket,
            claim=claim,
            fence={
                "executor_id": claim.executor_id,
                "boot_id": claim.boot_id,
                "lease_token": token,
            },
        )

    return make


async def finish(db, flow, status):
    return await runs.finish(
        db,
        flow.attempt.id,
        ExecutorFinish(**flow.fence, status=status, process_exited=True),
        training_authority=bind_training_authority(db),
    )


async def test_upload_authorization_signs_the_saved_run_bucket(make_flow, db_session):
    flow = await make_flow()
    value = artifact(flow)
    request = ExecutorUpload(
        **flow.fence, artifacts=[{k: v for k, v in value.items() if k != "key"}]
    )
    result = await runs.authorize_upload(db_session, flow.attempt.id, request)
    assert (
        result["best.pt"]["url"]
        == f"https://owned-storage/{flow.bucket}/{value['key']}"
    )
    assert flow.client.calls == [("sign-put_object", flow.bucket, value["key"])]
    await db_session.refresh(flow.attempt)
    assert flow.attempt.upload_inventory == {
        "best.pt": request.artifacts[0].model_dump()
    }


@pytest.mark.parametrize("invalid", ["missing-pin", "wrong-origin", "foreign-attempt"])
async def test_upload_authorization_rejects_unknown_owner_before_signing(
    make_flow, db_session, invalid
):
    flow = await make_flow(pinned=invalid != "missing-pin")
    if invalid == "wrong-origin":
        flow.client.meta.endpoint_url = "https://other-origin.invalid"
    elif invalid == "foreign-attempt":
        flow.run.current_attempt_id = uuid.uuid4()
    request = ExecutorUpload(
        **flow.fence,
        artifacts=[
            {"name": "best.pt", "role": "weights", "sha256": "a" * 64, "size_bytes": 32}
        ],
    )
    with pytest.raises(HTTPException) as error:
        await runs.authorize_upload(db_session, flow.attempt.id, request)
    assert error.value.status_code == 409
    await db_session.refresh(flow.attempt)
    assert flow.client.calls == [] and flow.attempt.upload_inventory == {}


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


async def lease_holder(db, flow):
    lease = await runs.data.get_lease(db, flow.claim.resource_id)
    return lease.attempt_id


@pytest.mark.parametrize("status", ["failed", "cancelled"])
async def test_unpinned_stopped_receipt_can_settle_without_storage_resolution(
    make_flow, db_session, monkeypatch, status
):
    flow = await make_flow(pinned=False)
    before = {
        "best.pt": {
            "name": "best.pt",
            "role": "weights",
            "sha256": "a" * 64,
            "size_bytes": 32,
        }
    }
    flow.attempt.upload_inventory = before

    def refuse_resolution(*args):
        pytest.fail("A stopped, unpinned attempt must not resolve storage")

    monkeypatch.setattr(runs, "storage_owner", refuse_resolution)
    result = await finish(db_session, flow, status)
    assert result.id == flow.attempt.id and result.status == status
    await db_session.refresh(flow.attempt)
    assert flow.attempt.status == status and flow.attempt.upload_inventory == before
    assert flow.attempt.artifacts == {} and flow.client.calls == []
    assert await lease_holder(db_session, flow) is None


async def test_success_cannot_release_a_lease_when_storage_is_unknown(
    make_flow, db_session
):
    flow = await make_flow(pinned=False)
    with pytest.raises(HTTPException) as error:
        await finish(db_session, flow, "succeeded")
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "training_storage_unpinned"
    await db_session.refresh(flow.attempt)
    assert flow.attempt.status == "training" and flow.client.calls == []
    assert await lease_holder(db_session, flow) == flow.attempt.id


async def test_failed_receipt_for_foreign_attempt_cannot_release_gpu(
    make_flow, db_session
):
    flow = await make_flow(pinned=False)
    foreign = await historical_run(
        db_session, flow.dataset, bucket=None, status="training", config=flow.run.config
    )
    flow.attempt.run_id = foreign.id
    with pytest.raises(HTTPException) as error:
        await finish(db_session, flow, "failed")
    assert (
        error.value.status_code == 409 and error.value.detail["code"] == "stale_attempt"
    )
    assert await lease_holder(db_session, flow) == flow.attempt.id
    await db_session.refresh(flow.attempt)
    assert flow.attempt.status == "training" and flow.client.calls == []


async def test_successful_finish_verifies_every_artifact_in_saved_bucket(
    make_flow, db_session
):
    flow = await make_flow()
    roles = {
        "best.pt": "weights",
        "last.pt": "checkpoint",
        "metrics.json": "metrics",
        "config.json": "config",
        "train.log": "log",
    }
    inventory = {
        name: artifact(flow, name, role, (name + " frozen bytes").encode())
        for name, role in roles.items()
    }
    flow.attempt.upload_inventory = {
        name: {k: v for k, v in spec.items() if k != "key"}
        for name, spec in inventory.items()
    }
    result = await finish(db_session, flow, "succeeded")
    await db_session.refresh(flow.attempt)
    assert result.artifacts == flow.attempt.artifacts == inventory
    assert flow.attempt.status == "succeeded"
    assert sorted(flow.client.calls) == sorted(
        ("get", flow.bucket, spec["key"]) for spec in inventory.values()
    )
    assert await lease_holder(db_session, flow) is None


@pytest.mark.parametrize("log", [False, True])
async def test_api_artifact_download_uses_the_saved_run_bucket(
    make_flow, db_session, project_admin, httpx_client, log
):
    flow = await make_flow(status="succeeded")
    value = artifact(
        flow,
        "train.log" if log else "best.pt",
        "log" if log else "weights",
        b"safe frozen output\n",
    )
    flow.attempt.artifacts = {value["name"]: value}
    await db_session.flush()
    path = (
        f"/api/v1/projects/{flow.run.project_id}/training-runs/{flow.run.id}"
        f"/attempts/{flow.attempt.id}/artifacts/{value['name']}"
    )
    _, token = project_admin
    headers = {"Authorization": f"Bearer {token}"}
    response = await httpx_client.get(path + "/download", headers=headers)
    assert response.status_code == 200, response.text
    result = response.json()
    if log:
        assert result["requires_authentication"] is True
        assert result["url"] == path.removeprefix("/api/v1") + "/content"
        content = await httpx_client.get("/api/v1" + result["url"], headers=headers)
        assert content.status_code == 200 and content.content == b"safe frozen output\n"
        assert flow.client.calls == [("get", flow.bucket, value["key"])] * 2
    else:
        assert result["url"] and result["sha256"] == value["sha256"]
        assert flow.client.calls == [
            ("head", flow.bucket, value["key"]),
            ("sign-get_object", flow.bucket, value["key"]),
        ]


async def test_run_creation_allocates_before_its_first_attempt(
    db_session, project_admin, httpx_client, training_config
):
    owner, token = project_admin
    project, dataset = await ready_dataset(db_session, owner)
    data, _ = await create_run(httpx_client, token, project, dataset)
    run = await runs.data.get_run(db_session, project.id, uuid.UUID(data["id"]))
    attempt = await runs.data.get_attempt(db_session, run.current_attempt_id)
    assert run.storage_ref["bucket"] == dataset.storage_ref["bucket"]
    assert run.storage_ref["prefix"] == f"projects/{project.id}/runs/{run.id}/"
    assert str(attempt.id) == data["current_attempt_id"]
    assert attempt.run_id == run.id and attempt.ordinal == 1


async def parent_checkpoint(db, flow, *, invalid=None):
    run = flow.run
    if invalid == "foreign-run":
        run = await historical_run(
            db,
            flow.dataset,
            bucket="other-run-bucket",
            status="failed",
            config=run.config,
        )
    flow.attempt.ordinal = 2
    await db.flush()
    parent = TrainingAttempt(run_id=run.id, ordinal=1, status="failed")
    db.add(parent)
    await db.flush()
    key_attempt = uuid.uuid4() if invalid == "foreign-key" else parent.id
    checkpoint = {
        "role": "checkpoint",
        "key": run.storage_ref["prefix"] + f"attempts/{key_attempt}/last.pt",
        "sha256": "d" * 64,
        "size_bytes": 5,
    }
    parent.artifacts = {} if invalid == "no-checkpoint" else {"last.pt": checkpoint}
    flow.attempt.resume_from_id = parent.id
    await db.flush()
    return checkpoint


@pytest.mark.parametrize("resume", [False, True])
async def test_assignment_keeps_dataset_and_run_sources_separate(
    make_flow, db_session, resume
):
    flow = await make_flow()
    checkpoint = await parent_checkpoint(db_session, flow) if resume else None
    before = deepcopy(flow.run.config)
    result = await runs.assignment(
        db_session, flow.run, flow.attempt, flow.fence["lease_token"]
    )
    dataset_key = flow.dataset.package_key
    assert (
        result["dataset_url"]
        == f"https://owned-storage/dataset-input-bucket/{dataset_key}"
    )
    expected_calls = [("sign-get_object", "dataset-input-bucket", dataset_key)]
    if resume:
        assert result["resume"] == {
            **checkpoint,
            "url": f"https://owned-storage/{flow.bucket}/{checkpoint['key']}",
        }
        expected_calls.append(("sign-get_object", flow.bucket, checkpoint["key"]))
    else:
        assert result["resume"] is None
    assert result["config"] == flow.run.config == before
    assert flow.client.calls == expected_calls


@pytest.mark.parametrize("invalid", ["foreign-run", "foreign-key", "no-checkpoint"])
async def test_resume_cannot_sign_another_attempt_owner(make_flow, db_session, invalid):
    flow = await make_flow()
    await parent_checkpoint(db_session, flow, invalid=invalid)
    with pytest.raises(HTTPException) as error:
        await runs.assignment(
            db_session, flow.run, flow.attempt, flow.fence["lease_token"]
        )
    assert error.value.status_code == 409
    assert flow.client.calls == []


@pytest.mark.parametrize("foreign_attempt", [False, True])
async def test_assignment_initialization_uses_its_source_run_owner(
    make_flow, db_session, saved_dataset, foreign_attempt
):
    source = await historical_run(
        db_session,
        saved_dataset,
        bucket="source-model-bucket",
        status="succeeded",
        config={"framework": "ultralytics", "framework_version": "8.4.177"},
    )
    source_attempt = TrainingAttempt(run_id=source.id, ordinal=1, status="succeeded")
    db_session.add(source_attempt)
    await db_session.flush()
    source.current_attempt_id = source_attempt.id
    key_attempt = uuid.uuid4() if foreign_attempt else source_attempt.id
    weights = {
        "key": source.storage_ref["prefix"] + f"attempts/{key_attempt}/best.pt",
        "role": "weights",
        "size_bytes": 12,
        "sha256": "c" * 64,
    }
    source_attempt.artifacts = {"best.pt": weights}
    version = TrainedModelVersion(
        project_id=source.project_id,
        creator_id=source.creator_id,
        run_id=source.id,
        attempt_id=source_attempt.id,
        request_id=uuid.uuid4(),
        request_digest="a" * 64,
        name="Frozen source model",
        framework="ultralytics",
        framework_version=source.config["framework_version"],
        artifact=weights,
        classes=["car"],
        bindings=[],
        binding_digest=content_digest([]),
        provenance={},
    )
    db_session.add(version)
    await db_session.flush()
    frozen = {
        "project_id": str(source.project_id),
        "model_version_id": str(version.id),
        "run_id": str(source.id),
        "attempt_id": str(source_attempt.id),
        "source_digest": "b" * 64,
        "artifact": dict(weights),
    }
    flow = await make_flow(initialization=frozen)
    before = deepcopy(flow.run.config)
    if foreign_attempt:
        with pytest.raises(HTTPException) as error:
            await runs.assignment(
                db_session, flow.run, flow.attempt, flow.fence["lease_token"]
            )
        assert error.value.status_code == 409 and flow.client.calls == []
    else:
        result = await runs.assignment(
            db_session, flow.run, flow.attempt, flow.fence["lease_token"]
        )
        assert result["initialization"] == {
            **weights,
            "source_digest": frozen["source_digest"],
            "url": f"https://owned-storage/source-model-bucket/{weights['key']}",
        }
        assert (
            result["dataset_url"]
            == f"https://owned-storage/dataset-input-bucket/{flow.dataset.package_key}"
        )
        assert flow.client.calls == [
            ("sign-get_object", "dataset-input-bucket", flow.dataset.package_key),
            ("sign-get_object", "source-model-bucket", weights["key"]),
        ]
    assert flow.run.config == before


async def test_unknown_active_claim_replay_retains_its_gpu_lease(
    make_flow, db_session, training_config
):
    flow = await make_flow(pinned=False)
    with pytest.raises(HTTPException) as error:
        await runs.claim(
            db_session,
            flow.claim.model_copy(update={"recover_only": True}),
            training_authority=bind_training_authority(db_session),
        )
    assert (
        error.value.status_code == 409
        and error.value.detail["code"] == "training_storage_unpinned"
    )
    assert await lease_holder(db_session, flow) == flow.attempt.id
    await db_session.refresh(flow.attempt)
    assert flow.attempt.status == "training" and flow.client.calls == []


@pytest.mark.parametrize("foreign_attempt", [False, True])
async def test_unpinned_queued_run_cannot_acquire_gpu(
    make_flow, db_session, training_config, foreign_attempt
):
    flow = await make_flow(pinned=False, status="queued")
    authority = bind_training_authority(db_session)
    if foreign_attempt:
        foreign = await historical_run(
            db_session,
            flow.dataset,
            bucket=None,
            status="failed",
            config=flow.run.config,
        )
        flow.attempt.run_id = foreign.id
        with pytest.raises(HTTPException) as error:
            await runs.claim(db_session, flow.claim, training_authority=authority)
        assert error.value.status_code == 409
    else:
        assert (
            await runs.claim(db_session, flow.claim, training_authority=authority)
            is None
        )
    await db_session.refresh(flow.run)
    await db_session.refresh(flow.attempt)
    assert (
        flow.run.status
        == flow.attempt.status
        == ("queued" if foreign_attempt else "failed")
    )
    assert await lease_holder(db_session, flow) is None and flow.client.calls == []
