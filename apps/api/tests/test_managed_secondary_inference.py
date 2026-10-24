"""Committed secondary-inference requests retain source, parent and authority."""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import threading
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import uuid

import httpx
from PIL import Image
import pytest
from sqlalchemy import select

from app.db.models.annotation import Annotation
from app.db.models.api_key import ApiKey
from app.db.models.audit_log import AuditLog
from app.db.models.prediction import Prediction
from app.db.models.prediction_input import PredictionInputLease
from app.db.models.project import Project
from app.db.models.storage_maintenance import StorageOperation
from app.db.models.task import Task
from app.db.models.task_lock import TaskLock
from app.db.models.user import User
from app.services import storage_access
from app.services.annotation import AnnotationService
from app.services.ml_client import PredictionResult
from app.services.ml_routing.client import RoutedMLBackendClient
from tests.conftest import create_registry_with_pool
from tests.factory import (
    build_tool_bindings,
    create_batch,
    create_membership,
    create_project,
)
from tests.managed_image_inference_fixtures import (
    assert_inputs_retired,
    attach_managed,
    db_session as db_session,
    input_world as input_world,
    sync_world as sync_world,
)
from tests.test_export_media_bundle_models import _migration
from tests.test_managed_image_inference import httpx_client as httpx_client
from tests.test_prediction_reference_actions import api_key


@pytest.fixture
async def secondary_world(db_session, sync_world, super_admin, monkeypatch, request):
    engine = storage_access.engine(db_session)
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: _migration(
                sync,
                "0240_secondary_prediction_input_owner",
            ).upgrade()
        )
    actor = super_admin[0]
    project = await create_project(db_session, owner_id=actor.id)
    project.tool_bindings = build_tool_bindings(["car", "plate"])
    # This operation is available even with the unrelated interactive UI off.
    project.ai_interactive_enabled = False
    backend, pool = await create_registry_with_pool(
        db_session,
        name="secondary-" + uuid.uuid4().hex[:8],
        url="http://secondary.test/",
        state="connected",
        enabled_pool=True,
    )
    from app.db.models.ml_backend_registry import ProjectMLBackendPool

    db_session.add(
        ProjectMLBackendPool(project_id=project.id, pool_id=pool.id, enabled=True)
    )
    backend.health_meta = {
        "capabilities": {
            "task_contract_revision": 1,
            "models": [
                {
                    "id": "cls",
                    "task": "classify",
                    "supported_inputs": ["crop"],
                    "output_attribute_types": ["class"],
                    "supported_geometric_outputs": [],
                    "resource_profile": {"device": "cpu", "batchable": True},
                },
                {
                    "id": "det",
                    "task": "detect",
                    "supported_inputs": ["crop"],
                    "supported_geometric_outputs": ["bbox"],
                    "resource_profile": {"device": "cpu", "batchable": True},
                },
            ],
        }
    }
    member = await attach_managed(
        db_session,
        project,
        actor,
        size=(128, 96),
        roi_marker=True,
        origin=getattr(request, "param", "direct"),
    )
    parent = await AnnotationService(db_session).create(
        task_id=member.task.id,
        user_id=actor.id,
        annotation_type="bbox",
        class_name="car",
        geometry={"type": "bbox", "x": 0.25, "y": 0.25, "w": 0.5, "h": 0.5},
        tool_unit_id="bbox",
        attributes={"color": "red", "brand": "BMW"},
    )
    parent.attributes_meta = {
        "brand": {"origin": "ai", "model_ref": {"model_id": "old"}}
    }
    await db_session.commit()
    world = SimpleNamespace(
        sources=sync_world,
        inputs=sync_world.inputs,
        factory=sync_world.factory,
        actor_id=actor.id,
        task_id=member.task.id,
        project_id=project.id,
        annotation_id=parent.id,
        backend_id=backend.id,
        member=member,
        original_attributes=deepcopy(parent.attributes),
        original_meta=deepcopy(parent.attributes_meta),
        original_version=parent.version,
        calls=[],
        during=None,
        failure=None,
        geometry=False,
        child_class="plate",
        empty=False,
        barrier=None,
    )

    async def predict(self, tasks, context=None, **kwargs):
        assert len(tasks) == 1 and tasks[0]["id"] == "0"
        parsed = urlsplit(tasks[0]["file_path"])
        bucket, key = parsed.path.lstrip("/").split("/", 1)
        version = parse_qs(parsed.query)["versionId"][0]
        assert version not in {"", "null"} and "/original/" not in key
        lease_id = uuid.UUID(key.split("/")[1])
        raw = world.inputs.versions[bucket, key][version][0]
        async with world.factory() as db:
            owner = await db.get(PredictionInputLease, lease_id)
            assert owner.owner_kind == "secondary_inference" and owner.object_count == 1
            relative = key[len(owner.storage_ref["key_prefix"]) :]
            receipt = owner.inventory["objects"][relative]
            assert receipt["state"] == "verified" and receipt["version_id"] == version
            assert receipt["sha256"] == hashlib.sha256(raw).hexdigest()
            assert receipt["size_bytes"] == len(raw)
        with Image.open(io.BytesIO(raw)) as crop:
            assert crop.size == (64, 48) and crop.format == "JPEG"
            rgb = crop.convert("RGB")
            try:
                for point in ((0, 0), (32, 24), (63, 47)):
                    assert all(
                        abs(a - b) <= 2
                        for a, b in zip(
                            rgb.getpixel(point), (100, 150, 200), strict=True
                        )
                    )
            finally:
                rgb.close()
        world.calls.append(
            {
                "lease_id": lease_id,
                "key": key,
                "version": version,
                "context": deepcopy(context),
            }
        )
        index = len(world.calls) - 1
        if world.during:
            await world.during()
        if world.barrier:
            await world.barrier(index)
        if world.failure:
            raise world.failure
        if world.empty:
            result = []
        elif world.geometry:
            result = [
                {
                    "type": "rectanglelabels",
                    "value": {
                        "x": 20.0,
                        "y": 20.0,
                        "width": 40.0,
                        "height": 40.0,
                        "rectanglelabels": [world.child_class],
                    },
                    "score": 0.8,
                }
            ]
        else:
            result = [{"attributes": {"color": "blue", "brand": "Audi"}, "score": 0.9}]
        return [PredictionResult(task_id="0", result=result)]

    monkeypatch.setattr("app.services.ml_client.MLBackendClient.predict", predict)
    return world


def auth(actor):
    return {"Authorization": "Bearer " + actor[1]}


async def submit(client, world, headers, **overrides):
    annotation_id = overrides.pop("annotation_id", world.annotation_id)
    return await client.post(
        f"/api/v1/tasks/{world.task_id}/annotations/{annotation_id}/secondary-inference",
        headers=headers,
        json={
            "ml_backend_id": str(world.backend_id),
            "write_target": "geometry" if world.geometry else "attributes",
            "model_id": "det" if world.geometry else "cls",
            "task": "detect" if world.geometry else "classify",
            "pad": 0,
            **overrides,
        },
    )


async def clean(world, monkeypatch, *, require_records=True):
    await assert_inputs_retired(
        world.sources, world.task_id, monkeypatch, require_records=require_records
    )


async def assert_no_results(world, *, expected_attributes=None, allow_audit=False):
    async with world.factory() as db:
        parent = await db.get(Annotation, world.annotation_id)
        assert parent.attributes == (
            world.original_attributes
            if expected_attributes is None
            else expected_attributes
        )
        assert not await db.scalar(
            select(Annotation.id).where(
                Annotation.parent_annotation_id == world.annotation_id
            )
        )
        assert not await db.scalar(
            select(Prediction.id).where(Prediction.task_id == world.task_id)
        )
        if not allow_audit:
            assert not await db.scalar(
                select(AuditLog.id).where(
                    AuditLog.target_id == str(world.annotation_id),
                    AuditLog.detail_json["secondary_inference"].as_boolean().is_(True),
                )
            )


@pytest.mark.parametrize(
    "secondary_world", ["direct", "primary", "task"], indirect=True
)
async def test_attributes_use_owned_roi_and_preserve_manual_values(
    httpx_client,
    secondary_world,
    super_admin,
    monkeypatch,
):
    world = secondary_world
    response = await submit(httpx_client, world, auth(super_admin))
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["created_children"] == []
    assert body["annotation"]["attributes"] == {"color": "red", "brand": "Audi"}
    meta = body["annotation"]["attributes_meta"]
    assert "color" not in meta and meta["brand"]["origin"] == "ai"
    assert meta["brand"]["model_ref"] == {
        "backend_id": str(world.backend_id),
        "model_id": "cls",
    }
    assert body["annotation"]["version"] == world.original_version + 1
    assert len(world.calls) == 1 and len(world.sources.captured) == 1
    assert world.sources.captured[0].bucket == world.member.source.bucket
    assert len(world.inputs.puts) == 1
    async with world.factory() as db:
        assert not await db.scalar(
            select(Prediction.id).where(Prediction.task_id == world.task_id)
        )
    await clean(world, monkeypatch)


@pytest.mark.parametrize("unknown", [False, True])
async def test_geometry_remaps_to_one_level_child_with_unknown_class_fallback(
    httpx_client,
    secondary_world,
    super_admin,
    monkeypatch,
    unknown,
):
    world = secondary_world
    world.geometry = True
    if unknown:
        async with world.factory() as db:
            project = await db.get(Project, world.project_id)
            project.tool_bindings = build_tool_bindings(["car"])
            await db.commit()
    response = await submit(httpx_client, world, auth(super_admin))
    assert response.status_code == 201, response.text
    child = response.json()["created_children"][0]
    assert child["class_name"] == ("__unknown" if unknown else "plate")
    assert child["parent_annotation_id"] == str(world.annotation_id)
    assert child["source"] == "prediction_based"
    for key, expected in {"x": 0.35, "y": 0.35, "w": 0.2, "h": 0.2}.items():
        assert child["geometry"][key] == pytest.approx(expected)
    assert response.json()["annotation"]["version"] == world.original_version
    rejected = await submit(
        httpx_client, world, auth(super_admin), annotation_id=child["id"]
    )
    assert rejected.status_code == 400, rejected.text
    assert len(world.calls) == 1
    await clean(world, monkeypatch)


@pytest.mark.parametrize(
    "change",
    [
        "key",
        "role",
        "task",
        "batch",
        "source",
        "lock",
        "parent_version",
        "parent_geometry",
        "parent_attributes",
        "parent_meta",
        "parent_activity",
        "parent_lock",
    ],
)
async def test_final_drift_rejects_without_overwriting_current_parent(
    httpx_client,
    db_session,
    secondary_world,
    super_admin,
    annotator,
    monkeypatch,
    change,
):
    world = secondary_world
    await db_session.commit()
    key_id, key_headers = await api_key(
        httpx_client, super_admin, ["annotations:write"]
    )
    batch_id = replacement = None
    async with world.factory() as db:
        if change == "batch":
            batch = await create_batch(
                db, project_id=world.project_id, status="annotating"
            )
            batch_id = batch.id
        if change == "source":
            replacement = await attach_managed(
                db,
                await db.get(Project, world.project_id),
                await db.get(User, world.actor_id),
                size=(128, 96),
                roi_marker=True,
            )
        await db.commit()

    async def alter():
        async with world.factory() as db:
            parent = await db.get(Annotation, world.annotation_id)
            if change == "key":
                (await db.get(ApiKey, key_id)).revoked_at = datetime.now(timezone.utc)
            elif change == "role":
                (await db.get(User, world.actor_id)).role = "viewer"
            elif change == "task":
                (await db.get(Task, world.task_id)).version += 1
            elif change == "batch":
                (await db.get(Task, world.task_id)).batch_id = batch_id
            elif change == "source":
                task = await db.get(Task, world.task_id)
                task.dataset_item_id, task.file_path = (
                    replacement.item.id,
                    replacement.source.object_key,
                )
            elif change == "lock":
                db.add(
                    TaskLock(
                        task_id=world.task_id,
                        user_id=annotator[0].id,
                        unique_id=uuid.uuid4(),
                        expire_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                    )
                )
            elif change == "parent_version":
                parent.version += 1
            elif change == "parent_geometry":
                parent.geometry = {**parent.geometry, "x": 0.2}
            elif change == "parent_attributes":
                parent.attributes = {**parent.attributes, "brand": "Human edit"}
            elif change == "parent_meta":
                parent.attributes_meta = {}
            elif change == "parent_activity":
                parent.was_cancelled = True
            else:
                parent.is_locked = True
            await db.commit()

    world.during = alter
    response = await submit(httpx_client, world, key_headers)
    assert response.status_code in {403, 409}, response.text
    await assert_no_results(
        world,
        expected_attributes={"color": "red", "brand": "Human edit"}
        if change == "parent_attributes"
        else None,
    )
    await clean(world, monkeypatch)


@pytest.mark.parametrize("change", ["key", "parent", "source"])
async def test_read_boundary_drift_never_reaches_model(
    httpx_client,
    secondary_world,
    super_admin,
    monkeypatch,
    change,
):
    world = secondary_world
    key_id, key_headers = await api_key(
        httpx_client, super_admin, ["annotations:write"]
    )
    loop = asyncio.get_running_loop()

    async def alter():
        async with world.factory() as db:
            if change == "key":
                (await db.get(ApiKey, key_id)).revoked_at = datetime.now(timezone.utc)
            elif change == "parent":
                (await db.get(Annotation, world.annotation_id)).version += 1
            else:
                (await db.get(Task, world.task_id)).file_name = "changed-name.png"
            await db.commit()

    def capture(spec):
        asyncio.run_coroutine_threadsafe(alter(), loop).result(5)

    world.sources.capture_hook = capture
    response = await submit(httpx_client, world, key_headers)
    assert response.status_code in {403, 409}, response.text
    assert not world.calls
    await assert_no_results(world)
    await clean(world, monkeypatch, require_records=False)


async def test_routing_key_revocation_prevents_crop_dispatch(
    httpx_client,
    secondary_world,
    super_admin,
    monkeypatch,
):
    world = secondary_world
    key_id, key_headers = await api_key(
        httpx_client, super_admin, ["annotations:write"]
    )
    original = RoutedMLBackendClient._routed_transport
    selected = []

    @asynccontextmanager
    async def revoke(self, **kwargs):
        async with original(self, **kwargs) as transport:
            assert self.last_instance_id == world.backend_id
            selected.append(self.last_instance_id)
            async with world.factory() as db:
                (await db.get(ApiKey, key_id)).revoked_at = datetime.now(timezone.utc)
                await db.commit()
            yield transport

    monkeypatch.setattr(RoutedMLBackendClient, "_routed_transport", revoke)
    response = await submit(httpx_client, world, key_headers)
    assert selected == [world.backend_id] and response.status_code == 403, response.text
    assert not world.calls
    await assert_no_results(world)
    await clean(world, monkeypatch)


async def test_overlapping_geometry_requests_have_independent_crop_versions(
    httpx_client,
    secondary_world,
    super_admin,
    monkeypatch,
):
    world = secondary_world
    world.geometry = True
    arrived = asyncio.Event()
    releases = [asyncio.Event(), asyncio.Event()]

    async def barrier(index):
        if len(world.calls) == 2:
            arrived.set()
        await releases[index].wait()

    world.barrier = barrier
    first = asyncio.create_task(submit(httpx_client, world, auth(super_admin)))
    while not world.calls and not first.done():
        await asyncio.sleep(0.01)
    second = asyncio.create_task(submit(httpx_client, world, auth(super_admin)))
    try:
        await asyncio.wait_for(arrived.wait(), 5)
        assert len({call["lease_id"] for call in world.calls}) == 2
        assert len({call["key"] for call in world.calls}) == 2
        assert len({call["version"] for call in world.calls}) == 2
        releases[0].set()
        one = await asyncio.wait_for(first, 5)
        releases[1].set()
        two = await asyncio.wait_for(second, 5)
        assert one.status_code == two.status_code == 201, (one.text, two.text)
        async with world.factory() as db:
            children = list(
                await db.scalars(
                    select(Annotation).where(
                        Annotation.parent_annotation_id == world.annotation_id
                    )
                )
            )
            assert len(children) == 2
    finally:
        for release in releases:
            release.set()
        await asyncio.gather(first, second, return_exceptions=True)
    await clean(world, monkeypatch)


@pytest.mark.parametrize(
    "scopes,allowed",
    [([], False), (["tasks:read"], False), (["annotations:write"], True)],
)
async def test_secondary_keeps_exact_key_write_scope_and_ignores_interactive_toggle(
    httpx_client,
    secondary_world,
    super_admin,
    monkeypatch,
    scopes,
    allowed,
):
    world = secondary_world
    _, headers = await api_key(httpx_client, super_admin, scopes)
    response = await submit(httpx_client, world, headers)
    assert response.status_code == (201 if allowed else 403), response.text
    assert bool(world.calls) == allowed
    await clean(world, monkeypatch, require_records=allowed)


async def test_reviewer_has_no_secondary_write_exception(
    httpx_client,
    db_session,
    secondary_world,
    reviewer,
    super_admin,
    monkeypatch,
):
    world = secondary_world
    await create_membership(
        db_session,
        project_id=world.project_id,
        user_id=reviewer[0].id,
        role="reviewer",
        assigned_by=world.actor_id,
    )
    await db_session.commit()
    response = await submit(httpx_client, world, auth(reviewer))
    assert response.status_code == 403, response.text
    assert not world.calls
    await clean(world, monkeypatch, require_records=False)


@pytest.mark.parametrize(
    "failure,status",
    [
        ("timeout", 504),
        ("connect", 502),
        ("upstream", 502),
        ("empty", 201),
        ("tiny", 422),
    ],
)
async def test_failures_and_empty_detections_keep_legacy_semantics(
    httpx_client,
    secondary_world,
    super_admin,
    monkeypatch,
    failure,
    status,
):
    world = secondary_world
    if failure == "timeout":
        world.failure = httpx.ReadTimeout("test timeout")
    elif failure == "connect":
        world.failure = httpx.ConnectError("test connection")
    elif failure == "upstream":
        request = httpx.Request("POST", "http://secondary.test/predict")
        world.failure = httpx.HTTPStatusError(
            "test upstream",
            request=request,
            response=httpx.Response(500, request=request),
        )
    elif failure == "empty":
        world.empty = True
    else:
        async with world.factory() as db:
            parent = await db.get(Annotation, world.annotation_id)
            parent.geometry = {"type": "bbox", "x": 0.5, "y": 0.5, "w": 0.01, "h": 0.01}
            await db.commit()
    response = await submit(httpx_client, world, auth(super_admin))
    assert response.status_code == status, response.text
    await assert_no_results(world, allow_audit=failure == "empty")
    await clean(world, monkeypatch)


async def test_repeated_cancel_drains_source_before_input_retirement(
    httpx_client,
    secondary_world,
    super_admin,
    monkeypatch,
):
    world = secondary_world
    started, release = threading.Event(), threading.Event()

    def pause(spec):
        started.set()
        if not release.wait(5):
            raise TimeoutError("test source read gate")

    world.sources.capture_hook = pause
    pending = asyncio.create_task(submit(httpx_client, world, auth(super_admin)))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done()
        async with world.factory() as db:
            assert await db.scalar(select(StorageOperation.id))
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
    assert not world.calls
    await assert_no_results(world)
    await clean(world, monkeypatch)


async def test_unknown_source_close_blocks_retirement_even_after_retention_clock(
    httpx_client,
    secondary_world,
    super_admin,
    monkeypatch,
):
    from app.services import prediction_inputs
    from app.services.content_snapshot import ContentError

    world = secondary_world

    def unknown(stream):
        raise OSError("test source closure is unconfirmed")

    world.sources.source_close_hook = unknown
    try:
        try:
            response = await submit(httpx_client, world, auth(super_admin))
        except (ContentError, OSError):
            pass
        else:
            assert response.status_code == 503, response.text
        async with world.factory() as db:
            leases = list(
                await db.scalars(
                    select(PredictionInputLease).where(
                        PredictionInputLease.task_id == world.task_id,
                    )
                )
            )
            assert leases and all(
                row.state == "cleanup_pending" and row.uncertain_write for row in leases
            )
            operations = list(await db.scalars(select(StorageOperation)))
            assert any(
                op.purpose == prediction_inputs.purpose(leases[0].id)
                for op in operations
            )
        future = prediction_inputs.now() + timedelta(days=20)
        with monkeypatch.context() as patch:
            patch.setattr(prediction_inputs, "now", lambda: future)
            assert (
                await prediction_inputs.purge_input(world.factory, leases[0].id)
                == "deferred"
            )
        async with world.factory() as db:
            assert (
                await db.get(PredictionInputLease, leases[0].id)
            ).state == "cleanup_pending"
            assert await db.scalar(select(StorageOperation.id))
    finally:
        # Only this disposable namespace is dropped; no unresolved ledger is
        # falsely settled to accommodate an idempotent second close.
        world.sources.source_close_hook = None


async def test_post_write_jwt_logout_rolls_back_attributes_and_audit(
    httpx_client,
    secondary_world,
    super_admin,
    monkeypatch,
):
    import os
    from app.api.v1.tasks import annotations as route
    from app.core import token_blacklist
    from app.core.security import decode_access_token

    assert os.environ.get("AAP_WORKTREE_MODE") == "test"
    world = secondary_world
    claims = decode_access_token(super_admin[1])
    jti = claims["jti"]
    assert not await token_blacklist.is_blacklisted(jti)
    original = route.AuditService.log
    writes = []

    async def log_then_logout(*args, **kwargs):
        result = await original(*args, **kwargs)
        if (kwargs.get("detail") or {}).get("secondary_inference"):
            writes.append(True)
            remaining = int(claims["exp"] - datetime.now(timezone.utc).timestamp())
            assert remaining > 0
            await token_blacklist.blacklist_token(jti, remaining)
        return result

    monkeypatch.setattr(route.AuditService, "log", log_then_logout)
    try:
        response = await submit(httpx_client, world, auth(super_admin))
        assert writes and response.status_code == 403, response.text
        await assert_no_results(world)
        await clean(world, monkeypatch)
    finally:
        redis = token_blacklist._get_redis()
        try:
            await redis.delete(token_blacklist._KEY_PREFIX + jti)
        finally:
            await redis.aclose()
