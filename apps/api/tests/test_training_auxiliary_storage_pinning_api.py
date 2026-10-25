"""Manager pinning checks retained auxiliary bytes without altering frozen facts."""

from copy import deepcopy
from datetime import timedelta
import hashlib
import json
import threading
import uuid

import pytest
from sqlalchemy import select

from app.db.models.active_learning import ActiveLearningAttempt, ActiveLearningCandidate
from app.db.models.sample_evaluation import (
    SampleEvaluationAttempt,
    SampleEvaluationSample,
)
from app.db.models.training_preview import TrainingPreviewResourceLease
from app.services import (
    training_auxiliary_storage_adoption as adoption,
    training_previews,
    sample_evaluations,
    sample_evaluation_storage,
    active_learning_storage,
    storage_access,
)
from app.services.storage import storage_service
from app.services.training_dataset_bridge import content_digest
from tests.sample_evaluation_fixtures import Objects, seed, evaluation
from tests.active_learning_fixtures import (
    seed as seed_learning,
    create as create_learning,
)
from tests.test_training_previews import preview, evidence
from tests.test_training_runs import training_config as training_config
from tests.test_training_storage_adoption import Store


def clone(source):
    values = {
        column.key: deepcopy(getattr(source, column.key))
        for column in source.__table__.columns
    }
    values.update(
        id=uuid.uuid4(),
        request_id=uuid.uuid4(),
        storage_ref=None,
        async_job_id=None,
        status="cancelled",
        completed_at=training_previews.now(),
    )
    if "current_attempt_id" in values:
        values.update(current_attempt_id=None, generation=1)
    return type(source)(**values)


@pytest.fixture(params=["training-preview", "sample-evaluation", "active-learning"])
async def legacy(
    request, db_session, super_admin, httpx_client, training_config, monkeypatch
):
    purpose = request.param
    actor, token = super_admin
    store = Store(storage_service.training_bucket)
    original = Objects()
    monkeypatch.setattr(storage_service, "client", original)
    if purpose == "training-preview":
        source, *_ = await preview(db_session, httpx_client, super_admin)
        row = clone(source)
        db_session.add(row)
        await db_session.flush()
        from tests.test_training_previews import Objects as PreviewObjects

        objects = PreviewObjects()
        inventory = evidence(row, objects)
        row.upload_inventory = {value["name"]: value for value in inventory}
        row.artifacts = {
            name: {**value, "key": training_previews.prefix(row) + name}
            for name, value in row.upload_inventory.items()
        }
        row.status = "succeeded"
        row.put_expires_at = training_previews.now() - timedelta(seconds=1)
        for key, (raw, metadata) in objects.objects.items():
            store.objects[key], store.metadata[key] = raw, metadata
        path = f"/api/v1/projects/{row.project_id}/training-previews/{row.id}"
    elif purpose == "sample-evaluation":
        world = await seed(db_session, actor, original)
        source, _ = await evaluation(db_session, actor, world)
        row = clone(source)
        row.spec = {**row.spec, "evaluation_id": str(row.id)}
        row.spec_digest = content_digest(row.spec)
        db_session.add(row)
        await db_session.flush()
        for frozen in row.spec["samples"]:
            db_session.add(
                SampleEvaluationSample(
                    run_id=row.id,
                    ordinal=frozen["ordinal"],
                    request_id=uuid.uuid5(row.id, f"sample:{frozen['ordinal']}"),
                )
            )
        attempt = SampleEvaluationAttempt(
            run_id=row.id,
            ordinal=1,
            generation=1,
            status="cancelled",
            claim_id=uuid.uuid4(),
            completed_at=training_previews.now(),
        )
        db_session.add(attempt)
        await db_session.flush()
        row.current_attempt_id = attempt.id
        report = sample_evaluations.build_report(
            row,
            attempt,
            await sample_evaluations.sample_rows(db_session, row),
            "cancelled",
        )
        raw = json.dumps(
            report, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode()
        sha = hashlib.sha256(raw).hexdigest()
        # Legacy fixture bytes exist before this row has a saved reference.
        key = (
            sample_evaluation_storage.prefix_for(row.project_id, row.id)
            + f"{attempt.id}/{sha}.json"
        )
        attempt.report = {"key": key, "sha256": sha, "size_bytes": len(raw)}
        store.objects[key], store.metadata[key] = (
            raw,
            sample_evaluations.report_metadata(row, attempt),
        )
        path = f"/api/v1/projects/{row.project_id}/sample-evaluations/{row.id}"
    else:
        world = await seed_learning(db_session, actor, monkeypatch)
        source, _, _ = await create_learning(db_session, actor, world)
        row = clone(source)
        row.spec = {**row.spec, "round_id": str(row.id)}
        row.spec_digest = content_digest(row.spec)
        db_session.add(row)
        await db_session.flush()
        for sample in row.spec["samples"]:
            db_session.add(
                ActiveLearningCandidate(
                    round_id=row.id,
                    ordinal=sample["ordinal"],
                    task_id=uuid.UUID(sample["task_id"]),
                    request_id=uuid.uuid5(row.id, f"candidate:{sample['ordinal']}"),
                )
            )
        attempt = ActiveLearningAttempt(
            round_id=row.id,
            ordinal=1,
            generation=1,
            status="cancelled",
            completed_at=training_previews.now(),
        )
        db_session.add(attempt)
        await db_session.flush()
        row.current_attempt_id = attempt.id
        sample = row.spec["samples"][0]
        from app.services import active_learning_originals as originals

        context = await originals.delegated(db_session, row)
        raw, _, _ = await originals.read_input(db_session, context, sample["snapshot"])
        descriptor = {
            "key": active_learning_storage.prefix_for(row.project_id, row.id)
            + f"media/{sample['ordinal']}.png",
            "sha256": sample["derived_sha256"],
        }
        member = await db_session.get(
            ActiveLearningCandidate, (row.id, sample["ordinal"])
        )
        member.media = descriptor
        store.objects[descriptor["key"]] = raw
        store.metadata[descriptor["key"]] = {
            "round-id": str(row.id),
            "ordinal": str(sample["ordinal"]),
            "source-sha256": sample["sha256"],
            "sha256": sample["derived_sha256"],
        }
        path = f"/api/v1/projects/{row.project_id}/active-learning-rounds/{row.id}"
    await db_session.flush()
    monkeypatch.setattr(adoption, "CAPACITY", threading.BoundedSemaphore(1))
    monkeypatch.setattr(
        adoption,
        "verify_operation",
        lambda owner, state: adoption.verify(store, owner, state),
    )
    return row, store, path, {"Authorization": "Bearer " + token}, purpose


async def test_get_never_pins_explicit_pin_is_idempotent_and_hides_internal_ref(
    legacy, httpx_client, db_session, monkeypatch
):
    row, store, path, headers, purpose = legacy
    before = deepcopy(row.spec)
    get = await httpx_client.get(path, headers=headers)
    assert get.status_code == 200, get.text
    assert row.storage_ref is None and not store.reads
    response = await httpx_client.post(path + "/storage-pin", headers=headers)
    assert response.status_code == 200, response.text
    await db_session.refresh(row)
    assert row.storage_ref["bucket"] == store.bucket
    assert (
        row.storage_ref["purpose"] == purpose
        and row.storage_ref["origin"] == "verified-retained"
    )
    assert "storage_ref" not in response.json() and row.spec == before
    evidence = deepcopy(row.storage_ref["evidence"])
    read_count = len(store.reads)
    again = await httpx_client.post(path + "/storage-pin", headers=headers)
    assert again.status_code == 200, again.text
    await db_session.refresh(row)
    assert row.storage_ref["evidence"] == evidence and len(store.reads) == read_count


@pytest.mark.parametrize(
    "change", ["missing", "extra", "bytes", "active", "busy", "gate"]
)
async def test_unverified_or_active_copy_is_retained_without_pin(
    legacy, change, httpx_client, db_session, monkeypatch
):
    row, store, path, headers, _ = legacy
    if change == "missing":
        store.objects.pop(next(iter(store.objects)))
    elif change == "extra":
        store.objects[next(iter(store.objects)).rsplit("/", 1)[0] + "/unexplained"] = (
            b"extra"
        )
    elif change == "bytes":
        store.objects[next(iter(store.objects))] += b"changed"
    elif change == "active":
        row.status = "lost"
    elif change == "busy":
        adoption.CAPACITY.acquire()
    else:

        async def draining(*args, **kwargs):
            raise storage_access.error("storage_maintenance")

        monkeypatch.setattr(storage_access, "acquire", draining)
    await db_session.flush()
    before = deepcopy(store.objects)
    try:
        response = await httpx_client.post(path + "/storage-pin", headers=headers)
        assert response.status_code == (429 if change == "busy" else 409), response.text
    finally:
        if change == "busy":
            adoption.CAPACITY.release()
    await db_session.refresh(row)
    assert row.storage_ref is None and store.objects == before


async def test_preview_any_cpu_lease_or_live_put_window_blocks_pin(
    legacy, httpx_client, db_session
):
    row, store, path, headers, purpose = legacy
    if purpose != "training-preview":
        pytest.skip("Preview-only leases and PUT windows")
    row.put_expires_at = training_previews.now() + timedelta(minutes=1)
    await db_session.flush()
    assert (
        await httpx_client.post(path + "/storage-pin", headers=headers)
    ).status_code == 409
    row.put_expires_at = None
    db_session.add(
        TrainingPreviewResourceLease(resource_id="legacy-other-cpu", preview_id=row.id)
    )
    await db_session.flush()
    assert (
        await httpx_client.post(path + "/storage-pin", headers=headers)
    ).status_code == 409
    assert not store.reads


async def test_final_context_mutation_refuses_publication(
    legacy, httpx_client, db_session, monkeypatch
):
    row, store, path, headers, _ = legacy
    operation = adoption.verify_operation
    original = storage_access.run

    async def changed(*args, **kwargs):
        result = await original(*args, **kwargs)
        row.error_message = "Changed while verification was settling"
        await db_session.flush()
        return result

    monkeypatch.setattr(storage_access, "run", changed)
    response = await httpx_client.post(path + "/storage-pin", headers=headers)
    assert response.status_code == 409, response.text
    await db_session.refresh(row)
    assert row.storage_ref is None and store.reads
    assert adoption.verify_operation is operation


async def test_key_scope_changed_after_verification_cannot_publish(
    legacy, httpx_client, db_session, monkeypatch
):
    from app.db.models.api_key import ApiKey

    row, store, path, headers, _ = legacy
    created = await httpx_client.post(
        "/api/v1/me/api-keys",
        headers=headers,
        json={
            "name": "Auxiliary pin",
            "scopes": ["annotations:read", "datasets:read", "datasets:write"],
        },
    )
    assert created.status_code == 201, created.text
    key = await db_session.get(ApiKey, uuid.UUID(created.json()["id"]))
    original = storage_access.run

    async def changed(*args, **kwargs):
        result = await original(*args, **kwargs)
        key.scopes = ["datasets:read"]
        await db_session.flush()
        return result

    monkeypatch.setattr(storage_access, "run", changed)
    response = await httpx_client.post(
        path + "/storage-pin",
        headers={"Authorization": "Bearer " + created.json()["plaintext"]},
    )
    assert response.status_code == 403, response.text
    await db_session.refresh(row)
    assert row.storage_ref is None and store.reads


async def test_other_project_employee_never_starts_verification(
    legacy, httpx_client, annotator
):
    row, store, path, _, _ = legacy
    response = await httpx_client.post(
        path + "/storage-pin", headers={"Authorization": "Bearer " + annotator[1]}
    )
    assert response.status_code in {403, 404}
    assert row.storage_ref is None and not store.reads


async def test_cancellation_waits_for_physical_verifier_before_releasing_capacity(
    legacy, httpx_client, db_session, monkeypatch
):
    import asyncio

    row, store, path, headers, _ = legacy
    entered, release = threading.Event(), threading.Event()

    def blocked(owner, state):
        entered.set()
        assert release.wait(timeout=10)
        return adoption.verify(store, owner, state)

    monkeypatch.setattr(adoption, "verify_operation", blocked)
    task = asyncio.create_task(
        httpx_client.post(path + "/storage-pin", headers=headers)
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
    await db_session.refresh(row)
    assert row.storage_ref is None and len(store.reads) == len(store.objects)


async def test_unknown_close_retains_capacity_and_durable_access_responsibility(
    legacy, httpx_client, db_session
):
    from app.db.models.storage_maintenance import StorageOperation

    row, store, path, headers, purpose = legacy
    store.close_failure = True
    response = await httpx_client.post(path + "/storage-pin", headers=headers)
    assert response.status_code == 503, response.text
    assert not adoption.CAPACITY.acquire(blocking=False)
    await db_session.refresh(row)
    assert row.storage_ref is None
    assert await db_session.scalar(
        select(StorageOperation.id)
        .where(
            StorageOperation.purpose == purpose.replace("-", "_") + "_storage_adoption"
        )
        .limit(1)
    )
