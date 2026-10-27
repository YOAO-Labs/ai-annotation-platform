"""Real ownership, provider and HTTP contracts around reusable video stages."""

from datetime import datetime, timedelta, timezone
import hashlib
import uuid

import pytest
from sqlalchemy import select, update

from app.api.v1.original_playback import COOKIE_NAME, playback_registry
from app.core.security import hash_password
from app.db.models.api_key import ApiKey
from app.db.models.dataset import DatasetItem
from app.db.models.dataset_asset import DatasetAssetLocation
from app.db.models.storage_maintenance import StorageMaintenanceGate as Gate
from app.services import original_playback as playback
from app.services import original_binary as originals
from app.services import comment_attachment_authority as credentials
from app.services.dataset_assets import LEGACY_LOCATION_ID, register_item_asset
from app.services.original_playback_registry import PlaybackRegistry
from app.services.storage import storage_service
from tests.test_verified_task_content import headers, stored_world as stored_world


@pytest.fixture
async def registry(app_module, httpx_client):
    owner = PlaybackRegistry()
    app_module.dependency_overrides[playback_registry] = lambda: owner
    try:
        yield owner
    finally:
        await owner.shutdown()
        app_module.dependency_overrides.pop(playback_registry, None)


@pytest.fixture
async def video_world(db_session, stored_world):
    task, _, member, _, _ = stored_world
    previous = await db_session.get(DatasetItem, task.dataset_item_id)
    raw = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 256
    item = DatasetItem(
        dataset_id=previous.dataset_id,
        file_name="original.mp4",
        file_path="video-playback-tests/" + uuid.uuid4().hex + ".mp4",
        file_type="video",
        file_size=len(raw),
    )
    db_session.add(item)
    await db_session.flush()
    asset = await register_item_asset(
        db_session, item, sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw)
    )
    task.dataset_item_id = item.id
    task.file_name, task.file_path, task.file_type = (
        item.file_name,
        item.file_path,
        "video",
    )
    await db_session.flush()
    bucket = storage_service.datasets_bucket
    storage_service.client.put_object(Bucket=bucket, Key=item.file_path, Body=raw)
    try:
        yield task, item, asset, member, raw
    finally:
        storage_service.client.delete_object(Bucket=bucket, Key=item.file_path)


def prepare_path(world, kind="task"):
    task, item, _, _, _ = world
    return (
        f"/api/v1/files/tasks/{task.id}"
        if kind == "task"
        else f"/api/v1/datasets/{item.dataset_id}/items/{item.id}"
    ) + "/original-playback"


def payload(world, session_id=None):
    return {
        "content_id": str(world[2].id),
        "sha256": world[2].checksum_value,
        "session_id": str(session_id or uuid.uuid4()),
    }


async def prepare(client, world, actor, *, kind="task", session_id=None):
    response = await client.post(
        prepare_path(world, kind),
        headers=headers(actor),
        json=payload(world, session_id),
    )
    assert response.status_code == 200, response.text
    return response


@pytest.mark.parametrize("kind", ["task", "item"])
async def test_prepare_and_repeated_ranges_use_one_saved_original_capture(
    httpx_client, registry, video_world, project_admin, monkeypatch, kind
):
    captured = []
    original = playback.capture_video

    async def record(spec):
        captured.append(spec)
        return await original(spec)

    monkeypatch.setattr(playback, "capture_video", record)
    session = uuid.uuid4()
    prepared = await prepare(
        httpx_client, video_world, project_admin, kind=kind, session_id=session
    )
    wire = prepared.json()
    assert set(wire) == {"lease_id", "media_path", "expires_at"}
    cookie = prepared.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert f"Path={wire['media_path']}" in cookie and "Domain=" not in cookie
    assert not any(value in wire["media_path"] for value in ["?", "sha256", "token"])
    raw = video_world[-1]
    for start, end in [(0, 99), (1000, 2010), (len(raw) - 3, len(raw) + 100)]:
        response = await httpx_client.get(
            wire["media_path"], headers={"Range": f"bytes={start}-{end}"}
        )
        assert response.status_code == 206
        assert response.content == raw[start : min(end + 1, len(raw))]
        assert (
            response.headers["content-range"]
            == f"bytes {start}-{min(end, len(raw) - 1)}/{len(raw)}"
        )
        assert response.headers["cache-control"] == "no-store"
    head = await httpx_client.head(wire["media_path"], headers={"Range": "invalid"})
    assert head.status_code == 200 and head.content == b""
    assert head.headers["content-length"] == str(len(raw))
    repeated = await prepare(
        httpx_client, video_world, project_admin, kind=kind, session_id=session
    )
    assert repeated.json()["lease_id"] != wire["lease_id"]
    assert len(captured) == 1
    assert registry.diagnostics()["response_pins"] == 0


async def test_cookie_does_not_authorize_another_lease_and_missing_cookie_is_denied(
    httpx_client, registry, video_world, project_admin
):
    first = await prepare(httpx_client, video_world, project_admin)
    second = await prepare(httpx_client, video_world, project_admin, kind="item")
    secret = first.cookies.get(COOKIE_NAME)
    response = await httpx_client.get(
        second.json()["media_path"], headers={"Cookie": f"{COOKIE_NAME}={secret}"}
    )
    assert response.status_code == 403
    httpx_client.cookies.clear()
    response = await httpx_client.get(first.json()["media_path"])
    assert response.status_code == 403
    assert registry.diagnostics()["response_pins"] == 0


async def test_release_requires_exact_actor_resource_session_then_retires(
    httpx_client, registry, video_world, project_admin, reviewer
):
    session = uuid.uuid4()
    response = await prepare(
        httpx_client, video_world, project_admin, session_id=session
    )
    lease = response.json()
    path = prepare_path(video_world) + "/" + lease["lease_id"]
    wrong = await httpx_client.request(
        "DELETE", path, headers=headers(reviewer), json={"session_id": str(session)}
    )
    assert wrong.status_code == 404
    wrong = await httpx_client.request(
        "DELETE",
        path,
        headers=headers(project_admin),
        json={"session_id": str(uuid.uuid4())},
    )
    assert wrong.status_code == 404
    wrong = await httpx_client.request(
        "DELETE",
        prepare_path(video_world, "item") + "/" + lease["lease_id"],
        headers=headers(project_admin),
        json={"session_id": str(session)},
    )
    assert wrong.status_code == 404
    done = await httpx_client.request(
        "DELETE",
        path,
        headers=headers(project_admin),
        json={"session_id": str(session)},
    )
    assert done.status_code == 204 and "Max-Age=0" in done.headers["set-cookie"]
    expired = await httpx_client.get(lease["media_path"])
    assert expired.status_code == 410
    assert registry.diagnostics()["stages"] == 0


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("change", ["membership", "gate", "binding", "jwt_expiry"])
async def test_every_native_request_rechecks_authority_relation_and_gate(
    httpx_client,
    registry,
    db_session,
    video_world,
    annotator,
    monkeypatch,
    method,
    change,
):
    response = await prepare(httpx_client, video_world, annotator)
    task, _, _, member, _ = video_world
    if change == "membership":
        await db_session.delete(member)
    elif change == "gate":
        await db_session.execute(
            update(Gate)
            .where(Gate.location_id == LEGACY_LOCATION_ID)
            .values(state="stopped", revision=2)
        )
    elif change == "binding":
        task.dataset_item_id = None
    else:
        monkeypatch.setattr(
            credentials, "now", lambda: datetime.now(timezone.utc) + timedelta(days=3)
        )
    await db_session.commit()
    denied = await httpx_client.request(
        method, response.json()["media_path"], headers={"Range": "bytes=999999999-"}
    )
    assert denied.status_code in {403, 404, 409}
    assert "content-range" not in denied.headers
    assert registry.diagnostics()["response_pins"] == 0


@pytest.mark.parametrize("kind", ["task", "item"])
async def test_saved_bucket_is_used_and_corrupt_source_cannot_publish(
    httpx_client, registry, db_session, video_world, project_admin, monkeypatch, kind
):
    _, item, asset, _, raw = video_world
    location = await db_session.scalar(
        select(DatasetAssetLocation).where(DatasetAssetLocation.asset_id == asset.id)
    )
    assert location.bucket == storage_service.datasets_bucket
    trap = storage_service.import_bucket
    storage_service.client.put_object(
        Bucket=trap, Key=item.file_path, Body=b"x" * len(raw)
    )
    try:
        with monkeypatch.context() as patch:
            patch.setattr(storage_service, "datasets_bucket", trap)
            patch.setattr(storage_service, "bucket", trap)
            response = await prepare(
                httpx_client, video_world, project_admin, kind=kind
            )
            downloaded = await httpx_client.get(response.json()["media_path"])
            assert downloaded.content == raw
        await registry.revoke_lease(uuid.UUID(response.json()["lease_id"]))
        storage_service.client.put_object(
            Bucket=location.bucket, Key=item.file_path, Body=b"x" * len(raw)
        )
        denied = await httpx_client.post(
            prepare_path(video_world, kind),
            headers=headers(project_admin),
            json=payload(video_world),
        )
        assert denied.status_code == 409
        assert registry.diagnostics()["stages"] == 0
    finally:
        storage_service.client.delete_object(Bucket=trap, Key=item.file_path)


async def test_wrong_identity_and_non_video_are_rejected_before_capture(
    httpx_client, registry, video_world, project_admin, stored_world, monkeypatch
):
    async def forbidden(*args, **kwargs):
        pytest.fail("rejected preparation must perform no object I/O")

    monkeypatch.setattr(playback, "capture_video", forbidden)
    wrong = payload(video_world)
    wrong["sha256"] = "f" * 64
    denied = await httpx_client.post(
        prepare_path(video_world), headers=headers(project_admin), json=wrong
    )
    assert denied.status_code == 409
    # The original image DatasetItem still exists even after the Task is bound to video.
    image = await httpx_client.post(
        f"/api/v1/datasets/{video_world[1].dataset_id}/items/{stored_world[1].dataset_item_id}/original-playback",
        headers=headers(project_admin),
        json={
            "content_id": str(stored_world[1].id),
            "sha256": stored_world[1].checksum_value,
            "session_id": str(uuid.uuid4()),
        },
    )
    assert image.status_code == 415
    assert registry.diagnostics()["stages"] == 0


async def test_new_session_owns_another_stage_and_capacity_is_explicit(
    httpx_client, registry, video_world, project_admin
):
    await prepare(httpx_client, video_world, project_admin)
    await prepare(httpx_client, video_world, project_admin)
    response = await httpx_client.post(
        prepare_path(video_world),
        headers=headers(project_admin),
        json=payload(video_world),
    )
    assert response.status_code == 429 and response.headers["retry-after"] == "1"
    assert registry.diagnostics()["stages"] == 2


async def test_origin_checks_support_existing_same_origin_proxy_only(
    httpx_client, registry, video_world, project_admin
):
    response = await prepare(httpx_client, video_world, project_admin)
    path = response.json()["media_path"]
    for request_headers in [
        {"Origin": "http://other.test"},
        {"Sec-Fetch-Site": "same-site"},
        {"Sec-Fetch-Site": "cross-site"},
    ]:
        denied = await httpx_client.get(path, headers=request_headers)
        assert denied.status_code == 403
    accepted = await httpx_client.get(
        path,
        headers={
            "Origin": "http://frontend.test",
            "Sec-Fetch-Site": "same-origin",
            "Range": "bytes=0-9",
        },
    )
    assert accepted.status_code == 206 and accepted.content == video_world[-1][:10]


async def test_range_errors_release_the_pin_and_are_not_auth_bypasses(
    httpx_client, registry, video_world, project_admin
):
    response = await prepare(httpx_client, video_world, project_admin)
    path = response.json()["media_path"]
    denied = await httpx_client.get(path, headers={"Range": "bytes=9999999999-"})
    assert (
        denied.status_code == 416
        and denied.headers["content-range"] == f"bytes */{len(video_world[-1])}"
    )
    invalid = await httpx_client.get(path, headers={"Range": "bytes=broken"})
    assert invalid.status_code == 400
    assert registry.diagnostics()["response_pins"] == 0
    full = await httpx_client.get(
        path, headers={"Range": "bytes=10-20", "If-Range": 'W/"old"'}
    )
    assert full.status_code == 200 and full.content == video_world[-1]


@pytest.mark.parametrize("change", ["expiry", "revoke"])
async def test_pin_must_still_be_admitted_after_awaited_authorization(
    httpx_client, registry, video_world, project_admin, monkeypatch, change
):
    response = await prepare(httpx_client, video_world, project_admin)
    lease_id = uuid.UUID(response.json()["lease_id"])
    recheck = originals.recheck_original

    async def change_after_wait(db, context):
        await recheck(db, context)
        if change == "expiry":
            monkeypatch.setattr(
                registry,
                "_clock",
                lambda: datetime.now(timezone.utc) + timedelta(seconds=301),
            )
        else:
            await registry.revoke_lease(lease_id)

    monkeypatch.setattr(originals, "recheck_original", change_after_wait)
    denied = await httpx_client.get(response.json()["media_path"])
    assert denied.status_code == 410
    assert registry.diagnostics()["stages"] == 0
    assert registry.diagnostics()["response_pins"] == 0


async def test_ready_stage_lease_is_rechecked_after_final_authorization_wait(
    httpx_client, registry, video_world, project_admin, monkeypatch
):
    session = uuid.uuid4()
    await prepare(httpx_client, video_world, project_admin, session_id=session)
    recheck = originals.recheck_original

    async def expire_after_wait(db, context):
        await recheck(db, context)
        monkeypatch.setattr(
            registry,
            "_clock",
            lambda: datetime.now(timezone.utc) + timedelta(seconds=301),
        )

    monkeypatch.setattr(originals, "recheck_original", expire_after_wait)
    denied = await httpx_client.post(
        prepare_path(video_world),
        headers=headers(project_admin),
        json=payload(video_world, session),
    )
    assert denied.status_code == 410
    assert "set-cookie" not in denied.headers
    assert registry.diagnostics()["leases"] == 0


async def test_api_key_authority_is_naturally_limited_and_rechecked_without_bearer(
    httpx_client, registry, video_world, db_session, project_admin
):
    token = "ak_" + uuid.uuid4().hex
    key = ApiKey(
        user_id=project_admin[0].id,
        name="Video reader",
        key_prefix=token[:12],
        key_hash=hash_password(token),
        scopes=["tasks:read"],
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=90),
    )
    db_session.add(key)
    await db_session.flush()
    response = await prepare(httpx_client, video_world, (project_admin[0], token))
    max_age = int(response.headers["set-cookie"].split("Max-Age=")[1].split(";")[0])
    assert 0 < max_age <= 90
    key.scopes = ["datasets:read"]
    await db_session.commit()
    denied = await httpx_client.get(response.json()["media_path"])
    assert denied.status_code == 403
    assert registry.diagnostics()["response_pins"] == 0


async def test_https_browser_cookie_remains_secure_behind_existing_host_rewrite_proxy(
    httpx_client, registry, video_world, project_admin
):
    response = await httpx_client.post(
        prepare_path(video_world),
        headers={
            **headers(project_admin),
            "Origin": "https://frontend.test",
            "Sec-Fetch-Site": "same-origin",
        },
        json=payload(video_world),
    )
    assert response.status_code == 200
    assert "; Secure" in response.headers["set-cookie"]
    assert "HttpOnly" in response.headers["set-cookie"]
