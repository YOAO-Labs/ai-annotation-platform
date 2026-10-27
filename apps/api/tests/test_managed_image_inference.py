"""Real synchronous guards for managed image sources, delivery and native writes."""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import threading
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import uuid

from aap_protocol_v2 import (
    CocoRlePayload,
    NativeMaskCandidate,
    NativeMaskCandidateValue,
)
from aap_protocol_v2 import encode_low_res_mask, native_mask_candidate_id
from fastapi import HTTPException
import numpy as np
import httpx
import pytest
from sqlalchemy import func, select

from app.config import settings
from app.deps import get_db
from app.db.models.annotation import Annotation
from app.db.models.api_key import ApiKey
from app.db.models.prediction import Prediction, PredictionMeta
from app.db.models.prediction_input import PredictionInputLease
from app.db.models.storage_maintenance import StorageMaintenanceGate, StorageOperation
from app.db.models.task import Task
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.db.models.user import User
from app.services.ai_mask_receipt import issue_ai_mask_receipt, verify_ai_mask_receipt
from app.services.ai_mask_session import issue_ai_mask_session, verify_ai_mask_session
from app.services.content_snapshot import ContentError
from aap_inference_contract.backend_control import BackendControlSetupResult
from aap_inference_contract.execution import ModelTaskResult, thaw_json
from tests.inference_execution_test_ports import FakeModelExecutionPort
from tests import live_provider_guard
from tests.factory import create_project
from tests.managed_image_inference_fixtures import (
    assert_inputs_retired,
    attach_managed,
    db_session as db_session,
    input_world as input_world,
    sync_world as sync_world,
)
from tests.test_ai_mask_accept import accept_storage_mocks as accept_storage_mocks
from tests.test_interactive_native_mask_endpoint import _setup
from tests.test_ml_backend_project_scope import _seed_backend
from tests.test_prediction_reference_actions import api_key
from tests.test_reference_image_inference import accept_body, call


@pytest.fixture
async def httpx_client(app_module, sync_world):
    """Each request owns its actual transaction; seed ORM never owns that handle."""

    async def database():
        async with sync_world.factory() as db:
            try:
                yield db
            finally:
                await db.rollback()

    missing = object()
    previous = app_module.dependency_overrides.get(get_db, missing)
    app_module.dependency_overrides[get_db] = database
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_module),
            base_url="http://test",
        ) as client:
            yield client
    finally:
        if previous is missing:
            app_module.dependency_overrides.pop(get_db, None)
        else:
            app_module.dependency_overrides[get_db] = previous


@pytest.fixture
async def managed_world(db_session, sync_world, super_admin, monkeypatch, request):
    actor = super_admin[0]
    project = await create_project(db_session, owner_id=actor.id, type_key="image-seg")
    project.raster_mask_native_editing_enabled = True
    project.tool_bindings = {
        "region": {
            "enabled": True,
            "classes": [{"name": "car"}],
            "attribute_schema": {"fields": []},
        }
    }
    backend = await _seed_backend(db_session, project.id)
    from app.db.models.ml_backend_pool import MLBackendServicePool

    pool = await db_session.scalar(
        select(MLBackendServicePool).where(
            MLBackendServicePool.legacy_instance_id == backend.id
        )
    )
    pool.enabled = True
    member = await attach_managed(
        db_session,
        project,
        actor,
        origin=getattr(request, "param", "direct"),
        size=(48, 32),
    )
    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)
    world = SimpleNamespace(
        project_id=project.id,
        project=project,
        task_id=member.task.id,
        task=member.task,
        backend_id=backend.id,
        backend=backend,
        member=member,
        sources=sync_world,
        inputs=sync_world.inputs,
        mask=False,
        logits=False,
        during=None,
        calls=[],
        setup_during=None,
    )

    async def setup(registry_id):
        if world.setup_during:
            await world.setup_during()
        return BackendControlSetupResult(
            registry_id=uuid.UUID(str(registry_id)),
            setup=_setup(["polygon", "mask"]),
        )

    async def infer(operation, task_input):
        task_data = thaw_json(task_input)
        context = thaw_json(operation.context)
        parsed = urlsplit(task_data["file_path"])
        bucket, key = parsed.path.lstrip("/").split("/", 1)
        version = parse_qs(parsed.query)["versionId"][0]
        assert version not in {"null", ""}
        actual = world.inputs.versions[bucket, key][version][0]
        assert actual == member.raw
        assert hashlib.sha256(actual).hexdigest() == member.source.sha256
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
        return ModelTaskResult(
            task_id=task_data["id"],
            result=shapes,
            score=0.9,
            inference_time_ms=12,
            model_version="qualified-test",
            cache_hit=False,
            model_load_ms=0,
            mask_input_next=encode_low_res_mask(np.zeros((256, 256), dtype=np.float32))
            if world.logits
            else None,
        )

    class Control:
        async def setup(self, registry_id):
            return await setup(registry_id)

    control = Control()
    monkeypatch.setattr(
        "app.api.v1.ml_backends.backend_control_service",
        lambda _db: control,
    )
    monkeypatch.setattr(
        "app.api.v1.ml_backends.model_execution_port",
        lambda _db: FakeModelExecutionPort(infer),
    )
    return world


def auth(actor):
    return {"Authorization": "Bearer " + actor[1]}


async def clean(world, monkeypatch, *, require_records=True):
    await assert_inputs_retired(
        world.sources,
        world.task_id,
        monkeypatch,
        require_records=require_records,
    )


@pytest.mark.parametrize("managed_world", ["direct", "primary", "task"], indirect=True)
@pytest.mark.parametrize("entry", ["interactive", "test"])
async def test_both_endpoints_read_saved_original_and_retire_exact_versions(
    httpx_client,
    managed_world,
    super_admin,
    entry,
    monkeypatch,
):
    world = managed_world
    response = await call(httpx_client, world, auth(super_admin), entry)
    assert response.status_code == 200, response.text
    assert len(world.calls) == 1 and len(world.sources.captured) == 1
    spec = world.sources.captured[0]
    assert (spec.bucket, spec.object_key) == (
        world.member.source.bucket,
        world.member.source.object_key,
    )
    assert world.inputs.read_versions == world.inputs.signed_versions
    await clean(world, monkeypatch)


@pytest.mark.parametrize("entry", ["interactive", "test"])
@pytest.mark.parametrize(
    "change",
    ["version", "source", "account", "key", "key_scope", "gate", "ai_disabled"],
)
async def test_response_rechecks_source_credentials_task_and_gate(
    httpx_client,
    managed_world,
    super_admin,
    entry,
    change,
    monkeypatch,
):
    world = managed_world
    key_id, headers = await api_key(
        httpx_client,
        super_admin,
        ["tasks:read", "annotations:write", "projects:write"],
    )
    replacement = None
    if change == "source":
        async with world.sources.factory() as db:
            actor = await db.get(User, super_admin[0].id)
            project = await db.get(type(world.project), world.project_id)
            replacement = await attach_managed(db, project, actor)

    async def alter():
        async with world.sources.factory() as db:
            if change == "version":
                task = await db.get(Task, world.task_id)
                task.version += 1
            elif change == "source":
                task = await db.get(Task, world.task_id)
                task.dataset_item_id = replacement.item.id
                task.file_path = replacement.task.file_path
            elif change == "account":
                (await db.get(User, super_admin[0].id)).is_active = False
            elif change in {"key", "key_scope"}:
                key = await db.get(ApiKey, key_id)
                if change == "key":
                    key.revoked_at = datetime.now(timezone.utc)
                else:
                    key.scopes = []
            elif change == "gate":
                gate = await db.get(
                    StorageMaintenanceGate, world.member.source.location_id
                )
                gate.state, gate.revision = "draining", gate.revision + 1
            else:
                (
                    await db.get(type(world.project), world.project_id)
                ).ai_interactive_enabled = False
            await db.commit()

    world.during = alter
    response = await call(httpx_client, world, headers, entry)
    if change == "ai_disabled" and entry == "test":
        assert response.status_code == 200, response.text
    else:
        assert response.status_code in {403, 409}, response.text
        assert "accept_receipts" not in response.json()
    if change == "gate":
        async with world.sources.factory() as db:
            gate = await db.get(StorageMaintenanceGate, world.member.source.location_id)
            gate.state, gate.revision = "active", gate.revision + 1
            await db.commit()
    await clean(world, monkeypatch)


@pytest.mark.parametrize("entry", ["interactive"])
async def test_setup_key_revocation_never_dispatches_inference(
    httpx_client,
    managed_world,
    super_admin,
    monkeypatch,
    entry,
):
    world = managed_world
    key_id, headers = await api_key(
        httpx_client,
        super_admin,
        ["tasks:read", "annotations:write", "projects:write"],
    )

    async def revoke():
        async with world.sources.factory() as db:
            (await db.get(ApiKey, key_id)).revoked_at = datetime.now(timezone.utc)
            await db.commit()

    world.setup_during = revoke
    response = await call(httpx_client, world, headers, entry)
    assert response.status_code == 403, response.text
    assert not world.calls
    await clean(world, monkeypatch, require_records=False)


@pytest.mark.parametrize("managed_world", ["primary", "task"], indirect=True)
async def test_native_create_refine_and_expired_candidate_replay_preserve_provenance(
    httpx_client,
    db_session,
    managed_world,
    super_admin,
    accept_storage_mocks,
    monkeypatch,
):
    world = managed_world
    world.mask = world.logits = True
    response = await call(httpx_client, world, auth(super_admin))
    assert response.status_code == 200, response.text
    body = accept_body(response.json())
    claims = verify_ai_mask_receipt(body["candidate"]["receipt"])
    identity = claims["original_input"]
    assert identity == world.member.identity
    assert claims.get("reference_input") is None
    assert (
        verify_ai_mask_session(response.json()["mask_input_next"])["original_input"]
        == identity
    )
    url = f"/api/v1/tasks/{world.task_id}/ai-mask-candidates/accept"
    accepted = await httpx_client.post(url, json=body, headers=auth(super_admin))
    assert accepted.status_code == 200, accepted.text
    assert "_original_input" not in accepted.json()
    annotation_id = uuid.UUID(accepted.json()["annotation"]["id"])
    annotation = await db_session.get(Annotation, annotation_id)
    metadata = await db_session.scalar(
        select(PredictionMeta).where(
            PredictionMeta.prediction_id == annotation.parent_prediction_id,
        )
    )
    assert all(
        metadata.extra["original_input"][name] == value
        for name, value in identity.items()
    )
    assert "reference_input" not in metadata.extra
    assert not {"bucket", "endpoint_url", "object_key", "facts", "file_path"} & set(
        metadata.extra["original_input"]
    )
    # The Decision owns exact replay even when its original candidate expires.
    unsigned = {
        name: value for name, value in claims.items() if name not in {"v", "iat", "exp"}
    }
    body["candidate"]["receipt"] = issue_ai_mask_receipt(
        unsigned,
        now=int(datetime.now(timezone.utc).timestamp()) - 3600,
    )
    await db_session.refresh(world.task)
    world.task.version += 1
    await db_session.commit()
    replay = await httpx_client.post(url, json=body, headers=auth(super_admin))
    assert replay.status_code == 200 and replay.json()["replayed"], replay.text
    assert "_original_input" not in replay.json()
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Prediction)
            .where(
                Prediction.task_id == world.task_id,
            )
        )
        == 1
    )
    from unittest.mock import AsyncMock

    monkeypatch.setattr(settings, "raster_mask_read_enabled", True)
    monkeypatch.setattr(
        "app.services.ai_mask_prompt.load_coco_rle",
        AsyncMock(
            return_value={
                "encoding": "coco_rle",
                "size": [32, 48],
                "counts": [1, 2, 1533],
            }
        ),
    )
    prior_version = annotation.version
    refined = await call(
        httpx_client,
        world,
        auth(super_admin),
        context={
            "type": "point",
            "points": [[0.4, 0.4]],
            "labels": [1],
            "output_geometry": "mask",
            "mask_prompt_source": {
                "annotation_id": str(annotation_id),
                "source_version": prior_version,
            },
        },
    )
    assert refined.status_code == 200, refined.text
    next_body = accept_body(refined.json())
    next_body["target"] = {
        "mode": "refine",
        "source_annotation_id": str(annotation_id),
        "source_version": prior_version,
    }
    next_accepted = await httpx_client.post(
        url,
        json=next_body,
        headers={**auth(super_admin), "If-Match": str(prior_version)},
    )
    assert next_accepted.status_code == 200, next_accepted.text
    assert next_accepted.json()["annotation"]["id"] == str(annotation_id)
    assert next_accepted.json()["result_version"] == prior_version + 1
    await clean(world, monkeypatch)


@pytest.mark.parametrize("managed_world", ["direct", "primary"], indirect=True)
async def test_decision_replay_rejects_new_canonical_owner_even_when_sha_matches(
    httpx_client,
    db_session,
    managed_world,
    super_admin,
    accept_storage_mocks,
    monkeypatch,
):
    world = managed_world
    world.mask = True
    candidate = await call(httpx_client, world, auth(super_admin))
    assert candidate.status_code == 200, candidate.text
    body = accept_body(candidate.json())
    url = f"/api/v1/tasks/{world.task_id}/ai-mask-candidates/accept"
    accepted = await httpx_client.post(url, json=body, headers=auth(super_admin))
    assert accepted.status_code == 200, accepted.text
    replacement = await attach_managed(
        db_session,
        world.project,
        super_admin[0],
        size=(48, 32),
    )
    assert replacement.source.sha256 == world.member.source.sha256
    assert replacement.source.content_id != world.member.source.content_id
    original_task = await db_session.get(Task, world.task_id)
    if original_task.dataset_item_id is not None:
        original_task.dataset_item_id = replacement.item.id
    else:
        link = await db_session.scalar(
            select(TaskDatasetItemLink).where(
                TaskDatasetItemLink.task_id == world.task_id,
                TaskDatasetItemLink.role == "primary_lidar",
            )
        )
        link.dataset_item_id = replacement.item.id
    original_task.file_path = replacement.task.file_path
    await db_session.commit()
    replay = await httpx_client.post(url, json=body, headers=auth(super_admin))
    assert replay.status_code == 409, replay.text
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Prediction)
            .where(
                Prediction.task_id == world.task_id,
            )
        )
        == 1
    )
    await clean(world, monkeypatch)


@pytest.mark.parametrize("tamper", ["absent", "content_id", "sha256", "width"])
async def test_native_accept_rejects_missing_or_other_original_identity(
    httpx_client,
    db_session,
    managed_world,
    super_admin,
    accept_storage_mocks,
    monkeypatch,
    tamper,
):
    world = managed_world
    world.mask = True
    response = await call(httpx_client, world, auth(super_admin))
    assert response.status_code == 200, response.text
    body = accept_body(response.json())
    claims = verify_ai_mask_receipt(body["candidate"]["receipt"])
    for key in ("v", "iat", "exp"):
        claims.pop(key)
    if tamper == "absent":
        claims.pop("original_input")
    elif tamper == "content_id":
        claims["original_input"][tamper] = str(uuid.uuid4())
    elif tamper == "sha256":
        claims["original_input"][tamper] = "0" * 64
    else:
        claims["original_input"][tamper] += 1
    body["candidate"]["receipt"] = issue_ai_mask_receipt(claims)
    rejected = await httpx_client.post(
        f"/api/v1/tasks/{world.task_id}/ai-mask-candidates/accept",
        json=body,
        headers=auth(super_admin),
    )
    assert rejected.status_code == 409, rejected.text
    assert not await db_session.scalar(
        select(Prediction.id).where(Prediction.task_id == world.task_id)
    )
    accept_storage_mocks[0].assert_not_awaited()
    await clean(world, monkeypatch)


@pytest.mark.parametrize("origin", ["absent", "sha256"])
async def test_logits_require_the_same_managed_original(
    httpx_client,
    managed_world,
    super_admin,
    monkeypatch,
    origin,
):
    world = managed_world
    world.mask = world.logits = True
    response = await call(httpx_client, world, auth(super_admin))
    assert response.status_code == 200, response.text
    claims = verify_ai_mask_session(response.json()["mask_input_next"])
    raw = claims.pop("raw")
    for name in ("v", "iat", "exp"):
        claims.pop(name)
    if origin == "absent":
        claims.pop("original_input")
    else:
        claims["original_input"]["sha256"] = "0" * 64
    token = issue_ai_mask_session(raw, claims)
    rejected = await call(
        httpx_client,
        world,
        auth(super_admin),
        context={
            "type": "point",
            "points": [[0.4, 0.4]],
            "labels": [1],
            "output_geometry": "mask",
            "mask_input": token,
        },
    )
    assert rejected.status_code == 409, rejected.text
    assert len(world.calls) == 1
    await clean(world, monkeypatch)


async def test_repeated_cancellation_drains_late_source_and_retires_inputs(
    httpx_client,
    managed_world,
    super_admin,
    monkeypatch,
):
    world = managed_world
    started, release = threading.Event(), threading.Event()

    def wait(spec):
        started.set()
        if not release.wait(5):
            raise TimeoutError("test source gate")

    world.sources.capture_hook = wait
    pending = asyncio.create_task(call(httpx_client, world, auth(super_admin)))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done()
        async with world.sources.factory() as db:
            assert await db.scalar(select(StorageOperation.id))
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
    finally:
        release.set()
        if not pending.done():
            await asyncio.gather(pending, return_exceptions=True)
    assert not world.calls
    await clean(world, monkeypatch)


async def test_unknown_source_close_retains_cross_location_cleanup_responsibility(
    httpx_client,
    managed_world,
    super_admin,
):
    world = managed_world

    def fail_close(stream):
        raise OSError("synthetic private source close")

    world.sources.source_close_hook = fail_close
    try:
        with pytest.raises((ContentError, OSError, HTTPException)):
            response = await call(httpx_client, world, auth(super_admin))
            if response.status_code >= 400:
                raise HTTPException(response.status_code)
        async with world.sources.factory() as db:
            leases = list(
                await db.scalars(
                    select(PredictionInputLease).where(
                        PredictionInputLease.task_id == world.task_id,
                    )
                )
            )
            assert leases and all(row.state == "cleanup_pending" for row in leases)
            operations = list(await db.scalars(select(StorageOperation)))
            assert operations
            assert any(
                op.purpose == "prediction_input_" + leases[0].id.hex
                for op in operations
            )
            assert all(row.uncertain_write for row in leases)
    finally:
        # No ledger is falsely settled; teardown destroys this owned schema.
        world.sources.source_close_hook = None


@pytest.mark.parametrize("with_prompt", [False, True])
async def test_native_refine_only_requires_annotation_read_for_a_source_prompt(
    httpx_client,
    db_session,
    managed_world,
    super_admin,
    accept_storage_mocks,
    with_prompt,
):
    from tests.test_ai_mask_accept import _body
    from app.services.raster_mask_storage import build_rle_reference

    world = managed_world
    rle = {"encoding": "coco_rle", "size": [32, 48], "counts": [1, 2, 1533]}
    source = Annotation(
        task_id=world.task_id,
        project_id=world.project_id,
        user_id=super_admin[0].id,
        source="manual",
        annotation_type="raster_mask",
        tool_unit_id="region",
        class_name="car",
        geometry={"type": "raster_mask", "mask": build_rle_reference(rle)},
        version=1,
    )
    db_session.add(source)
    await db_session.commit()
    from app.db.models.ml_backend_pool import MLBackendServicePool

    pool = await db_session.scalar(
        select(MLBackendServicePool).where(
            MLBackendServicePool.legacy_instance_id == world.backend_id,
        )
    )
    # Mint the same signed native contract, with an explicit refine target. A
    # point-only candidate has no source-Mask prompt and needs no read scope.
    body = _body(
        world.task, world.backend, pool, rle=rle, source=source, class_name="car"
    )
    claims = verify_ai_mask_receipt(body["candidate"]["receipt"])
    for key in ("v", "iat", "exp"):
        claims.pop(key)
    if not with_prompt:
        claims["prompt_source"] = None
    body["candidate"]["receipt"] = issue_ai_mask_receipt(claims)
    _, key_headers = await api_key(httpx_client, super_admin, ["annotations:write"])
    result = await httpx_client.post(
        f"/api/v1/tasks/{world.task_id}/ai-mask-candidates/accept",
        json=body,
        headers={**key_headers, "If-Match": 'W/"1"'},
    )
    assert result.status_code == (403 if with_prompt else 200), result.text
    if with_prompt:
        accept_storage_mocks[0].assert_not_awaited()
    else:
        assert result.json()["annotation"]["id"] == str(source.id)
        assert result.json()["result_version"] == 2


@pytest.mark.parametrize(
    "change",
    ["actor", "session", "transaction", "nested", "request", "mask", "version"],
)
async def test_native_private_receipt_cannot_leave_its_actual_transaction(
    httpx_client,
    db_session,
    managed_world,
    super_admin,
    change,
    monkeypatch,
):
    from app.schemas.ai_mask import AiMaskAcceptRequest
    from app.services.managed_native_inference import (
        qualify_native_acceptance,
        validate_native_receipt,
    )

    world = managed_world
    world.mask = True
    response = await call(httpx_client, world, auth(super_admin))
    assert response.status_code == 200, response.text
    data = AiMaskAcceptRequest.model_validate(accept_body(response.json()))
    actor = await db_session.get(User, super_admin[0].id, populate_existing=True)
    task = await db_session.get(Task, world.task_id, populate_existing=True)
    receipt = await qualify_native_acceptance(
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
        db_session,
        world.task_id,
        world.project_id,
        receipt,
        actor_id=actor.id,
        data=data,
    )
    session, other, nested, kwargs = db_session, None, None, {}
    if change == "actor":
        kwargs["actor_id"] = uuid.uuid4()
    elif change == "session":
        session = other = world.sources.factory()
    elif change == "transaction":
        await db_session.commit()
    elif change == "nested":
        nested = await db_session.begin_nested()
    elif change == "request":
        data.class_name = "another"
        kwargs["data"] = data
    elif change == "mask":
        kwargs["result"] = [
            {"geometry": {"type": "raster_mask", "mask": {"sha256": "0" * 64}}}
        ]
    else:
        task.version += 1
        await db_session.flush()
    try:
        with pytest.raises(ContentError):
            await validate_native_receipt(
                session, world.task_id, world.project_id, receipt, **kwargs
            )
    finally:
        if nested:
            await nested.rollback()
        if other:
            await other.close()
        await db_session.rollback()
    await clean(world, monkeypatch)


async def test_key_expiry_during_mask_storage_rolls_back_every_business_write(
    httpx_client,
    db_session,
    managed_world,
    super_admin,
    accept_storage_mocks,
    monkeypatch,
):
    from app.db.models.ai_mask_accept_decision import AiMaskAcceptDecision
    from app.db.models.audit_log import AuditLog
    from app.services import prediction_originals as originals

    world = managed_world
    world.mask = True
    key_id, key_headers = await api_key(
        httpx_client, super_admin, ["annotations:write"]
    )
    expires = datetime.now(timezone.utc) + timedelta(minutes=5)
    key = await db_session.get(ApiKey, key_id)
    key.expires_at = expires
    await db_session.commit()
    response = await call(httpx_client, world, key_headers)
    assert response.status_code == 200, response.text
    body = accept_body(response.json())
    writes = []

    class ExpiredDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            instant = expires + timedelta(seconds=1)
            return instant if tz is not None else instant.replace(tzinfo=None)

    async def store(*args, **kwargs):
        writes.append(True)
        monkeypatch.setattr(originals, "datetime", ExpiredDateTime)

    accept_storage_mocks[0].side_effect = store
    result = await httpx_client.post(
        f"/api/v1/tasks/{world.task_id}/ai-mask-candidates/accept",
        json=body,
        headers=key_headers,
    )
    assert writes and result.status_code == 403, result.text
    await db_session.rollback()
    for model in (Prediction, Annotation, AiMaskAcceptDecision):
        assert (
            await db_session.scalar(
                select(func.count())
                .select_from(model)
                .where(model.task_id == world.task_id)
            )
            == 0
        )
    assert (
        await db_session.scalar(select(func.count()).select_from(PredictionMeta)) == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.action == "mask_ai.candidate_accept",
            )
        )
        == 0
    )
    await clean(world, monkeypatch)


async def test_jwt_logout_during_mask_storage_rolls_back_every_business_write(
    httpx_client,
    db_session,
    managed_world,
    super_admin,
    accept_storage_mocks,
    monkeypatch,
):
    from app.core import token_blacklist
    from app.core.security import decode_access_token
    from app.db.models.ai_mask_accept_decision import AiMaskAcceptDecision
    from app.db.models.audit_log import AuditLog

    # This regression deliberately exercises the run's own disposable Redis,
    # never an authentication substitute or a developer service selected by a
    # default.
    live_provider_guard.owned_redis()
    world = managed_world
    world.mask = True
    claims = decode_access_token(super_admin[1])
    jti = claims["jti"]
    assert isinstance(jti, str) and jti
    assert not await token_blacklist.is_blacklisted(jti)
    candidate = await call(httpx_client, world, auth(super_admin))
    assert candidate.status_code == 200, candidate.text
    body = accept_body(candidate.json())
    writes = []

    async def store(*args, **kwargs):
        writes.append(True)
        remaining = int(claims["exp"] - datetime.now(timezone.utc).timestamp())
        assert remaining > 0
        await token_blacklist.blacklist_token(jti, remaining)
        assert await token_blacklist.is_blacklisted(jti)

    accept_storage_mocks[0].side_effect = store
    try:
        result = await httpx_client.post(
            f"/api/v1/tasks/{world.task_id}/ai-mask-candidates/accept",
            json=body,
            headers=auth(super_admin),
        )
        assert writes and result.status_code == 403, result.text
        await db_session.rollback()
        for model in (Prediction, Annotation, AiMaskAcceptDecision):
            assert (
                await db_session.scalar(
                    select(func.count())
                    .select_from(model)
                    .where(model.task_id == world.task_id)
                )
                == 0
            )
        assert (
            await db_session.scalar(select(func.count()).select_from(PredictionMeta))
            == 0
        )
        assert (
            await db_session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "mask_ai.candidate_accept")
            )
            == 0
        )
        await clean(world, monkeypatch)
    finally:
        redis = token_blacklist._get_redis()
        try:
            await redis.delete(token_blacklist._KEY_PREFIX + jti)
        finally:
            await redis.aclose()
