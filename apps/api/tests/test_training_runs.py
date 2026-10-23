"""Managed training authority, exclusive claims, fences and durable artifacts."""

import hashlib
import io
import uuid
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.config import settings
from app.db.models.training_dataset import TrainingDatasetVersion
from app.db.models.training_run import (
    TrainingAttempt,
    TrainingEvent,
    TrainingResourceLease,
    TrainingRun,
)
from app.schemas.training_run import (
    ExecutorClaim,
    ExecutorFinish,
    ExecutorHeartbeat,
    ExecutorUpload,
)
from app.services import training_runs as service
from app.services.storage import StorageService
from tests.factory import create_membership, create_project

TOKEN = "test-executor-credential-" + "a" * 40
GPU = "GPU-11111111-2222-3333-4444-555555555555"
IMAGE = "sha256:" + "1" * 64


@pytest.fixture
def training_config(monkeypatch):
    monkeypatch.setattr(settings, "training_enabled", True)
    monkeypatch.setattr(settings, "training_executor_token", TOKEN)
    monkeypatch.setattr(settings, "training_gpu_uuid", GPU)
    monkeypatch.setattr(settings, "training_node_id", "test")
    monkeypatch.setattr(
        settings,
        "training_catalog_json",
        {
            "ultralytics": {
                "version": "8.4.177",
                "image_digest": IMAGE,
                "base_models": {
                    "yolo11n": {"sha256": "2" * 64, "filename": "yolo11n.pt"}
                },
            }
        },
    )
    monkeypatch.setattr(
        service,
        "internal_download",
        lambda key, **kwargs: "http://owned-storage/" + key,
    )


async def ready_dataset(db, owner):
    project = await create_project(db, owner_id=owner.id)
    dataset = TrainingDatasetVersion(
        project_id=project.id,
        creator_id=owner.id,
        request_id=uuid.uuid4(),
        request_digest="a" * 64,
        snapshot_digest="b" * 64,
        name="Frozen",
        snapshot={},
        status="ready",
        manifest={
            "schema": 1,
            "content_digest": "c" * 64,
            "validation_digest": "d" * 64,
            "classes": ["car"],
        },
        package_key="owned/dataset.zip",
        package_sha256="e" * 64,
    )
    db.add(dataset)
    await db.flush()
    from app.services.training_dataset_storage import allocate

    ref = await allocate(db, dataset)
    dataset.package_key = ref["prefix"] + "dataset.zip"
    return project, dataset


async def create_run(client, owner_token, project, dataset, **extra):
    payload = {
        "name": "Round one",
        "dataset_id": str(dataset.id),
        "request_id": str(uuid.uuid4()),
        "parameters": {"epochs": 3},
        **extra,
    }
    preflight = await client.post(
        f"/api/v1/projects/{project.id}/training-runs:preflight",
        headers={"Authorization": f"Bearer {owner_token}"},
        json=payload,
    )
    assert preflight.status_code == 200, preflight.text
    payload["preflight_digest"] = preflight.json()["config_digest"]
    response = await client.post(
        f"/api/v1/projects/{project.id}/training-runs",
        headers={"Authorization": f"Bearer {owner_token}"},
        json=payload,
    )
    assert response.status_code == 202, response.text
    return response.json(), payload


def executor_claim():
    return ExecutorClaim(
        executor_id="owned-executor",
        boot_id=uuid.uuid4(),
        claim_id=uuid.uuid4(),
        resource_id="test/" + GPU,
        runtime={
            "image_digest": IMAGE,
            "gpu_uuid": GPU,
            "frameworks": {"ultralytics": "8.4.177"},
        },
    )


def fence(claim, assignment):
    return {
        "executor_id": claim.executor_id,
        "boot_id": claim.boot_id,
        "lease_token": assignment["lease_token"],
    }


async def claimed_run(db, client, owner_token, project, dataset):
    data, _ = await create_run(client, owner_token, project, dataset)
    claim = executor_claim()
    assignment = await service.claim(db, claim)
    assert assignment["run_id"] == data["id"]
    await db.flush()
    return await db.get(TrainingRun, uuid.UUID(data["id"])), claim, assignment


async def test_manager_creation_is_frozen_idempotent_and_blocks_dataset_deletion(
    db_session, super_admin, httpx_client, training_config, monkeypatch
):
    owner, token = super_admin
    project, dataset = await ready_dataset(db_session, owner)
    data, payload = await create_run(httpx_client, token, project, dataset)
    path = f"/api/v1/projects/{project.id}/training-runs"
    monkeypatch.setattr(settings, "training_catalog_json", {})
    repeated = await httpx_client.post(
        path, headers={"Authorization": f"Bearer {token}"}, json=payload
    )
    assert repeated.status_code == 202 and repeated.json()["id"] == data["id"]
    assert repeated.json()["config"] == data["config"]
    changed = await httpx_client.post(
        path,
        headers={"Authorization": f"Bearer {token}"},
        json={**payload, "name": "Changed"},
    )
    assert changed.status_code == 409
    deleted = await httpx_client.delete(
        f"/api/v1/projects/{project.id}/training-datasets/{dataset.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert deleted.status_code == 409
    assert await db_session.get(TrainingDatasetVersion, dataset.id)


async def test_members_and_unauthenticated_executor_cannot_run_training(
    db_session, super_admin, annotator, httpx_client, training_config
):
    owner, _ = super_admin
    member, member_token = annotator
    project, dataset = await ready_dataset(db_session, owner)
    await create_membership(
        db_session, project_id=project.id, user_id=member.id, role="annotator"
    )
    response = await httpx_client.get(
        f"/api/v1/projects/{project.id}/training-runs",
        headers={"Authorization": f"Bearer {member_token}"},
    )
    assert response.status_code == 403
    response = await httpx_client.post(
        "/api/v1/internal/training/claim", json=executor_claim().model_dump(mode="json")
    )
    assert response.status_code == 403


async def test_claim_response_replay_is_identical_and_resource_stays_exclusive(
    db_session, super_admin, httpx_client, training_config
):
    owner, token = super_admin
    project, dataset = await ready_dataset(db_session, owner)
    run, claim, assignment = await claimed_run(
        db_session, httpx_client, token, project, dataset
    )
    replay = await service.claim(db_session, claim)
    assert replay == assignment
    assert await service.claim(db_session, executor_claim()) is None
    assert (
        await db_session.get(TrainingResourceLease, "test/" + GPU)
    ).attempt_id == run.current_attempt_id
    unknown = executor_claim().model_copy(update={"recover_only": True})
    assert await service.claim(db_session, unknown) is None


async def test_events_replay_identically_and_reject_changed_or_skipped_sequence(
    db_session, super_admin, httpx_client, training_config
):
    owner, token = super_admin
    project, dataset = await ready_dataset(db_session, owner)
    run, claim, assignment = await claimed_run(
        db_session, httpx_client, token, project, dataset
    )
    event = {
        "sequence": 1,
        "kind": "epoch",
        "epoch": 1,
        "metrics": {"metrics/mAP50(B)": 0.1},
    }
    body = ExecutorHeartbeat(**fence(claim, assignment), events=[event])
    assert not (await service.heartbeat(db_session, run.current_attempt_id, body))[
        "stop"
    ]
    await service.heartbeat(db_session, run.current_attempt_id, body)
    assert len((await db_session.scalars(select(TrainingEvent))).all()) == 1
    for bad in [{**event, "epoch": 2}, {**event, "sequence": 3}]:
        with pytest.raises(HTTPException) as error:
            await service.heartbeat(
                db_session,
                run.current_attempt_id,
                ExecutorHeartbeat(**fence(claim, assignment), events=[bad]),
            )
        assert error.value.status_code == 409


async def test_cancel_retains_lease_until_exit_and_completion_ack_replays(
    db_session, super_admin, httpx_client, training_config
):
    owner, token = super_admin
    project, dataset = await ready_dataset(db_session, owner)
    run, claim, assignment = await claimed_run(
        db_session, httpx_client, token, project, dataset
    )
    response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/training-runs/{run.id}/cancel",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200 and response.json()["status"] == "cancelling"
    lease = await db_session.get(TrainingResourceLease, "test/" + GPU)
    assert lease.attempt_id == run.current_attempt_id
    attempt = await db_session.get(TrainingAttempt, run.current_attempt_id)
    attempt.heartbeat_at = service.now() - timedelta(hours=1)
    await db_session.flush()
    await service.observe_lost(db_session, lease)
    assert run.status == "cancelling"
    body = ExecutorFinish(
        **fence(claim, assignment), status="succeeded", process_exited=True
    )
    done = await service.finish(db_session, attempt.id, body)
    assert done.status == "cancelled" and lease.attempt_id is None
    repeated = await service.finish(db_session, attempt.id, body)
    assert repeated.status == "cancelled"


async def test_lost_heartbeat_does_not_release_gpu_or_allow_new_claim(
    db_session, super_admin, httpx_client, training_config
):
    owner, token = super_admin
    project, dataset = await ready_dataset(db_session, owner)
    run, claim, assignment = await claimed_run(
        db_session, httpx_client, token, project, dataset
    )
    attempt = await db_session.get(TrainingAttempt, run.current_attempt_id)
    attempt.heartbeat_at = service.now() - timedelta(hours=1)
    await db_session.flush()
    assert await service.claim(db_session, executor_claim()) is None
    assert run.status == "lost"
    lease = await db_session.get(TrainingResourceLease, "test/" + GPU)
    assert lease.attempt_id == attempt.id
    result = await service.heartbeat(
        db_session,
        attempt.id,
        ExecutorHeartbeat(
            **fence(claim, assignment),
            events=[
                {
                    "sequence": 1,
                    "kind": "epoch",
                    "epoch": 1,
                    "metrics": {"metrics/mAP50(B)": 0.5},
                }
            ],
        ),
    )
    assert result == {"stop": True, "reason": "heartbeat_expired"}
    assert run.status == "lost"
    assert run.metrics["epoch"] == 1
    with pytest.raises(HTTPException) as error:
        await service.fenced(
            db_session,
            attempt.id,
            ExecutorHeartbeat(**{**fence(claim, assignment), "boot_id": uuid.uuid4()}),
        )
    assert error.value.status_code == 409


async def test_upload_inventory_is_immutable_and_reports_uploading(
    db_session, super_admin, httpx_client, training_config, monkeypatch
):
    owner, token = super_admin
    project, dataset = await ready_dataset(db_session, owner)
    run, claim, assignment = await claimed_run(
        db_session, httpx_client, token, project, dataset
    )
    attempt = await db_session.get(TrainingAttempt, run.current_attempt_id)
    monkeypatch.setattr(
        service.storage.client,
        "generate_presigned_url",
        lambda *args, **kwargs: "https://storage.test/signed",
    )
    artifact = {"name": "train.log", "role": "log", "sha256": "a" * 64, "size_bytes": 1}
    request = ExecutorUpload(**fence(claim, assignment), artifacts=[artifact])
    first = await service.authorize_upload(db_session, attempt.id, request)
    assert run.status == attempt.status == "uploading"
    assert first == await service.authorize_upload(db_session, attempt.id, request)
    run.status = attempt.status = "cancelling"
    await service.authorize_upload(db_session, attempt.id, request)
    assert run.status == "cancelling"
    with pytest.raises(HTTPException) as changed:
        await service.authorize_upload(
            db_session,
            attempt.id,
            ExecutorUpload(
                **fence(claim, assignment), artifacts=[{**artifact, "sha256": "b" * 64}]
            ),
        )
    assert changed.value.status_code == 409


async def test_project_busy_is_retryable_and_not_revocation(
    db_session, super_admin, httpx_client, training_config, monkeypatch
):
    owner, token = super_admin
    project, dataset = await ready_dataset(db_session, owner)
    data, _ = await create_run(httpx_client, token, project, dataset)

    async def busy(*args):
        raise HTTPException(status_code=409, detail="project_busy")

    original = service.authority
    monkeypatch.setattr(service, "authority", busy)
    with pytest.raises(HTTPException) as error:
        await service.claim(db_session, executor_claim())
    assert error.value.status_code == 409
    run = await db_session.get(TrainingRun, uuid.UUID(data["id"]))
    assert run.status == "queued"
    monkeypatch.setattr(service, "authority", original)
    claim = executor_claim()
    assignment = await service.claim(db_session, claim)
    monkeypatch.setattr(service, "authority", busy)
    with pytest.raises(HTTPException) as error:
        await service.heartbeat(
            db_session,
            run.current_attempt_id,
            ExecutorHeartbeat(**fence(claim, assignment)),
        )
    assert error.value.status_code == 409 and run.status == "preparing"


async def test_resume_requires_committed_checkpoint_and_fences_old_attempt(
    db_session, super_admin, httpx_client, training_config, monkeypatch
):
    owner, token = super_admin
    project, dataset = await ready_dataset(db_session, owner)
    run, claim, assignment = await claimed_run(
        db_session, httpx_client, token, project, dataset
    )
    old_id = run.current_attempt_id
    attempt = await service.finish(
        db_session,
        old_id,
        ExecutorFinish(
            **fence(claim, assignment), status="failed", process_exited=True
        ),
    )
    path = f"/api/v1/projects/{project.id}/training-runs/{run.id}/resume"
    headers = {"Authorization": f"Bearer {token}"}
    assert (await httpx_client.post(path, headers=headers)).status_code == 409
    attempt.artifacts = {
        "checkpoint.pt": {
            "role": "checkpoint",
            "epoch": 1,
            "key": f"projects/{project.id}/runs/{run.id}/attempts/{attempt.id}/checkpoint.pt",
            "sha256": "1" * 64,
            "size_bytes": 10,
        }
    }
    await db_session.flush()
    monkeypatch.setattr(
        service.storage.client,
        "head_object",
        lambda **kwargs: {
            "ContentLength": 10,
            "Metadata": {"attempt-id": str(attempt.id), "sha256": "1" * 64},
        },
    )
    run.metrics = {"epoch": 1}
    await db_session.flush()
    response = await httpx_client.post(path, headers=headers)
    assert response.status_code == 202, response.text
    assert response.json()["config_digest"] == run.config_digest
    assert response.json()["current_attempt_id"] != str(old_id)
    assert response.json()["metrics"] == {}
    with pytest.raises(HTTPException) as error:
        await service.heartbeat(
            db_session, old_id, ExecutorHeartbeat(**fence(claim, assignment))
        )
    assert error.value.status_code == 409


async def test_artifact_digest_failure_cannot_publish_success_and_failure_frees_lease(
    db_session, super_admin, httpx_client, training_config, monkeypatch
):
    owner, token = super_admin
    project, dataset = await ready_dataset(db_session, owner)
    run, claim, assignment = await claimed_run(
        db_session, httpx_client, token, project, dataset
    )
    attempt = await db_session.get(TrainingAttempt, run.current_attempt_id)
    attempt.upload_inventory = {
        "best.pt": {
            "name": "best.pt",
            "role": "weights",
            "sha256": "0" * 64,
            "size_bytes": 3,
        }
    }

    def invalid(*args):
        raise ValueError("Artifact mismatch")

    monkeypatch.setattr(service, "verify_artifact", invalid)
    with pytest.raises(HTTPException) as error:
        await service.finish(
            db_session,
            attempt.id,
            ExecutorFinish(
                **fence(claim, assignment), status="succeeded", process_exited=True
            ),
        )
    assert error.value.status_code == 422 and run.status == "preparing"
    failed = await service.finish(
        db_session,
        attempt.id,
        ExecutorFinish(
            **fence(claim, assignment), status="failed", process_exited=True
        ),
    )
    assert failed.artifacts == {} and failed.status == "failed"
    assert (
        await db_session.get(TrainingResourceLease, "test/" + GPU)
    ).attempt_id is None


def test_real_artifact_stream_digest_and_partial_delete_errors(monkeypatch):
    class Client:
        def get_object(self, **kwargs):
            return {
                "ContentLength": 3,
                "Metadata": {"attempt-id": str(attempt_id)},
                "Body": io.BytesIO(b"abc"),
            }

        def list_objects_v2(self, **kwargs):
            return {"Contents": [{"Key": "owned/a"}]}

        def delete_objects(self, **kwargs):
            return {"Errors": [{"Key": "owned/a", "Code": "AccessDenied"}]}

    attempt_id = uuid.uuid4()
    client = Client()
    monkeypatch.setattr(service.storage, "client", client)
    service.verify_artifact(
        "owned/a",
        {"sha256": hashlib.sha256(b"abc").hexdigest(), "size_bytes": 3},
        attempt_id,
    )
    with pytest.raises(ValueError):
        service.verify_artifact(
            "owned/a", {"sha256": "0" * 64, "size_bytes": 3}, attempt_id
        )
    storage = StorageService.__new__(StorageService)
    storage.client, storage.bucket = client, "owned"
    with pytest.raises(RuntimeError, match="未能删除"):
        storage.delete_prefix("owned")


async def test_libreyolo_catalog_freezes_native_options_and_return_capability(
    db_session, super_admin, httpx_client, training_config, monkeypatch
):
    owner, token = super_admin
    project, dataset = await ready_dataset(db_session, owner)
    catalog = dict(settings.training_catalog_json)
    catalog["libreyolo"] = {
        "version": "1.6.0",
        "image_digest": IMAGE,
        "base_models": {
            "LibreYOLO9t": {"sha256": "3" * 64, "filename": "LibreYOLO9t.pt"}
        },
    }
    monkeypatch.setattr(settings, "training_catalog_json", catalog)
    options = service.catalog()["items"]
    libre = next(x for x in options if x["framework"] == "libreyolo")
    assert (
        libre["capabilities"]["model_registration"] is True
        and libre["capabilities"]["resume"] is True
    )
    run, _ = await create_run(
        httpx_client,
        token,
        project,
        dataset,
        framework="libreyolo",
        base_model="LibreYOLO9t",
    )
    assert run["config"]["framework_version"] == "1.6.0"
    assert run["config"]["fixed_options"]["device"] == "cuda:0"
    assert run["config"]["fixed_options"]["eval_interval"] == 1
    assert run["config"]["base_model"]["sha256"] == "3" * 64
    monkeypatch.setattr(
        settings, "training_catalog_json", {"ultralytics": catalog["ultralytics"]}
    )
    assert [x["framework"] for x in service.catalog()["items"]] == ["ultralytics"]
    retained = await httpx_client.get(
        f"/api/v1/projects/{project.id}/training-runs/{run['id']}",
        headers={"Authorization": "Bearer " + token},
    )
    assert (
        retained.status_code == 200
        and retained.json()["run"]["config"] == run["config"]
    )
    assert retained.json()["registration_eligibility"]["qualified"] is True
    assert (
        retained.json()["registration_eligibility"]["qualification"]["model_size"]
        == "t"
    )
