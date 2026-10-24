"""Synchronous image consumers retain original identity and current authority."""

import asyncio
import hashlib
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import urlsplit
import uuid

from aap_protocol_v2 import (
    CocoRlePayload,
    NativeMaskCandidate,
    NativeMaskCandidateValue,
    encode_low_res_mask,
    native_mask_candidate_id,
)
from fastapi import HTTPException
import numpy as np
import pytest
from sqlalchemy import func, select

from app.config import settings
from app.db.models.annotation import Annotation
from app.db.models.ml_backend_pool import MLBackendServicePool
from app.db.models.api_key import ApiKey
from app.db.models.prediction import Prediction, PredictionMeta
from app.db.models.prediction_input import PredictionInputLease
from app.db.models.task_lock import TaskLock
from app.deps import get_prediction_input_session_factory
from app.services.ai_mask_receipt import issue_ai_mask_receipt, verify_ai_mask_receipt
from app.services.ai_mask_session import verify_ai_mask_session
from app.services.ai_mask_session import issue_ai_mask_session
from app.services.ml_client import PredictionResult
from app.services.reference_inference import validate_native_receipt
from tests.test_dataset_content import content_world as content_world, headers
from tests.test_dataset_references import reference_world as reference_world
from tests.managed_image_inference_fixtures import (
    assert_inputs_retired,
    db_session as db_session,
    input_world as input_world,
    sync_world as sync_world,
)
from tests.test_ml_backend_project_scope import _seed_backend
from tests.test_interactive_native_mask_endpoint import _setup
from tests.test_ai_mask_accept import accept_storage_mocks as accept_storage_mocks
from tests.test_prediction_reference_actions import api_key
from tests.factory import create_batch


@pytest.fixture
async def inference_world(
    db_session, reference_world, input_world, app_module, monkeypatch
):
    _, project, task, content = reference_world
    backend = await _seed_backend(db_session, project.id)
    pool = await db_session.scalar(
        select(MLBackendServicePool).where(
            MLBackendServicePool.legacy_instance_id == backend.id
        )
    )
    pool.enabled = True
    project.raster_mask_native_editing_enabled = True
    project.tool_bindings = {
        "region": {
            "enabled": True,
            "classes": [{"name": "car"}],
            "attribute_schema": {"fields": []},
        }
    }
    await db_session.commit()
    app_module.dependency_overrides[get_prediction_input_session_factory] = lambda: (
        input_world.factory
    )
    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)

    async def setup(self):
        return _setup(["polygon", "mask"])

    monkeypatch.setattr("app.services.ml_client.MLBackendClient.setup", setup)
    world = SimpleNamespace(
        project_id=project.id,
        task_id=task.id,
        backend_id=backend.id,
        item_id=task.dataset_item_id,
        project=project,
        task=task,
        backend=backend,
        content=content,
        inputs=input_world,
        calls=[],
        during=None,
        source_files=[],
        factory=input_world.factory,
        monkeypatch=monkeypatch,
        mask=False,
        logits=False,
    )

    async def infer(task_data, context=None):
        assert not db_session.sync_session.new
        parsed = urlsplit(task_data["file_path"])
        bucket, key = parsed.path.lstrip("/").split("/", 1)
        assert input_world.objects[bucket, key] == content[1]
        assert key.startswith("prediction-inputs/")
        world.calls.append(deepcopy(context))
        if world.during:
            await world.during()
        if world.mask:
            rle = CocoRlePayload(
                encoding="coco_rle", size=[32, 48], counts=[1, 2, 1533]
            )
            shapes = [
                NativeMaskCandidate(
                    value=NativeMaskCandidateValue(rle=rle, masklabels=["car"]),
                    score=0.9,
                    candidate_id=native_mask_candidate_id(
                        rle,
                        prompt_revision=context["prompt_revision"],
                        candidate_index=0,
                    ),
                ).model_dump(mode="json")
            ]
        else:
            shapes = [
                {
                    "type": "polygon",
                    "value": {
                        "points": [[0.1, 0.1], [0.5, 0.1], [0.5, 0.5]],
                        "polygonlabels": ["car"],
                    },
                    "score": 0.9,
                }
            ]
        return PredictionResult(
            task_id=task_data["id"],
            result=shapes,
            score=0.9,
            inference_time_ms=12,
            cache_hit=False,
            model_load_ms=0,
            mask_input_next=encode_low_res_mask(np.zeros((256, 256), dtype=np.float32))
            if world.logits
            else None,
        )

    async def predict(self, tasks, **kwargs):
        return [await infer(task) for task in tasks]

    async def interactive(self, task_data, context, **kwargs):
        return await infer(task_data, context)

    monkeypatch.setattr("app.services.ml_client.MLBackendClient.predict", predict)
    monkeypatch.setattr(
        "app.services.ml_client.MLBackendClient.predict_interactive", interactive
    )
    try:
        yield world
    finally:
        app_module.dependency_overrides.pop(get_prediction_input_session_factory, None)


async def call(client, world, actor_headers, entry="interactive", context=None):
    base = f"/api/v1/projects/{world.project_id}/ml-backends/{world.backend_id}"
    if entry == "test":
        return await client.post(
            base + "/predict-test",
            params={"task_id": str(world.task_id)},
            headers=actor_headers,
        )
    return await client.post(
        base + "/interactive-annotating",
        json={
            "task_id": str(world.task_id),
            "context": context
            or {
                "type": "point",
                "points": [[0.3, 0.4]],
                "labels": [1],
                "output_geometry": "mask" if world.mask else "polygon",
            },
        },
        headers=actor_headers,
    )


async def assert_clean(world):
    await assert_inputs_retired(
        world, world.task_id, world.monkeypatch, require_records=False
    )


@pytest.mark.parametrize("entry", ["interactive", "test"])
async def test_reference_api_receives_strict_bytes_and_releases_inputs(
    httpx_client, inference_world, super_admin, entry
):
    w = inference_world
    response = await call(httpx_client, w, headers(super_admin), entry)
    assert response.status_code == 200, response.text
    assert len(w.calls) == 1
    await assert_clean(w)


@pytest.mark.parametrize("entry", ["interactive", "test"])
@pytest.mark.parametrize(
    "change",
    ["version", "source", "location", "account", "key", "key_scope", "ai_disabled"],
)
async def test_response_rechecks_original_authority_and_binding(
    httpx_client, db_session, inference_world, super_admin, entry, change
):
    w = inference_world
    key_id, auth = await api_key(
        httpx_client, super_admin, ["tasks:read", "annotations:write"]
    )

    async def alter():
        if change == "version":
            w.task.version += 1
        elif change == "source":
            w.content[5].config_revision += 1
        elif change == "location":
            w.content[4].config_revision += 1
        elif change == "account":
            super_admin[0].is_active = False
        elif change == "ai_disabled":
            w.project.ai_interactive_enabled = False
        else:
            key = await db_session.get(ApiKey, key_id)
            if change == "key":
                key.revoked_at = datetime.now(timezone.utc)
            else:
                key.scopes = []
        await db_session.commit()

    w.during = alter
    response = await call(httpx_client, w, auth, entry)
    if change == "ai_disabled" and entry == "test":
        assert response.status_code == 200, response.text
    else:
        assert response.status_code in {403, 409}, response.text
        assert "accept_receipts" not in response.json()
    await assert_clean(w)


@pytest.mark.parametrize(
    "entry,scopes,allowed",
    [
        ("test", ["tasks:read"], True),
        ("test", ["annotations:write"], False),
        ("interactive", ["tasks:read"], False),
        ("interactive", ["annotations:write"], False),
        ("interactive", ["tasks:read", "annotations:write"], True),
    ],
)
async def test_scopes_are_specific_to_consumer(
    httpx_client, inference_world, super_admin, entry, scopes, allowed
):
    w = inference_world
    _, auth = await api_key(httpx_client, super_admin, scopes)
    response = await call(httpx_client, w, auth, entry)
    assert response.status_code == (200 if allowed else 403), response.text
    assert bool(w.calls) == allowed
    await assert_clean(w)


@pytest.mark.parametrize("entry", ["interactive", "test"])
async def test_completed_task_keeps_predict_test_read_semantics(
    httpx_client, db_session, inference_world, super_admin, entry
):
    w = inference_world
    w.task.status = "completed"
    await db_session.commit()
    response = await call(httpx_client, w, headers(super_admin), entry)
    assert response.status_code == (200 if entry == "test" else 409), response.text
    await assert_clean(w)


@pytest.mark.parametrize("change", ["batch_status", "batch_assignee", "task_lock"])
async def test_interactive_rechecks_batch_and_edit_lease(
    httpx_client, db_session, inference_world, annotator, reviewer, change
):
    w = inference_world
    batch = await create_batch(
        db_session,
        project_id=w.project_id,
        status="annotating",
    )
    batch.annotator_id = annotator[0].id
    w.task.batch_id, w.task.assignee_id = batch.id, None
    await db_session.commit()

    async def alter():
        if change == "batch_status":
            batch.status = "completed"
        elif change == "batch_assignee":
            batch.annotator_id = reviewer[0].id
        else:
            db_session.add(
                TaskLock(
                    task_id=w.task_id,
                    user_id=reviewer[0].id,
                    expire_at=datetime.now(timezone.utc) + timedelta(minutes=2),
                )
            )
        await db_session.commit()

    w.during = alter
    response = await call(httpx_client, w, headers(annotator))
    assert response.status_code in {403, 404, 409}, response.text
    await assert_clean(w)


@pytest.mark.parametrize("failure", ["put", "backend", "cancel", "original"])
async def test_failure_and_cancellation_leave_cleanup_owner(
    httpx_client, inference_world, super_admin, failure
):
    w = inference_world
    if failure == "put":
        w.inputs.fail_put = True
    elif failure == "original":
        (w.content[0] / "图片 空格.png").write_bytes(b"changed")
    else:

        async def fail():
            if failure == "cancel":
                raise asyncio.CancelledError()
            raise HTTPException(502, "synthetic backend failure")

        w.during = fail
    if failure == "cancel":
        started = asyncio.Event()

        async def wait_for_cancel():
            started.set()
            await asyncio.Event().wait()

        w.during = wait_for_cancel
        pending = asyncio.create_task(call(httpx_client, w, headers(super_admin)))
        await asyncio.wait_for(started.wait(), 5)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    else:
        response = await call(httpx_client, w, headers(super_admin))
        assert response.status_code in {409, 502, 503}, response.text
    assert not w.inputs.objects
    # An uncertain PUT deliberately retains a seven-day recovery record.
    async with w.inputs.factory() as db:
        records = list(
            (
                await db.scalars(
                    select(PredictionInputLease).where(
                        PredictionInputLease.task_id == w.task_id
                    )
                )
            ).all()
        )
        if failure == "put":
            assert records
            assert all(
                record.state == "cleanup_pending" and record.uncertain_write
                for record in records
            )
        else:
            assert all(record.state == "deleted" for record in records)
    assert not w.inputs.versions
    assert all(storage.closed for storage in w.inputs.storages)


def accept_body(response):
    candidate = response["result"][0]
    return {
        "idempotency_key": f"reference-mask-{uuid.uuid4()}",
        "class_name": "car",
        "target": {"mode": "create"},
        "candidate": {
            "candidate": candidate,
            "candidate_index": 0,
            "prompt_revision": response["prompt_revision"],
            "receipt": response["accept_receipts"][candidate["candidate_id"]],
        },
        "routing": response["routing"],
        "prompt_summary": response["prompt_summary"],
        "inference": {
            key: response[key]
            for key in (
                "model_version",
                "inference_time_ms",
                "cache_hit",
                "model_load_ms",
            )
        },
    }


async def test_native_mask_response_accept_and_logits_bind_original(
    httpx_client, db_session, inference_world, super_admin, accept_storage_mocks
):
    w = inference_world
    w.mask = w.logits = True
    response = await call(httpx_client, w, headers(super_admin))
    assert response.status_code == 200, response.text
    body = accept_body(response.json())
    evidence = verify_ai_mask_receipt(body["candidate"]["receipt"])["reference_input"]
    assert evidence["item_id"] == str(w.item_id)
    assert evidence["sha256"] == hashlib.sha256(w.content[1]).hexdigest()
    token = response.json()["mask_input_next"]
    assert verify_ai_mask_session(token)["reference_input"] == evidence
    continuation = await call(
        httpx_client,
        w,
        headers(super_admin),
        context={
            "type": "point",
            "points": [[0.4, 0.4]],
            "labels": [1],
            "output_geometry": "mask",
            "mask_input": token,
        },
    )
    assert continuation.status_code == 200, continuation.text
    url = f"/api/v1/tasks/{w.task_id}/ai-mask-candidates/accept"
    accepted = await httpx_client.post(url, json=body, headers=headers(super_admin))
    assert accepted.status_code == 200, accepted.text
    annotation_id = uuid.UUID(accepted.json()["annotation"]["id"])
    annotation = await db_session.get(Annotation, annotation_id)
    metadata = await db_session.scalar(
        select(PredictionMeta).where(
            PredictionMeta.prediction_id == annotation.parent_prediction_id
        )
    )
    assert all(metadata.extra["reference_input"][k] == v for k, v in evidence.items())
    replayed = await httpx_client.post(url, json=body, headers=headers(super_admin))
    assert replayed.status_code == 200 and replayed.json()["replayed"], replayed.text
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Prediction)
            .where(Prediction.task_id == w.task_id)
        )
        == 1
    )
    await assert_clean(w)


@pytest.mark.parametrize("tamper", ["absent", "item", "sha", "width"])
async def test_native_accept_requires_signed_original_provenance(
    httpx_client, db_session, inference_world, super_admin, accept_storage_mocks, tamper
):
    w = inference_world
    w.mask = True
    response = await call(httpx_client, w, headers(super_admin))
    assert response.status_code == 200, response.text
    body = accept_body(response.json())
    claims = verify_ai_mask_receipt(body["candidate"]["receipt"])
    for reserved in ("v", "iat", "exp"):
        claims.pop(reserved)
    if tamper == "absent":
        claims.pop("reference_input")
    elif tamper == "item":
        claims["reference_input"]["item_id"] = str(uuid.uuid4())
    elif tamper == "sha":
        claims["reference_input"]["sha256"] = "0" * 64
    else:
        claims["reference_input"]["width"] += 1
    body["candidate"]["receipt"] = issue_ai_mask_receipt(claims)
    response = await httpx_client.post(
        f"/api/v1/tasks/{w.task_id}/ai-mask-candidates/accept",
        json=body,
        headers=headers(super_admin),
    )
    assert response.status_code == 409, response.text
    assert (
        await db_session.scalar(
            select(Prediction.id).where(Prediction.task_id == w.task_id)
        )
        is None
    )
    assert accept_storage_mocks[0].await_count == 0
    await assert_clean(w)


async def test_native_receipt_rejects_forged_python_object(db_session, inference_world):
    w = inference_world
    from app.services.content_snapshot import ContentError

    for proof in (True, {}, SimpleNamespace(issuer=object())):
        with pytest.raises(ContentError, match="prediction_reference_receipt_invalid"):
            await validate_native_receipt(db_session, w.task_id, w.project_id, proof)


async def test_native_replay_outlives_candidate_receipt(
    httpx_client, db_session, inference_world, super_admin, accept_storage_mocks
):
    w = inference_world
    w.mask = True
    response = await call(httpx_client, w, headers(super_admin))
    assert response.status_code == 200, response.text
    body = accept_body(response.json())
    url = f"/api/v1/tasks/{w.task_id}/ai-mask-candidates/accept"
    accepted = await httpx_client.post(url, json=body, headers=headers(super_admin))
    assert accepted.status_code == 200, accepted.text
    claims = verify_ai_mask_receipt(body["candidate"]["receipt"])
    for reserved in ("v", "iat", "exp"):
        claims.pop(reserved)
    body["candidate"]["receipt"] = issue_ai_mask_receipt(
        claims, now=int(datetime.now(timezone.utc).timestamp()) - 3600
    )
    replayed = await httpx_client.post(url, json=body, headers=headers(super_admin))
    assert replayed.status_code == 200 and replayed.json()["replayed"], replayed.text
    assert "_reference_input" not in replayed.json()
    conflicting = deepcopy(body)
    conflicting["class_name"] = "different"
    rejected = await httpx_client.post(
        url, json=conflicting, headers=headers(super_admin)
    )
    assert (
        rejected.status_code == 409
        and rejected.json()["detail"]["reason"] == "idempotency_conflict"
    ), rejected.text
    body["idempotency_key"] = f"new-{uuid.uuid4()}"
    expired = await httpx_client.post(url, json=body, headers=headers(super_admin))
    assert (
        expired.status_code == 409
        and expired.json()["detail"]["reason"] == "candidate_receipt_expired"
    ), expired.text


@pytest.mark.parametrize("origin", ["absent", "sha"])
async def test_old_logits_cannot_seed_reference_image(
    httpx_client, inference_world, super_admin, origin
):
    w = inference_world
    w.mask = w.logits = True
    response = await call(httpx_client, w, headers(super_admin))
    assert response.status_code == 200, response.text
    claims = verify_ai_mask_session(response.json()["mask_input_next"])
    raw = claims.pop("raw")
    for key in ("v", "iat", "exp"):
        claims.pop(key)
    if origin == "absent":
        claims.pop("reference_input")
    else:
        claims["reference_input"]["sha256"] = "0" * 64
    token = issue_ai_mask_session(raw, claims)
    response = await call(
        httpx_client,
        w,
        headers(super_admin),
        context={
            "type": "point",
            "points": [[0.4, 0.4]],
            "labels": [1],
            "output_geometry": "mask",
            "mask_input": token,
        },
    )
    assert (
        response.status_code == 409
        and response.json()["detail"]["reason"] == "mask_session_mismatch"
    ), response.text
    assert len(w.calls) == 1
    await assert_clean(w)


async def test_setup_cannot_overtake_key_revocation(
    httpx_client, db_session, inference_world, super_admin, monkeypatch
):
    w = inference_world
    key_id, auth = await api_key(
        httpx_client, super_admin, ["tasks:read", "annotations:write"]
    )

    async def setup(self):
        key = await db_session.get(ApiKey, key_id)
        key.revoked_at = datetime.now(timezone.utc)
        await db_session.commit()
        return _setup(["polygon", "mask"])

    monkeypatch.setattr("app.services.ml_client.MLBackendClient.setup", setup)
    response = await call(httpx_client, w, auth)
    assert response.status_code == 403, response.text
    assert not w.calls
    await assert_clean(w)


async def test_busy_final_scope_is_bounded(httpx_client, inference_world, super_admin):
    from app.services.task_lock import TaskLockService

    w = inference_world
    async with w.inputs.factory() as other:
        await TaskLockService(other)._lock_task_scope(w.task_id)
        try:
            response = await asyncio.wait_for(
                call(httpx_client, w, headers(super_admin)), 8
            )
            assert (
                response.status_code == 409
                and response.json()["detail"]["code"] == "reference_inference_busy"
            ), response.text
        finally:
            await other.rollback()
    await assert_clean(w)


@pytest.mark.parametrize(
    "change", ["actor", "session", "transaction", "nested", "request", "mask"]
)
async def test_private_native_receipt_cannot_escape_its_write(
    httpx_client, db_session, inference_world, super_admin, change
):
    from app.db.models.task import Task
    from app.db.models.user import User
    from app.schemas.ai_mask import AiMaskAcceptRequest
    from app.services.content_snapshot import ContentError
    from app.services.reference_inference import qualify_native_acceptance

    w = inference_world
    actor_id = super_admin[0].id
    w.mask = True
    response = await call(httpx_client, w, headers(super_admin))
    assert response.status_code == 200, response.text
    data = AiMaskAcceptRequest.model_validate(accept_body(response.json()))
    task = await db_session.get(Task, w.task_id, populate_existing=True)
    actor = await db_session.get(User, actor_id, populate_existing=True)
    proof = await qualify_native_acceptance(
        db_session,
        task,
        actor,
        SimpleNamespace(
            state=SimpleNamespace(api_key_id=None),
            headers={"authorization": "Bearer " + super_admin[1]},
        ),
        data,
    )
    await validate_native_receipt(
        db_session, w.task_id, w.project_id, proof, actor_id=actor_id, data=data
    )
    kwargs = {}
    session = db_session
    other = nested = None
    if change == "actor":
        kwargs["actor_id"] = uuid.uuid4()
    elif change == "session":
        other = session = w.inputs.factory()
    elif change == "transaction":
        await db_session.commit()
    elif change == "nested":
        nested = await db_session.begin_nested()
    elif change == "request":
        data.class_name = "different"
        kwargs["data"] = data
    else:
        kwargs["result"] = [
            {"geometry": {"type": "raster_mask", "mask": {"sha256": "0" * 64}}}
        ]
    try:
        with pytest.raises(ContentError):
            await validate_native_receipt(
                session, w.task_id, w.project_id, proof, **kwargs
            )
    finally:
        if nested:
            await nested.rollback()
        if other:
            await other.close()
        await db_session.rollback()


async def test_inference_authorization_never_mutates_expired_edit_leases(
    httpx_client, db_session, inference_world, super_admin, reviewer
):
    w = inference_world
    expired = TaskLock(
        task_id=w.task_id,
        user_id=reviewer[0].id,
        expire_at=datetime.now(timezone.utc) - timedelta(minutes=2),
    )
    db_session.add(expired)
    await db_session.commit()
    lock_id = expired.id

    async def during():
        assert (
            await db_session.get(TaskLock, lock_id, populate_existing=True) is not None
        )

    w.during = during
    response = await call(httpx_client, w, headers(super_admin))
    assert response.status_code == 200, response.text
    assert await db_session.get(TaskLock, lock_id, populate_existing=True) is not None
    await assert_clean(w)
