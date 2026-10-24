"""Frozen membership, executor capability and fenced success use one manifest."""

from copy import deepcopy
import hashlib
import io
import json
from types import SimpleNamespace
import uuid

from fastapi import HTTPException
import pytest
from training_contract.catalog import digest
from training_contract.input_membership import (
    POLICY_DIGEST,
    compact_members,
    contract_for,
    member_records,
    receipt_for,
)

from app.db.models.training_run import (
    TrainingAttempt,
    TrainingResourceLease,
    TrainingRun,
)
from app.schemas.training_run import ExecutorFinish
from app.services import training_artifacts, training_runs as service
from tests.test_training_runs import (
    GPU,
    create_run,
    executor_claim,
    fence,
    ready_dataset,
    training_config,  # noqa: F401 -- shared fixture
)

pytestmark = pytest.mark.usefixtures("training_config")


def samples(count=3):
    result = []
    for index in range(count):
        split = "val" if index == count - 1 else "train"
        result.append(
            {
                "task_id": str(uuid.UUID(int=index + 1)),
                "split": split,
                "image_path": f"images/{split}/{index}.png",
                "label_path": f"labels/{split}/{index}.txt",
                "sha256": "a" * 64,
                "label_sha256": "b" * 64,
                "width": 32,
                "height": 32,
                "size_bytes": 100,
            }
        )
    return result


async def frozen_run(db, client, owner, token):
    project, dataset = await ready_dataset(db, owner)
    dataset.manifest = {**dataset.manifest, "samples": samples()}
    await db.flush()
    created, payload = await create_run(client, token, project, dataset)
    run = await db.get(TrainingRun, uuid.UUID(created["id"]))
    return project, dataset, run, payload


async def test_new_run_freezes_members_and_old_image_cannot_claim_or_replay(
    db_session, super_admin, httpx_client
):
    owner, token = super_admin
    project, dataset, run, payload = await frozen_run(
        db_session, httpx_client, owner, token
    )
    assert run.config["input_contract"] == contract_for(dataset.manifest)
    assert run.config_digest == digest(run.config)
    claim = executor_claim()
    assert not service.runtime_compatible(run.config, claim.runtime)
    assert await service.claim(db_session, claim) is None
    claim.runtime["input_receipt_policy_digest"] = POLICY_DIGEST
    assignment = await service.claim(db_session, claim)
    assert assignment["run_id"] == str(run.id)
    assert (await service.claim(db_session, claim)) == assignment
    # Existing leases can recover publication without requalifying startup inputs.
    claim.runtime["image_digest"] = "sha256:" + "f" * 64
    assert (await service.claim(db_session, claim)) == assignment
    claim.runtime.pop("input_receipt_policy_digest")
    with pytest.raises(HTTPException) as rejected:
        await service.claim(db_session, claim)
    assert rejected.value.status_code == 409
    dataset.manifest = {**dataset.manifest, "samples": []}
    await db_session.flush()
    repeated = await httpx_client.post(
        f"/api/v1/projects/{project.id}/training-runs",
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
    )
    assert repeated.status_code == 202, repeated.text
    assert repeated.json()["config_digest"] == run.config_digest


@pytest.mark.parametrize("bad_samples", [None, [], [{"split": "train"}]])
async def test_explicit_bad_member_manifest_cannot_create_new_run(
    db_session, super_admin, httpx_client, bad_samples
):
    owner, token = super_admin
    project, dataset = await ready_dataset(db_session, owner)
    dataset.manifest = {**dataset.manifest, "samples": bad_samples}
    await db_session.flush()
    response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/training-runs:preflight",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "name": "Invalid",
            "dataset_id": str(dataset.id),
            "parameters": {"epochs": 3},
        },
    )
    assert response.status_code == 422, response.text


def stored_manifest(monkeypatch, run, attempt, manifest):
    raw = json.dumps(manifest, ensure_ascii=False).encode()
    calls, bodies = [], []

    def get_object(**kwargs):
        calls.append(kwargs)
        body = io.BytesIO(raw)
        bodies.append(body)
        return {
            "Body": body,
            "Metadata": {"attempt-id": str(attempt.id)},
            "ContentLength": len(raw),
        }

    monkeypatch.setattr(training_artifacts.storage.client, "get_object", get_object)
    owner = service.attempt_storage_owner(run, attempt)
    key = service.artifact_key(owner, attempt.id, "manifest.json")
    spec = {
        "name": "manifest.json",
        "role": "manifest",
        "key": key,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
    }
    return spec, calls, bodies, owner


def execution_manifest(run, attempt, manifest):
    return {
        "run_id": str(run.id),
        "attempt_id": str(attempt.id),
        "config_digest": run.config_digest,
        "config": deepcopy(run.config),
        "input_membership": receipt_for(
            run.config, run.id, attempt.id, compact_members(member_records(manifest))
        ),
    }


@pytest.mark.parametrize(
    "change", ["missing", "attempt", "config", "member", "split", "role", None]
)
async def test_success_verifies_saved_owner_manifest_before_terminal_and_replay(
    db_session, super_admin, httpx_client, monkeypatch, change
):
    owner, token = super_admin
    _, dataset, run, _ = await frozen_run(db_session, httpx_client, owner, token)
    claim = executor_claim()
    claim.runtime["input_receipt_policy_digest"] = POLICY_DIGEST
    assignment = await service.claim(db_session, claim)
    attempt = await db_session.get(TrainingAttempt, run.current_attempt_id)
    manifest = execution_manifest(run, attempt, dataset.manifest)
    if change == "missing":
        manifest.pop("input_membership")
    elif change == "attempt":
        manifest["input_membership"]["attempt_id"] = str(uuid.uuid4())
    elif change == "config":
        manifest["config"]["parameters"]["epochs"] += 1
    elif change == "member":
        manifest["input_membership"]["members"]["train"].pop()
    elif change == "split":
        groups = manifest["input_membership"]["members"]
        groups["train"][0], groups["val"][0] = groups["val"][0], groups["train"][0]
    spec, calls, bodies, saved_owner = stored_manifest(
        monkeypatch, run, attempt, manifest
    )
    monkeypatch.setattr(
        service.storage, "training_bucket", "different-current-training"
    )
    if change == "role":
        spec["role"] = "log"
    attempt.upload_inventory = {
        "manifest.json": spec,
        **{
            f"artifact-{role}": {
                "name": f"artifact-{role}",
                "role": role,
                "sha256": "d" * 64,
                "size_bytes": 3,
            }
            for role in ("weights", "checkpoint", "metrics", "config", "log")
        },
    }
    original_verify = service.verify_artifact
    monkeypatch.setattr(service, "verify_artifact", lambda *args, **kwargs: None)
    body = ExecutorFinish(
        **fence(claim, assignment), status="succeeded", process_exited=True
    )
    if change is not None:
        with pytest.raises(HTTPException) as rejected:
            await service.finish(db_session, attempt.id, body)
        assert rejected.value.status_code == 422
        assert attempt.status == "preparing" and attempt.finish_request_digest is None
        assert (
            await db_session.get(TrainingResourceLease, "test/" + GPU)
        ).attempt_id == attempt.id
        body = ExecutorFinish(
            **fence(claim, assignment), status="failed", process_exited=True
        )
    finished = await service.finish(db_session, attempt.id, body)
    assert finished.status == ("failed" if change else "succeeded")
    assert (
        await db_session.get(TrainingResourceLease, "test/" + GPU)
    ).attempt_id is None
    reads = len(calls)
    monkeypatch.setattr(service, "verify_artifact", original_verify)
    assert await service.finish(db_session, attempt.id, body) is finished
    assert len(calls) == reads
    assert all(call["Bucket"] == saved_owner.bucket for call in calls)
    assert all(body.closed for body in bodies)


def test_twenty_thousand_members_fit_existing_verified_report_reader(monkeypatch):
    manifest = {"samples": samples(20000)}
    config = {
        "dataset_id": str(uuid.uuid4()),
        "dataset_sha256": "e" * 64,
        "input_contract": contract_for(manifest),
    }
    run_id, attempt_id = uuid.uuid4(), uuid.uuid4()
    run = SimpleNamespace(id=run_id, config=config, config_digest=digest(config))
    attempt = SimpleNamespace(id=attempt_id)
    raw = json.dumps(execution_manifest(run, attempt, manifest)).encode()
    bodies = []

    def get_object(**kwargs):
        assert kwargs == {"Bucket": "saved-owner", "Key": "manifest.json"}
        body = io.BytesIO(raw)
        bodies.append(body)
        return {"Body": body, "Metadata": {"attempt-id": str(attempt_id)}}

    monkeypatch.setattr(training_artifacts.storage.client, "get_object", get_object)
    spec = {
        "key": "manifest.json",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
    }
    assert len(raw) < training_artifacts.MAX_REPORT_BYTES
    first = training_artifacts.read_json_artifact(
        spec, attempt_id, bucket="saved-owner"
    )
    second = training_artifacts.read_json_artifact(
        spec, attempt_id, bucket="saved-owner"
    )
    assert first == second and len(bodies) == 2 and all(body.closed for body in bodies)
    assert len(first["input_membership"]["members"]["train"]) == 19999


@pytest.mark.parametrize("cancel_requested", [False, True])
async def test_stopped_new_contract_releases_lease_without_input_receipt(
    db_session, super_admin, httpx_client, monkeypatch, cancel_requested
):
    owner, token = super_admin
    _, _, run, _ = await frozen_run(db_session, httpx_client, owner, token)
    claim = executor_claim()
    claim.runtime["input_receipt_policy_digest"] = POLICY_DIGEST
    assignment = await service.claim(db_session, claim)
    if cancel_requested:
        run.status = "cancelling"
    attempt = await db_session.get(TrainingAttempt, run.current_attempt_id)

    def unexpected(*args, **kwargs):
        raise AssertionError("Stopped execution must not require a success receipt")

    monkeypatch.setattr(service, "verify_input_artifact", unexpected)
    result = await service.finish(
        db_session,
        attempt.id,
        ExecutorFinish(
            **fence(claim, assignment),
            process_exited=True,
            status="succeeded" if cancel_requested else "failed",
        ),
    )
    assert result.status == ("cancelled" if cancel_requested else "failed")
    assert (
        await db_session.get(TrainingResourceLease, "test/" + GPU)
    ).attempt_id is None


async def test_resume_keeps_contract_but_requires_new_attempt_observation(
    db_session, super_admin, httpx_client, monkeypatch
):
    owner, token = super_admin
    project, dataset, run, _ = await frozen_run(db_session, httpx_client, owner, token)
    claim = executor_claim()
    claim.runtime["input_receipt_policy_digest"] = POLICY_DIGEST
    assignment = await service.claim(db_session, claim)
    old = await db_session.get(TrainingAttempt, run.current_attempt_id)
    old_receipt = execution_manifest(run, old, dataset.manifest)["input_membership"]
    config = deepcopy(run.config)
    await service.finish(
        db_session,
        old.id,
        ExecutorFinish(
            **fence(claim, assignment),
            status="failed",
            process_exited=True,
        ),
    )
    saved_owner = service.attempt_storage_owner(run, old)
    old.artifacts = {
        "checkpoint.pt": {
            "role": "checkpoint",
            "epoch": 1,
            "key": service.artifact_key(saved_owner, old.id, "checkpoint.pt"),
            "sha256": "1" * 64,
            "size_bytes": 10,
        }
    }
    run.metrics = {"epoch": 1}
    await db_session.flush()
    monkeypatch.setattr(
        service.storage.client,
        "head_object",
        lambda **kwargs: {
            "ContentLength": 10,
            "Metadata": {"attempt-id": str(old.id), "sha256": "1" * 64},
        },
    )
    response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/training-runs/{run.id}/resume",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 202, response.text
    assert run.config == config and run.current_attempt_id != old.id
    assignment = await service.claim(db_session, claim)
    current = await db_session.get(TrainingAttempt, run.current_attempt_id)
    assert assignment["config"] == config and assignment["attempt_id"] == str(
        current.id
    )
    manifest = execution_manifest(run, current, dataset.manifest)
    manifest["input_membership"] = old_receipt
    spec, _, _, _ = stored_manifest(monkeypatch, run, current, manifest)
    with pytest.raises(ValueError, match="identity"):
        service.verify_input_artifact(run, current, {"manifest.json": spec})
    manifest["input_membership"] = execution_manifest(run, current, dataset.manifest)[
        "input_membership"
    ]
    spec, _, _, _ = stored_manifest(monkeypatch, run, current, manifest)
    service.verify_input_artifact(run, current, {"manifest.json": spec})
