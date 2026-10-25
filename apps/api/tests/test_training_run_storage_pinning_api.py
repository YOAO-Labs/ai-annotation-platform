"""Stopped legacy runs adopt retained outputs explicitly under current authority."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import threading
import uuid

import pytest
from sqlalchemy import select

from app.db.models.api_key import ApiKey
from app.db.models.dataset_asset import StorageBinding
from app.db.models.training_run import (
    TrainingRun,
    TrainingAttempt,
    TrainingResourceLease,
)
from app.schemas.training_run import TrainingRunCreate
from app.services import storage_access, training_runs
from app.services import training_run_storage_adoption as adoption
from app.services.storage import storage_service
from app.services.training_dataset_bridge import content_digest
from tests.test_training_run_storage_adoption import retained as retained
from tests.test_training_runs import ready_dataset, training_config as training_config


@pytest.fixture
async def legacy_run(db_session, super_admin, retained, training_config, monkeypatch):
    store, old_owner, state = retained
    actor, token = super_admin
    project, dataset = await ready_dataset(db_session, actor)
    config = await training_runs.frozen_config(
        db_session,
        project.id,
        TrainingRunCreate(
            name="Legacy", dataset_id=dataset.id, request_id=uuid.uuid4()
        ),
    )
    run = TrainingRun(
        project_id=project.id,
        creator_id=actor.id,
        dataset_id=dataset.id,
        name="Legacy retained run",
        request_id=uuid.uuid4(),
        request_digest=content_digest(config),
        config=config,
        config_digest=content_digest(config),
        status="succeeded",
    )
    db_session.add(run)
    await db_session.flush()
    prefix = f"projects/{project.id}/runs/{run.id}/"
    store.objects = {
        key.replace(old_owner.prefix, prefix, 1): value
        for key, value in store.objects.items()
    }
    store.metadata = {
        key.replace(old_owner.prefix, prefix, 1): value
        for key, value in store.metadata.items()
    }
    for source in state["attempts"]:
        artifacts = deepcopy(source["artifacts"])
        for artifact in artifacts.values():
            artifact["key"] = artifact["key"].replace(old_owner.prefix, prefix, 1)
        attempt = TrainingAttempt(
            id=uuid.UUID(source["id"]),
            run_id=run.id,
            ordinal=state["attempts"].index(source) + 1,
            status=source["status"],
            upload_inventory=source["upload_inventory"],
            artifacts=artifacts,
        )
        db_session.add(attempt)
        await db_session.flush()
        run.current_attempt_id = attempt.id
    await db_session.flush()
    store.bucket = storage_service.training_bucket
    monkeypatch.setattr(adoption, "CAPACITY", threading.BoundedSemaphore(1))
    monkeypatch.setattr(
        adoption,
        "verify_operation",
        lambda owner, state: adoption.verify(store, owner, state),
    )
    return run, store, {"Authorization": "Bearer " + token}


async def test_unpinned_run_delete_returns_conflict_without_tombstone_or_io(
    httpx_client, db_session, legacy_run
):
    from app.db.models.training_storage_deletion import TrainingStorageDeletion

    run, store, auth = legacy_run
    response = await httpx_client.delete(
        f"/api/v1/projects/{run.project_id}/training-runs/{run.id}", headers=auth
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "training_storage_unpinned"
    assert await db_session.get(TrainingRun, run.id) is run
    assert not await db_session.scalar(
        select(TrainingStorageDeletion.id).where(
            TrainingStorageDeletion.prefix
            == f"projects/{run.project_id}/runs/{run.id}/"
        )
    )
    assert not store.reads


async def test_get_never_adopts_explicit_pin_is_idempotent_and_download_uses_saved_owner(
    httpx_client, db_session, legacy_run, monkeypatch
):
    run, store, auth = legacy_run
    path = f"/api/v1/projects/{run.project_id}/training-runs/{run.id}"
    response = await httpx_client.get(path, headers=auth)
    assert response.status_code == 200, response.text
    assert run.storage_ref is None and not store.reads
    config = deepcopy(run.config)
    saved = store.bucket
    response = await httpx_client.post(path + "/storage-pin", headers=auth)
    assert response.status_code == 200, response.text
    await db_session.refresh(run)
    assert (
        run.storage_ref["bucket"] == saved
        and run.storage_ref["origin"] == "verified-retained"
        and run.config == config
    )
    evidence = deepcopy(run.storage_ref["evidence"])
    read_count = len(store.reads)
    again = await httpx_client.post(path + "/storage-pin", headers=auth)
    assert again.status_code == 200, again.text
    await db_session.refresh(run)
    assert run.storage_ref["evidence"] == evidence and len(store.reads) == read_count
    signed = []
    monkeypatch.setattr(
        storage_service,
        "verify_upload",
        lambda key, *, bucket: bucket == saved and key in store.objects,
    )
    monkeypatch.setattr(
        storage_service,
        "generate_download_url",
        lambda key, *, bucket, **kw: (
            signed.append((key, bucket)) or "http://retained/saved"
        ),
    )
    from app.services import training_storage_io, training_dataset_storage
    from contextlib import contextmanager

    async def capture_fixture(db, owner, **kwargs):
        return await training_dataset_storage.target_for_ref(db, owner.ref, **kwargs)

    @contextmanager
    def admitted(target):
        yield storage_service

    monkeypatch.setattr(training_storage_io, "prepare", capture_fixture)
    monkeypatch.setattr(training_storage_io, "service", admitted)
    attempt = await db_session.get(TrainingAttempt, run.current_attempt_id)
    response = await httpx_client.get(
        path + f"/attempts/{attempt.id}/artifacts/best.pt/download", headers=auth
    )
    assert response.status_code == 200, response.text
    assert signed == [(attempt.artifacts["best.pt"]["key"], saved)]


@pytest.mark.parametrize(
    "change", ["missing", "bytes", "run-active", "attempt-active", "lease", "busy"]
)
async def test_missing_changed_active_or_busy_never_pins(
    httpx_client, db_session, legacy_run, change
):
    run, store, auth = legacy_run
    if change == "missing":
        store.objects.pop(next(iter(store.objects)))
    elif change == "bytes":
        store.objects[next(iter(store.objects))] += b"changed"
    elif change == "run-active":
        run.status = "lost"
    elif change == "attempt-active":
        (
            await db_session.get(TrainingAttempt, run.current_attempt_id)
        ).status = "uploading"
    elif change == "lease":
        db_session.add(
            TrainingResourceLease(
                resource_id=run.config["resource_id"], attempt_id=run.current_attempt_id
            )
        )
    else:
        adoption.CAPACITY.acquire()
    await db_session.flush()
    before = deepcopy(store.objects)
    try:
        response = await httpx_client.post(
            f"/api/v1/projects/{run.project_id}/training-runs/{run.id}/storage-pin",
            headers=auth,
        )
        assert response.status_code == (429 if change == "busy" else 409), response.text
    finally:
        if change == "busy":
            adoption.CAPACITY.release()
    await db_session.refresh(run)
    assert run.storage_ref is None and store.objects == before


@pytest.mark.parametrize("change", ["binding", "attempt", "config", "gate"])
async def test_context_change_after_io_never_publishes_the_previous_result(
    httpx_client, db_session, legacy_run, monkeypatch, change
):
    run, store, auth = legacy_run
    original = storage_access.run

    async def changed(*args, **kwargs):
        result = await original(*args, **kwargs)
        if change == "binding":
            binding = await db_session.scalar(
                select(StorageBinding).where(StorageBinding.purpose == "training")
            )
            binding.bucket = "changed-persisted-binding"
        elif change == "attempt":
            attempt = await db_session.get(TrainingAttempt, run.current_attempt_id)
            attempt.finish_request_digest = "f" * 64
        elif change == "config":
            run.config = {**run.config, "changed": True}
        else:
            from app.db.models.storage_maintenance import StorageMaintenanceGate

            gate = await db_session.get(
                StorageMaintenanceGate,
                uuid.UUID(args[1] if isinstance(args[1], str) else str(args[1])),
            )
            gate.revision += 1
        await db_session.flush()
        return result

    monkeypatch.setattr(storage_access, "run", changed)
    response = await httpx_client.post(
        f"/api/v1/projects/{run.project_id}/training-runs/{run.id}/storage-pin",
        headers=auth,
    )
    assert response.status_code == (200 if change == "binding" else 409), response.text
    await db_session.refresh(run)
    assert store.reads
    if change == "binding":
        assert run.storage_ref["bucket"] == store.bucket
    else:
        assert run.storage_ref is None


@pytest.mark.parametrize("change", ["revoke", "rotate", "scope", "expire"])
async def test_api_key_changed_during_verification_cannot_pin(
    httpx_client, db_session, legacy_run, monkeypatch, change
):
    run, store, auth = legacy_run
    created = await httpx_client.post(
        "/api/v1/me/api-keys",
        headers=auth,
        json={
            "name": "Run pin",
            "scopes": ["annotations:read", "datasets:read", "datasets:write"],
        },
    )
    assert created.status_code == 201, created.text
    key = await db_session.get(ApiKey, uuid.UUID(created.json()["id"]))
    original = storage_access.run

    async def changed(*args, **kwargs):
        result = await original(*args, **kwargs)
        if change == "revoke":
            key.revoked_at = datetime.now(timezone.utc)
        elif change == "rotate":
            key.key_hash = "rotated-key"
        elif change == "scope":
            key.scopes = ["datasets:read"]
        else:
            key.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db_session.flush()
        return result

    monkeypatch.setattr(storage_access, "run", changed)
    response = await httpx_client.post(
        f"/api/v1/projects/{run.project_id}/training-runs/{run.id}/storage-pin",
        headers={"Authorization": "Bearer " + created.json()["plaintext"]},
    )
    assert response.status_code == 403, response.text
    await db_session.refresh(run)
    assert run.storage_ref is None and store.reads


async def test_employee_never_reads_another_project_run(
    httpx_client, legacy_run, annotator
):
    run, store, _ = legacy_run
    response = await httpx_client.post(
        f"/api/v1/projects/{run.project_id}/training-runs/{run.id}/storage-pin",
        headers={"Authorization": "Bearer " + annotator[1]},
    )
    assert (
        response.status_code in {403, 404}
        and not store.reads
        and run.storage_ref is None
    )


async def test_cancellation_waits_for_real_verifier_before_releasing_capacity(
    httpx_client, db_session, legacy_run, monkeypatch
):
    import asyncio

    run, store, auth = legacy_run
    entered, release = threading.Event(), threading.Event()

    def blocked(owner, state):
        entered.set()
        assert release.wait(timeout=10)
        return adoption.verify(store, owner, state)

    monkeypatch.setattr(adoption, "verify_operation", blocked)
    task = asyncio.create_task(
        httpx_client.post(
            f"/api/v1/projects/{run.project_id}/training-runs/{run.id}/storage-pin",
            headers=auth,
        )
    )
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait), timeout=3)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not adoption.CAPACITY.acquire(blocking=False)
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert adoption.CAPACITY.acquire(blocking=False)
    adoption.CAPACITY.release()
    await db_session.refresh(run)
    assert run.storage_ref is None and len(store.reads) == len(store.objects)


async def test_unknown_physical_close_retains_capacity_and_durable_responsibility(
    httpx_client, db_session, legacy_run, monkeypatch
):
    from app.db.models.storage_maintenance import StorageOperation

    run, store, auth = legacy_run
    store.close_failure = True
    response = await httpx_client.post(
        f"/api/v1/projects/{run.project_id}/training-runs/{run.id}/storage-pin",
        headers=auth,
    )
    assert response.status_code == 503, response.text
    assert not adoption.CAPACITY.acquire(blocking=False)
    await db_session.refresh(run)
    assert run.storage_ref is None
    assert await db_session.scalar(
        select(StorageOperation.id)
        .where(StorageOperation.purpose == "training_run_storage_adoption")
        .limit(1)
    )
