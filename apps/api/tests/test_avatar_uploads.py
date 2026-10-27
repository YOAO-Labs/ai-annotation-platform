"""Avatar publication fences and cleanup retain physical ownership on failure."""

import asyncio
from datetime import timedelta
import io
import threading
from types import SimpleNamespace
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy import delete, select, text

from app.core.security import hash_password
from app.db.models.api_key import ApiKey
from app.db.models.avatar_upload import AvatarUpload
from app.db.models.storage_maintenance import StorageOperation
from app.services import avatar, avatar_uploads, storage_access
from tests._avatar_storage import patch_storage, seed_upload
from tests.test_me_avatar import AVATAR_URL, _bearer, _png_bytes


@pytest.fixture
def objects(monkeypatch):
    return patch_storage(monkeypatch)


async def upload(httpx_client, actor):
    result = await httpx_client.post(
        AVATAR_URL,
        files={"file": ("avatar.png", _png_bytes((256, 256)), "image/png")},
        headers=_bearer(actor[1]),
    )
    assert result.status_code == 200, result.text
    return result.json()["avatar_ref"].split(":", 1)[1]


async def test_saved_bucket_survives_current_bucket_change_for_read_and_delete(
    httpx_client, annotator, objects, db_session
):
    token = await upload(httpx_client, annotator)
    row = await db_session.get(AvatarUpload, token)
    saved_bucket, key = row.storage_ref["bucket"], row.storage_ref["object_key"]
    expected = objects.objects[(saved_bucket, key)]
    objects.avatars_bucket = "changed-current-avatar-bucket"
    objects.seed(key, b"same key, wrong bytes", bucket=objects.avatars_bucket)
    response = await httpx_client.get(f"/api/v1/avatars/{token}")
    assert response.status_code == 200 and response.content == expected
    cleared = await httpx_client.delete(AVATAR_URL, headers=_bearer(annotator[1]))
    assert cleared.status_code == 200, cleared.text
    assert (saved_bucket, key) in objects.deleted
    assert objects.objects[(objects.avatars_bucket, key)] == b"same key, wrong bytes"
    assert await db_session.get(AvatarUpload, token) is None


async def test_unknown_legacy_token_is_not_read_or_deleted(
    httpx_client, annotator, objects, db_session
):
    user, credential = annotator
    token = "a" * 32
    objects.seed(avatar.upload_object_key(token), b"unknown historical bytes")
    user.avatar_ref = avatar.build_upload_ref(token)
    await db_session.flush()
    response = await httpx_client.get(f"/api/v1/avatars/{token}")
    assert response.status_code == 404
    cleared = await httpx_client.delete(AVATAR_URL, headers=_bearer(credential))
    assert cleared.status_code == 200, cleared.text
    assert not objects.reads and not objects.deleted
    assert objects.objects[(objects.avatars_bucket, f"{token}.webp")]
    assert await db_session.get(AvatarUpload, token) is None


@pytest.mark.parametrize("change", ["sha", "size", "endpoint"])
async def test_read_checks_saved_receipt_even_when_if_none_match_matches(
    change, httpx_client, annotator, objects, db_session
):
    token = await upload(httpx_client, annotator)
    row = await db_session.get(AvatarUpload, token)
    key, bucket = row.storage_ref["object_key"], row.storage_ref["bucket"]
    objects.reads.clear()
    if change == "sha":
        objects.objects[(bucket, key)] = b"x" * row.size_bytes
    elif change == "size":
        objects.objects[(bucket, key)] += b"x"
    else:
        objects.client.meta.endpoint_url = "http://unexpected-storage.invalid:9000"
    response = await httpx_client.get(
        f"/api/v1/avatars/{token}", headers={"If-None-Match": f'"{row.sha256}"'}
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] in {
        "avatar_storage_invalid_receipt",
        "avatar_storage_identity_changed",
    }
    assert bool(objects.reads) is (change != "endpoint")
    assert all(body.closed for body in objects.bodies)


@pytest.mark.parametrize("state", ["pending", "retired"])
async def test_nonready_tokens_have_no_public_read(
    state, httpx_client, db_session, objects
):
    token = uuid.uuid4().hex
    await seed_upload(db_session, objects, token, b"receipt", state=state)
    response = await httpx_client.get(f"/api/v1/avatars/{token}")
    assert response.status_code == 404 and not objects.reads


@pytest.mark.parametrize("after_put", [False, True])
async def test_failed_or_unknown_put_keeps_pending_owner(
    after_put, httpx_client, annotator, objects, db_session
):
    objects.fail_puts, objects.fail_after_put = not after_put, after_put
    response = await httpx_client.post(
        AVATAR_URL,
        files={"file": ("avatar.png", _png_bytes((256, 256)), "image/png")},
        headers=_bearer(annotator[1]),
    )
    assert response.status_code == 503
    assert "secret" not in response.text and "signature" not in response.text
    rows = list(await db_session.scalars(select(AvatarUpload)))
    assert len(rows) == 1 and rows[0].state == "pending"
    await db_session.refresh(annotator[0])
    assert annotator[0].avatar_ref is None and not objects.deleted
    assert (await avatar_uploads.cleanup(db_session))["deleted"] == 0
    assert bool(objects.objects) is after_put


@pytest.mark.parametrize("change", ["reference", "disabled"])
async def test_account_or_reference_change_during_io_refuses_publication(
    change, httpx_client, annotator, objects, db_session, monkeypatch
):
    original = storage_access.run

    async def changed(*args, **kwargs):
        result = await original(*args, **kwargs)
        if args[2] == "avatar_upload":
            user = annotator[0]
            if change == "reference":
                user.avatar_ref = "preset:pixel-07"
            else:
                user.is_active = False
            await db_session.flush()
        return result

    monkeypatch.setattr(storage_access, "run", changed)
    response = await httpx_client.post(
        AVATAR_URL,
        files={"file": ("avatar.png", _png_bytes((256, 256)), "image/png")},
        headers=_bearer(annotator[1]),
    )
    assert response.status_code == (409 if change == "reference" else 401), (
        response.text
    )
    row = await db_session.scalar(select(AvatarUpload))
    assert row.state == "pending" and not objects.deleted
    await db_session.refresh(annotator[0])
    assert annotator[0].avatar_ref == (
        "preset:pixel-07" if change == "reference" else None
    )


async def test_gate_draining_prevents_io_and_leaves_pending(
    httpx_client, annotator, objects, db_session, monkeypatch
):
    async def draining(*args, **kwargs):
        raise storage_access.error("storage_maintenance")

    monkeypatch.setattr(storage_access, "acquire", draining)
    response = await httpx_client.post(
        AVATAR_URL,
        files={"file": ("avatar.png", _png_bytes((256, 256)), "image/png")},
        headers=_bearer(annotator[1]),
    )
    assert response.status_code == 409, response.text
    assert not objects.puts and not objects.deleted
    assert (await db_session.scalar(select(AvatarUpload))).state == "pending"


async def test_gate_revision_change_after_io_refuses_publication(
    httpx_client, annotator, objects, db_session, monkeypatch
):
    original = storage_access.check

    async def changed(*args, **kwargs):
        if kwargs.get("lock"):
            assert kwargs.get("expected_revision") is not None
            raise storage_access.error("storage_maintenance_changed")
        return await original(*args, **kwargs)

    monkeypatch.setattr(storage_access, "check", changed)
    response = await httpx_client.post(
        AVATAR_URL,
        files={"file": ("avatar.png", _png_bytes((256, 256)), "image/png")},
        headers=_bearer(annotator[1]),
    )
    assert response.status_code == 409, response.text
    assert objects.puts and not objects.deleted
    assert (await db_session.scalar(select(AvatarUpload))).state == "pending"


async def test_owner_rechecks_gate_after_api_admission(
    httpx_client, annotator, objects, db_session, app_module
):
    """The owner must reacquire the admission gate, not trust the API check.

    The wrapper advances the gate revision after the API's point-in-time check
    but before the native publication transaction runs. The API released its
    lock; only the owner's own FOR SHARE/revision comparison can reject this.
    """
    from app.api.v1.account_commands import get_account_commands
    from app.services.dataset_assets import LEGACY_LOCATION_ID
    from app.services.identity_account_commands import IdentityAccountCommands
    from app.services.identity_gateway import get_identity_gateway

    owner = IdentityAccountCommands(db_session, get_identity_gateway())

    class ChangedGateOwner:
        async def execute(self, command):
            if (
                command.operation == "set_avatar_ref"
                and command.payload.kind == "upload"
            ):
                await db_session.execute(
                    text(
                        "UPDATE public.storage_maintenance_gates "
                        "SET revision = revision + 1 WHERE location_id=:location"
                    ),
                    {"location": LEGACY_LOCATION_ID},
                )
            return await owner.execute(command)

    app_module.dependency_overrides[get_account_commands] = lambda: ChangedGateOwner()
    try:
        response = await httpx_client.post(
            AVATAR_URL,
            files={"file": ("avatar.png", _png_bytes((256, 256)), "image/png")},
            headers=_bearer(annotator[1]),
        )
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "storage_maintenance_changed"
        row = await db_session.scalar(select(AvatarUpload))
        assert row.state == "pending"
        assert objects.puts and not objects.deleted
        await db_session.refresh(annotator[0])
        assert annotator[0].avatar_ref is None
    finally:
        app_module.dependency_overrides.pop(get_account_commands, None)


async def key_for(httpx_client, actor):
    response = await httpx_client.post(
        "/api/v1/me/api-keys",
        json={"name": "Avatar scoped key", "scopes": ["datasets:read"]},
        headers=_bearer(actor[1]),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_existing_scoped_key_can_upload_and_clear_without_new_scope(
    httpx_client, annotator, objects
):
    key = await key_for(httpx_client, annotator)
    headers = _bearer(key["plaintext"])
    response = await httpx_client.post(
        AVATAR_URL,
        files={"file": ("avatar.png", _png_bytes((256, 256)), "image/png")},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert (await httpx_client.delete(AVATAR_URL, headers=headers)).status_code == 200


@pytest.mark.parametrize("change", ["hash", "scopes", "expires", "revoked"])
async def test_key_proof_change_after_io_refuses_publication(
    change, httpx_client, annotator, objects, db_session, monkeypatch
):
    created = await key_for(httpx_client, annotator)
    key = await db_session.get(ApiKey, uuid.UUID(created["id"]))
    original = storage_access.run

    async def changed(*args, **kwargs):
        result = await original(*args, **kwargs)
        if args[2] == "avatar_upload":
            if change == "hash":
                key.key_hash = hash_password("different-credential")
            elif change == "scopes":
                key.scopes = ["*"]
            elif change == "expires":
                key.expires_at = avatar_uploads.now() - timedelta(seconds=1)
            else:
                key.revoked_at = avatar_uploads.now()
            await db_session.flush()
        return result

    monkeypatch.setattr(storage_access, "run", changed)
    response = await httpx_client.post(
        AVATAR_URL,
        files={"file": ("avatar.png", _png_bytes((256, 256)), "image/png")},
        headers=_bearer(created["plaintext"]),
    )
    assert response.status_code == 401, response.text
    assert (await db_session.scalar(select(AvatarUpload))).state == "pending"
    assert not objects.deleted


@pytest.mark.parametrize("change", ["hash", "scopes"])
async def test_key_change_during_normalization_stops_before_pending_or_io(
    change, httpx_client, annotator, objects, db_session, monkeypatch
):
    from app.api.v1 import me

    created = await key_for(httpx_client, annotator)
    key = await db_session.get(ApiKey, uuid.UUID(created["id"]))
    original = asyncio.to_thread

    async def changed(call, *args, **kwargs):
        result = await original(call, *args, **kwargs)
        if call is me.normalize_avatar:
            if change == "hash":
                key.key_hash = hash_password("rotated-during-normalization")
            else:
                key.scopes = ["*"]
            await db_session.flush()
        return result

    monkeypatch.setattr(me.asyncio, "to_thread", changed)
    response = await httpx_client.post(
        AVATAR_URL,
        files={"file": ("avatar.png", _png_bytes((256, 256)), "image/png")},
        headers=_bearer(created["plaintext"]),
    )
    assert response.status_code == 401, response.text
    assert not objects.puts and not objects.deleted
    assert await db_session.scalar(select(AvatarUpload)) is None


async def test_capture_reads_through_the_owner_without_private_user_access():
    calls = []

    async def execute(operation, payload, *, operation_id=None):
        calls.append(operation)
        raise HTTPException(401, "无效的认证凭据")

    principal = SimpleNamespace(
        actor_id=uuid.uuid4(),
        credential_type="jwt",
        credential_id=uuid.uuid4(),
        scopes=None,
    )
    with pytest.raises(HTTPException) as caught:
        await avatar_uploads.capture_authority(None, execute, principal)
    assert caught.value.status_code == 401
    assert calls == ["read_self_account"]


async def test_failed_retired_delete_is_durable_and_retry_uses_saved_bucket(
    httpx_client, annotator, objects, db_session
):
    token = await upload(httpx_client, annotator)
    row = await db_session.get(AvatarUpload, token)
    bucket, key = row.storage_ref["bucket"], row.storage_ref["object_key"]
    objects.fail_deletes = True
    cleared = await httpx_client.delete(AVATAR_URL, headers=_bearer(annotator[1]))
    assert cleared.status_code == 200 and cleared.json()["avatar_ref"] is None
    await db_session.refresh(row)
    assert row.state == "retired" and row.last_attempted_at is not None
    objects.avatars_bucket = "another-avatar-bucket"
    objects.seed(key, b"unrelated", bucket=objects.avatars_bucket)
    objects.fail_deletes = False
    assert await avatar_uploads.cleanup(db_session) == {
        "deleted": 1,
        "failed": 0,
        "retained": 0,
    }
    assert (bucket, key) in objects.deleted
    assert objects.objects[(objects.avatars_bucket, key)] == b"unrelated"


async def test_cleanup_protects_references_and_pending_regardless_of_age(
    db_session, annotator, objects
):
    user = annotator[0]
    retired = await seed_upload(
        db_session, objects, uuid.uuid4().hex, b"retired", state="retired"
    )
    pending = await seed_upload(
        db_session, objects, uuid.uuid4().hex, b"pending", state="pending"
    )
    user.avatar_ref = avatar.build_upload_ref(retired.token)
    await db_session.flush()
    result = await avatar_uploads.cleanup(db_session)
    assert result == {"deleted": 0, "failed": 0, "retained": 1}
    assert (
        not objects.deleted
        and (await db_session.get(AvatarUpload, pending.token)).state == "pending"
    )


async def test_one_cleanup_failure_does_not_stop_other_retired_objects(
    db_session, objects, monkeypatch
):
    first = await seed_upload(
        db_session, objects, uuid.uuid4().hex, b"first", state="retired"
    )
    second = await seed_upload(
        db_session, objects, uuid.uuid4().hex, b"second", state="retired"
    )
    first_token, second_token = first.token, second.token
    original = avatar.delete_avatar_object

    def delete_one(record, **kwargs):
        if record.token == first_token:
            raise RuntimeError("private storage failure")
        return original(record, **kwargs)

    monkeypatch.setattr(avatar, "delete_avatar_object", delete_one)
    assert await avatar_uploads.cleanup(db_session) == {
        "deleted": 1,
        "failed": 1,
        "retained": 0,
    }
    assert await db_session.get(AvatarUpload, first_token) is not None
    assert await db_session.get(AvatarUpload, second_token) is None


async def test_cancellation_waits_for_real_put_and_retains_pending(
    httpx_client, annotator, objects, db_session, monkeypatch
):
    entered, release = threading.Event(), threading.Event()
    original = avatar.write_avatar_object

    def blocked(record, data, **kwargs):
        entered.set()
        assert release.wait(timeout=10)
        return original(record, data, **kwargs)

    monkeypatch.setattr(avatar, "write_avatar_object", blocked)
    task = asyncio.create_task(
        httpx_client.post(
            AVATAR_URL,
            files={"file": ("avatar.png", _png_bytes((256, 256)), "image/png")},
            headers=_bearer(annotator[1]),
        )
    )
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait), timeout=3)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert (await db_session.scalar(select(AvatarUpload))).state == "pending"
        assert objects.deleted == []
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await db_session.scalar(select(AvatarUpload))).state == "pending"
    assert all(body.closed for body in objects.bodies)


async def test_unknown_body_close_keeps_operation_and_pending_receipt(
    httpx_client, annotator, objects, db_session
):
    class Unsettled(io.BytesIO):
        def close(self):
            super().close()
            raise RuntimeError("close result unknown with private details")

    objects.body_factory = Unsettled
    before = set(await db_session.scalars(select(StorageOperation.id)))
    owned_ids = set()
    try:
        response = await httpx_client.post(
            AVATAR_URL,
            files={"file": ("avatar.png", _png_bytes((256, 256)), "image/png")},
            headers=_bearer(annotator[1]),
        )
        assert response.status_code == 503 and "private" not in response.text
        assert (await db_session.scalar(select(AvatarUpload))).state == "pending"
        assert not objects.deleted
        after = set(await db_session.scalars(select(StorageOperation.id)))
        owned_ids = after - before
        assert len(owned_ids) == 1
    finally:
        # This deliberately unresolved operation belongs only to this fixture.
        # Tests do not claim closure; remove their synthetic ledger explicitly.
        if owned_ids:
            async with storage_access.engine(db_session).begin() as connection:
                await connection.execute(
                    delete(StorageOperation).where(StorageOperation.id.in_(owned_ids))
                )


@pytest.mark.parametrize("commit_applied", [False, True])
async def test_owner_failure_after_io_retains_bytes_and_committed_state(
    httpx_client, annotator, objects, db_session, app_module, commit_applied
):
    from app.api.v1.account_commands import get_account_commands
    from app.services.identity_account_commands import IdentityAccountCommands
    from app.services.identity_contracts import IdentityGatewayUnavailable
    from app.services.identity_gateway import get_identity_gateway

    user, token = annotator
    owner = IdentityAccountCommands(db_session, get_identity_gateway())

    class FailingOwner:
        async def execute(self, command):
            if command.operation == "set_avatar_ref":
                if commit_applied:
                    await owner.execute(command)
                    await db_session.commit()
                raise IdentityGatewayUnavailable()
            return await owner.execute(command)

    app_module.dependency_overrides[get_account_commands] = lambda: FailingOwner()
    try:
        response = await httpx_client.post(
            AVATAR_URL,
            files={"file": ("avatar.png", _png_bytes((256, 256)), "image/png")},
            headers=_bearer(token),
        )
        assert response.status_code == 503
        row = await db_session.scalar(select(AvatarUpload))
        # The owner publishes through native SQL, so the request's ORM instance
        # still carries the pre-publication state. Assert the persisted row.
        await db_session.refresh(row)
        assert row.state == ("ready" if commit_applied else "pending")
        assert objects.puts and not objects.deleted
        await db_session.refresh(user)
        if commit_applied:
            assert user.avatar_ref == avatar.build_upload_ref(row.token)
        else:
            assert user.avatar_ref is None
    finally:
        app_module.dependency_overrides.pop(get_account_commands, None)
