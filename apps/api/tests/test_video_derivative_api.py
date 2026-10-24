"""HTTP ownership and authorization contracts without provider or decoder I/O.

The database owns real registered originals, immutable outputs and logical
pointers. Only the physical boundary is replaced: its double rechecks actual
current authority and receipts after the route has committed its initial work.
"""

from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace
import uuid

from fastapi import HTTPException, Request
import pytest
from sqlalchemy import select, update

from app.db.models.dataset import (
    Dataset,
    DatasetItem,
    VideoChunk,
    VideoFrameCache,
    VideoFrameIndex,
)
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.db.models.video_derivative import VideoDerivativeOutput as Output
from app.services import api_key_service, storage_access
from app.services import video_derivatives as core
from app.services import video_derivative_objects as objects
from app.services import video_derivative_originals as originals
from app.services import video_derivative_storage as physical
from app.services import video_frame_service as service
from app.services.content_snapshot import ContentError
from app.services.dataset_assets import LEGACY_LOCATION_ID, register_item_asset
from app.services.storage import storage_service


def _headers(actor):
    return {"Authorization": f"Bearer {actor[1]}"}


def _request(actor):
    return Request(
        {"type": "http", "headers": [(b"authorization", f"Bearer {actor[1]}".encode())]}
    )


@pytest.fixture
async def world(db_session, super_admin, annotator, monkeypatch):
    owner = super_admin[0]
    project = Project(
        display_id="VP-" + uuid.uuid4().hex[:10],
        name="Video owners",
        type_key="video-track",
        type_label="Video",
        data_type="video",
        owner_id=owner.id,
    )
    dataset = Dataset(
        display_id="VD-" + uuid.uuid4().hex[:10],
        name="Videos",
        data_type="video",
        created_by=owner.id,
    )
    db_session.add_all([project, dataset])
    await db_session.flush()
    raw = b"registered-video-without-provider-io"
    item = DatasetItem(
        dataset_id=dataset.id,
        file_name="source.mp4",
        file_path=f"video-api-tests/{uuid.uuid4()}/source.mp4",
        file_type="video",
        file_size=len(raw),
        metadata_={
            "video": {
                "fps": 999,
                "frame_count": 999,
                "poster_frame_path": "legacy/poster.webp",
                "playback_path": "legacy/transcode.mp4",
            }
        },
    )
    db_session.add(item)
    await db_session.flush()
    asset = await register_item_asset(
        db_session, item, sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw)
    )
    await storage_access.ensure(db_session, LEGACY_LOCATION_ID)
    task = Task(
        display_id="VT-" + uuid.uuid4().hex[:10],
        project_id=project.id,
        dataset_item_id=item.id,
        file_name=item.file_name,
        file_path=item.file_path,
        file_type="video",
        status="pending",
        assignee_id=annotator[0].id,
    )
    member = ProjectMember(
        project_id=project.id, user_id=annotator[0].id, role="annotator"
    )
    db_session.add_all([task, member])
    await db_session.flush()
    authority = await originals.authorize(
        db_session, task.id, owner, _request(super_admin)
    )
    calls, dispatched = [], []

    async def verified(db, output, authority, *, relative=None, bytes_limit=None):
        assert not db.in_transaction(), (
            "initial locks must be released before physical I/O"
        )
        core.validate_result(output, output.result, output.inventory)
        await core.check(db, output, authority)
        await db.commit()
        calls.append((output.id, relative, authority.owner_id))
        if bytes_limit is not None:
            return b"image-bytes"
        return f"https://saved-owner.test/{output.id}/{relative}" if relative else None

    def dispatch(ids):
        assert not db_session.in_transaction()
        if ids:
            dispatched.append(list(ids))

    def no_raw_signature(*args, **kwargs):
        pytest.fail("raw video/cache keys must never be signed")

    monkeypatch.setattr(core, "verified", verified)
    monkeypatch.setattr(core, "dispatch", dispatch)
    monkeypatch.setattr(storage_service, "generate_download_url", no_raw_signature)
    monkeypatch.setattr(service.SystemSettingsService, "get", _no_warmup)
    return SimpleNamespace(
        task=task,
        item=item,
        asset=asset,
        project=project,
        member=member,
        authority=authority,
        calls=calls,
        dispatched=dispatched,
    )


async def _no_warmup(*args, **kwargs):
    return 0


def _inventory(output, result, payload=None):
    serialized = json.dumps(
        result,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    values = {"metadata.json": serialized, **(payload or {})}
    owner = physical.resolve(output)
    return {
        "schema": 1,
        "objects": {
            owner.key(name): {
                "size_bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
            for name, raw in values.items()
        },
    }


async def _publish(
    db, world, kind="metadata", spec=None, *, poster=False, diagnostics=None
):
    output, _ = await core.prepare(db, world.item.id, world.authority, kind, spec=spec)
    token = await objects.claim(db, output)
    if kind == "metadata":
        result = {
            "schema": 1,
            "kind": "metadata",
            "video": {
                "fps": 30,
                "frame_count": 90,
                "width": 640,
                "height": 480,
                "duration_ms": 3000,
                "poster_frame_path": physical.resolve(output).key("poster.webp")
                if poster
                else None,
            },
            "frames": [
                {
                    "frame_index": index,
                    "pts_ms": pts,
                    "is_keyframe": index == 0,
                    "pict_type": "I" if index == 0 else "P",
                    "byte_offset": index * 100,
                }
                for index, pts in [(0, -50), (3, 50), (9, 250)]
            ],
        }
        payload = {"poster.webp": b"owned-poster"} if poster else {}
    else:
        result = {
            "schema": 1,
            "kind": kind,
            "spec": deepcopy(output.spec),
            "metadata_output_id": str(output.metadata_output_id),
        }
        relative = f"frame.{spec['format']}" if kind == "frame" else "chunk.mp4"
        payload = {relative: b"owned-frame" if kind == "frame" else b"owned-chunk"}
        if kind == "chunk":
            result.update(generation_mode="transcode", diagnostics=diagnostics or {})
    inventory = _inventory(output, result, payload)
    revision = await storage_access.check(db, LEGACY_LOCATION_ID)
    await core.publish(db, output, world.authority, token, result, inventory, revision)
    await db.commit()
    return output


@pytest.mark.parametrize("surface", ["task", "facade"])
async def test_unowned_metadata_prepares_committed_owner_and_never_signs_legacy_paths(
    world, db_session, httpx_client, super_admin, surface
):
    path = (
        f"/api/v1/tasks/{world.task.id}/video/manifest"
        if surface == "task"
        else f"/api/v1/videos/{world.item.id}/manifest"
    )
    first = await httpx_client.get(path, headers=_headers(super_admin))
    assert first.status_code == 503
    assert first.json()["detail"]["code"] == "video_metadata_preparing"
    await db_session.refresh(world.item)
    identifier = world.item.video_building_output_id
    assert identifier is not None and world.item.video_output_id is None
    output = await db_session.get(Output, identifier)
    assert output.original_ref["content_id"] == str(world.asset.id)
    assert output.auth_ref["owner_id"] == str(world.task.id)
    await db_session.commit()
    second = await httpx_client.get(path, headers=_headers(super_admin))
    assert second.status_code == 503
    await db_session.refresh(world.item)
    assert world.item.video_building_output_id == identifier
    assert world.calls == []
    assert world.dispatched == [[identifier], [identifier]]


@pytest.mark.parametrize("scopes", [[], ["datasets:read"], ["tasks:read"]])
@pytest.mark.parametrize("surface", ["task", "facade"])
async def test_existing_visible_video_read_adds_no_api_key_scopes(
    world, db_session, httpx_client, super_admin, scopes, surface
):
    generation = await _publish(db_session, world, poster=True)
    _, token = await api_key_service.create_key(
        db_session, super_admin[0], "existing-reader", scopes=scopes
    )
    await db_session.commit()
    path = (
        f"/api/v1/tasks/{world.task.id}/video/manifest"
        if surface == "task"
        else f"/api/v1/videos/{world.item.id}/manifest"
    )
    response = await httpx_client.get(
        path, headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["metadata_generation_id"] == str(generation.id)
    assert body["video_url"] is None
    assert body["poster_url"] == f"https://saved-owner.test/{generation.id}/poster.webp"
    assert world.calls[-1] == (generation.id, "poster.webp", world.task.id)
    if surface == "task":
        assert body["original_identity_path"].endswith(
            f"/tasks/{world.task.id}/original-identity"
        )


@pytest.mark.parametrize("surface", ["task", "facade"])
async def test_invisible_and_inactive_actors_never_enqueue_or_read(
    world, db_session, httpx_client, reviewer, surface
):
    path = (
        f"/api/v1/tasks/{world.task.id}/video/manifest"
        if surface == "task"
        else f"/api/v1/videos/{world.item.id}/manifest"
    )
    assert (await httpx_client.get(path, headers=_headers(reviewer))).status_code in {
        403,
        404,
    }
    reviewer[0].is_active = False
    await db_session.flush()
    assert (await httpx_client.get(path, headers=_headers(reviewer))).status_code == 401
    assert world.calls == [] and world.dispatched == []


@pytest.mark.parametrize("surface", ["task", "facade"])
async def test_timetable_uses_frozen_generation_and_rejects_generation_mismatch(
    world, db_session, httpx_client, super_admin, surface
):
    generation = await _publish(db_session, world)
    await db_session.execute(
        update(VideoFrameIndex)
        .where(VideoFrameIndex.dataset_item_id == world.item.id)
        .values(pts_ms=123456)
    )
    await db_session.commit()
    path = (
        f"/api/v1/tasks/{world.task.id}/video/frame-timetable"
        if surface == "task"
        else f"/api/v1/videos/{world.item.id}/frame-timetable"
    )
    mismatch = await httpx_client.get(
        path,
        headers=_headers(super_admin),
        params={"expected_metadata_generation_id": str(uuid.uuid4())},
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["detail"]["code"] == "video_metadata_generation_changed"
    assert world.calls == []
    response = await httpx_client.get(
        path,
        headers=_headers(super_admin),
        params={
            "expected_metadata_generation_id": str(generation.id),
            "from": 0,
            "to": 3,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["metadata_generation_id"] == str(generation.id)
    assert [
        (row["frame_index"], row["pts_ms"]) for row in response.json()["frames"]
    ] == [(0, -50), (3, 50)]
    assert str(generation.id) in response.headers["etag"]
    assert world.calls == [(generation.id, None, world.task.id)]


async def test_frame_batch_replaces_unowned_ready_slots_with_one_dispatch(
    world, db_session, httpx_client, super_admin
):
    generation = await _publish(db_session, world)
    legacy = VideoFrameCache(
        dataset_item_id=world.item.id,
        frame_index=0,
        width=512,
        format="webp",
        status="ready",
        storage_key="videos/legacy/frame.webp",
        byte_size=99,
    )
    db_session.add(legacy)
    await db_session.commit()
    response = await httpx_client.post(
        f"/api/v1/tasks/{world.task.id}/video/frames:prefetch",
        headers=_headers(super_admin),
        json={"frame_indices": [0, 1, 1]},
    )
    assert response.status_code == 200, response.text
    frames = response.json()["frames"]
    assert len(frames) == 2 and all(frame["status"] == "pending" for frame in frames)
    assert all(frame["url"] is None and frame["sha256"] is None for frame in frames)
    assert all(
        frame["metadata_generation_id"] == str(generation.id) for frame in frames
    )
    assert len(world.dispatched) == 1
    assert set(map(str, world.dispatched[0])) == {
        frame["output_id"] for frame in frames
    }
    await db_session.refresh(legacy)
    assert legacy.storage_key is None and legacy.output_id is not None
    assert world.calls == []


async def test_ready_frame_receipt_and_force_retry_keep_old_physical_owner(
    world, db_session, httpx_client, super_admin
):
    generation = await _publish(db_session, world)
    output = await _publish(
        db_session, world, "frame", core.frame_spec(generation, 3, 512, "webp")
    )
    previous = deepcopy(output.inventory)
    path = f"/api/v1/tasks/{world.task.id}/video/frames/3"
    response = await httpx_client.get(path, headers=_headers(super_admin))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["output_id"] == str(output.id)
    assert body["metadata_generation_id"] == str(generation.id)
    assert body["sha256"] == hashlib.sha256(b"owned-frame").hexdigest()
    assert body["byte_size"] == len(b"owned-frame")
    retried = await httpx_client.post(
        f"/api/v1/tasks/{world.task.id}/video/frames:retry",
        headers=_headers(super_admin),
        json={"frame_indices": [3], "force": True},
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["frames"][0]["output_id"] != str(output.id)
    await db_session.refresh(output)
    assert output.state == "ready" and output.inventory == previous


async def test_chunk_samples_full_verify_exact_output_and_ignore_mutable_row_diagnostics(
    world, db_session, httpx_client, super_admin
):
    generation = await _publish(db_session, world)
    diagnostics = {
        "codec_string": "avc1.4d001e",
        "width": 640,
        "height": 480,
        "samples": [
            {
                "frame_index": 0,
                "pts_ms": -50,
                "duration_ms": 33,
                "is_keyframe": True,
                "size_bytes": 5,
                "offset_in_chunk": 4,
            }
        ],
    }
    output = await _publish(
        db_session,
        world,
        "chunk",
        core.chunk_spec(generation, 0),
        diagnostics=diagnostics,
    )
    row = await db_session.scalar(
        select(VideoChunk).where(
            VideoChunk.dataset_item_id == world.item.id, VideoChunk.chunk_id == 0
        )
    )
    row.diagnostics = {"codec_string": "wrong", "samples": []}
    row.storage_key = "videos/legacy/other.mp4"
    await db_session.commit()
    path = f"/api/v1/videos/{world.item.id}/chunks/0/samples"
    mismatch = await httpx_client.get(
        path,
        headers=_headers(super_admin),
        params={"expected_output_id": str(uuid.uuid4())},
    )
    assert mismatch.status_code == 409 and world.calls == []
    response = await httpx_client.get(
        path,
        headers=_headers(super_admin),
        params={"expected_output_id": str(output.id)},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["samples"] == diagnostics["samples"]
    assert body["codec_string"] == diagnostics["codec_string"]
    assert body["output_id"] == str(output.id)
    assert body["metadata_generation_id"] == str(generation.id)
    assert body["sha256"] == hashlib.sha256(b"owned-chunk").hexdigest()
    assert world.calls == [(output.id, "chunk.mp4", world.task.id)]


@pytest.mark.parametrize("code", ["missing", "integrity_mismatch", "invalid_receipt"])
async def test_physical_integrity_failures_create_fresh_attempt_without_touching_ready_owner(
    world, db_session, httpx_client, super_admin, monkeypatch, code
):
    generation = await _publish(db_session, world)
    output = await _publish(
        db_session, world, "frame", core.frame_spec(generation, 0, 512, "webp")
    )

    async def corrupt(*args, **kwargs):
        raise physical.VideoDerivativeStorageError(code)

    monkeypatch.setattr(core, "verified", corrupt)
    response = await httpx_client.get(
        f"/api/v1/tasks/{world.task.id}/video/frames/0", headers=_headers(super_admin)
    )
    assert response.status_code == 202, response.text
    assert response.json()["output_id"] != str(output.id)
    assert response.json()["status"] == "pending"
    await db_session.refresh(output)
    assert output.state == "ready"
    assert len(world.dispatched) == 1 and len(world.dispatched[0]) == 1


@pytest.mark.parametrize("failure", ["authority", "gate", "unsettled"])
async def test_authority_gate_and_unknown_close_failures_never_become_cache_misses(
    world, db_session, httpx_client, super_admin, monkeypatch, failure
):
    generation = await _publish(db_session, world)
    output = await _publish(
        db_session, world, "frame", core.frame_spec(generation, 0, 512, "webp")
    )

    async def denied(*args, **kwargs):
        if failure == "authority":
            raise ContentError("access_revoked", 403)
        if failure == "gate":
            raise ContentError("storage_maintenance_active")
        raise storage_access.mark_unsettled(
            physical.VideoDerivativeStorageError("missing")
        )

    monkeypatch.setattr(core, "verified", denied)
    response = await httpx_client.get(
        f"/api/v1/tasks/{world.task.id}/video/frames/0", headers=_headers(super_admin)
    )
    assert response.status_code in {403, 404, 409}
    slot = await db_session.scalar(
        select(VideoFrameCache).where(VideoFrameCache.dataset_item_id == world.item.id)
    )
    assert slot.output_id == output.id and world.dispatched == []


async def test_task_retry_retains_extra_write_permission_while_facade_keeps_existing_visibility(
    world, db_session, httpx_client, reviewer
):
    await _publish(db_session, world)
    world.task.reviewer_id = reviewer[0].id
    db_session.add(
        ProjectMember(
            project_id=world.project.id, user_id=reviewer[0].id, role="reviewer"
        )
    )
    await db_session.commit()
    payload = {"frame_indices": [1], "force": True}
    task_response = await httpx_client.post(
        f"/api/v1/tasks/{world.task.id}/video/frames:retry",
        headers=_headers(reviewer),
        json=payload,
    )
    assert task_response.status_code == 403
    facade_response = await httpx_client.post(
        f"/api/v1/videos/{world.item.id}/frames:retry",
        headers=_headers(reviewer),
        json=payload,
    )
    assert facade_response.status_code == 200, facade_response.text
    assert facade_response.json()["frames"][0]["status"] == "pending"


async def test_pure_context_remains_compatible_but_none_authority_cannot_enqueue(
    world, db_session
):
    ctx = await service.build_context_from_task(db_session, world.task)
    assert ctx.authority is None and ctx.metadata.fps == 999
    with pytest.raises(HTTPException) as caught:
        await service.get_frame(db_session, ctx, 0, 512, "webp")
    assert caught.value.status_code == 403
    assert world.calls == [] and world.dispatched == []
    with pytest.raises(HTTPException) as caught:
        service._asset_url("videos/legacy/frame.webp")
    assert caught.value.status_code == 409


async def test_unregistered_legacy_original_is_refused_without_preparing(
    world, db_session, httpx_client, super_admin
):
    world.task.dataset_item_id = None
    await db_session.commit()
    response = await httpx_client.get(
        f"/api/v1/tasks/{world.task.id}/video/manifest", headers=_headers(super_admin)
    )
    assert response.status_code == 409, response.text
    assert world.calls == [] and world.dispatched == []


async def test_facade_keeps_chosen_task_authority_when_another_task_becomes_visible(
    world, db_session, httpx_client, annotator, monkeypatch
):
    generation = await _publish(db_session, world)
    await _publish(
        db_session, world, "frame", core.frame_spec(generation, 0, 512, "webp")
    )
    chosen = world.task.id
    captured = []

    async def revoke_chosen(db, output, authority, **kwargs):
        captured.append(authority.owner_id)
        world.task.assignee_id = None
        db.add(
            Task(
                display_id="VT-" + uuid.uuid4().hex[:10],
                project_id=world.project.id,
                dataset_item_id=world.item.id,
                file_name=world.item.file_name,
                file_path=world.item.file_path,
                file_type="video",
                status="pending",
                assignee_id=annotator[0].id,
            )
        )
        await db.flush()
        await core.check(db, output, authority)
        pytest.fail("another visible task cannot replace the frozen HTTP authority")

    monkeypatch.setattr(core, "verified", revoke_chosen)
    response = await httpx_client.get(
        f"/api/v1/videos/{world.item.id}/frames/0", headers=_headers(annotator)
    )
    assert response.status_code in {403, 404}
    assert captured == [chosen] and world.dispatched == []


async def test_ready_batch_rechecks_earlier_current_output_after_later_verification(
    world, db_session, httpx_client, super_admin, monkeypatch
):
    generation = await _publish(db_session, world)
    specs = [core.frame_spec(generation, index, 512, "webp") for index in (0, 1)]
    outputs = [await _publish(db_session, world, "frame", spec) for spec in specs]
    verified = core.verified

    async def replace_earlier(db, output, authority, **kwargs):
        value = await verified(db, output, authority, **kwargs)
        if output.id == outputs[1].id:
            await core.prepare(
                db, world.item.id, authority, "frame", spec=specs[0], force=True
            )
            await db.commit()
        return value

    monkeypatch.setattr(core, "verified", replace_earlier)
    response = await httpx_client.post(
        f"/api/v1/tasks/{world.task.id}/video/frames:prefetch",
        headers=_headers(super_admin),
        json={"frame_indices": [0, 1]},
    )
    assert response.status_code == 409
    assert world.dispatched == []


async def test_corrupt_active_metadata_reuses_building_attempt_and_preserves_active_pointer(
    world, db_session, httpx_client, super_admin, monkeypatch
):
    active = await _publish(db_session, world)

    async def corrupted(*args, **kwargs):
        raise ContentError("video_derivative_result_invalid")

    monkeypatch.setattr(core, "verified", corrupted)
    path = f"/api/v1/tasks/{world.task.id}/video/manifest"
    first = await httpx_client.get(path, headers=_headers(super_admin))
    assert first.status_code == 503
    await db_session.refresh(world.item)
    building = world.item.video_building_output_id
    assert building is not None and building != active.id
    await db_session.commit()
    second = await httpx_client.get(path, headers=_headers(super_admin))
    assert second.status_code == 503
    await db_session.refresh(world.item)
    assert world.item.video_building_output_id == building
    assert world.item.video_output_id == active.id
    await db_session.refresh(active)
    assert active.state == "ready"
