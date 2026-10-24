"""Original credentials and authorization windows around real registered I/O."""

import asyncio
from datetime import datetime, timedelta, timezone
import io
import threading
from unittest.mock import AsyncMock
import uuid

import pytest
from sqlalchemy import delete, select
from starlette.requests import Request

from app.core.security import create_access_token, decode_access_token, hash_password
from app.db.models.api_key import ApiKey
from app.db.models.guide_asset_upload import GuideAssetUpload as Upload
from app.db.models.project_guide_asset import ProjectGuideAsset as Reference
from app.db.models.project_member import ProjectMember
from app.db.models.storage_maintenance import StorageOperation
from app.services import comment_attachment_authority as authority
from app.services import guide_asset_uploads as uploads, storage_access
from tests.factory import create_membership, create_user
from tests.test_comment_attachment_uploads import bearer
from tests.test_guide_assets import (
    content,
    endpoint,
    objects as objects,
    project as project,
    upload,
)


async def key_for(client, actor):
    response = await client.post(
        "/api/v1/me/api-keys",
        headers=bearer(actor[1]),
        json={"name": "Guide authority", "scopes": ["datasets:read"]},
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize("stage", ["acquired", "final"])
@pytest.mark.parametrize("change", ["revoke", "rotate", "scopes", "expiry", "actor"])
async def test_original_narrow_key_is_rechecked_before_io_and_after_close(
    httpx_client,
    project_admin,
    project,
    objects,
    db_session,
    monkeypatch,
    stage,
    change,
):
    value = await upload(httpx_client, project_admin, project)
    project_id = project.id
    created = await key_for(httpx_client, project_admin)
    key_id = uuid.UUID(created["id"])
    original = storage_access.run
    before = len(objects.reads)

    async def mutate():
        if change == "actor":
            project_admin[0].is_active = False
        else:
            key = await db_session.get(ApiKey, key_id, populate_existing=True)
            if change == "revoke":
                key.revoked_at = datetime.now(timezone.utc)
            elif change == "rotate":
                key.key_hash = hash_password("different original secret")
            elif change == "scopes":
                key.scopes = ["annotations:read"]
            else:
                key.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db_session.flush()

    async def run(*args, **kwargs):
        if stage == "acquired":
            callback = kwargs["on_acquired"]

            async def acquired(permit):
                await mutate()
                await callback(permit)

            kwargs["on_acquired"] = acquired
            return await original(*args, **kwargs)
        result = await original(*args, **kwargs)
        await mutate()
        return result

    monkeypatch.setattr(storage_access, "run", run)
    result = await content(
        httpx_client, (project_admin[0], created["plaintext"]), project_id, value["key"]
    )
    assert result.status_code == 403, result.text
    assert result.json()["detail"]["code"] == "guide_asset_authority_revoked"
    assert len(objects.reads) - before == (1 if stage == "final" else 0)
    assert all(body.closed for body in objects.bodies)


async def test_original_narrow_key_works_without_new_scope_requirement(
    httpx_client, project_admin, project, objects
):
    # Exercise both registered checkpoints without requiring upload-only scopes.
    key = await key_for(httpx_client, project_admin)
    actor = project_admin[0], key["plaintext"]
    value = await upload(httpx_client, actor, project)
    assert (
        await content(httpx_client, actor, project, value["key"])
    ).status_code == 200


@pytest.mark.parametrize("change", ["membership", "reference", "gate"])
async def test_final_read_requires_current_visibility_reference_and_gate(
    httpx_client,
    project_admin,
    annotator,
    project,
    objects,
    db_session,
    monkeypatch,
    change,
):
    value = await upload(httpx_client, project_admin, project)
    project_id = project.id
    member = await create_membership(
        db_session,
        project_id=project_id,
        user_id=annotator[0].id,
        role="viewer",
        assigned_by=project_admin[0].id,
    )
    await db_session.commit()
    member_id = member.id
    identifier = uuid.UUID(value["key"].split("/")[-1])
    original = storage_access.run

    async def run(*args, **kwargs):
        result = await original(*args, **kwargs)
        if change == "membership":
            await db_session.execute(
                delete(ProjectMember).where(ProjectMember.id == member_id)
            )
        elif change == "reference":
            await db_session.execute(
                delete(Reference).where(
                    Reference.project_id == project_id,
                    Reference.upload_id == identifier,
                )
            )
        else:
            check = storage_access.check

            async def changed(*args, **kwargs):
                if kwargs.get("lock"):
                    raise storage_access.error("storage_maintenance_changed")
                return await check(*args, **kwargs)

            monkeypatch.setattr(storage_access, "check", changed)
        await db_session.flush()
        return result

    monkeypatch.setattr(storage_access, "run", run)
    result = await content(httpx_client, annotator, project_id, value["key"])
    assert result.status_code == (404 if change == "membership" else 409), result.text
    assert all(body.closed for body in objects.bodies)


async def test_platform_viewer_retains_visible_image_access(
    httpx_client, project_admin, project, objects, db_session
):
    value = await upload(httpx_client, project_admin, project)
    viewer = await create_user(
        db_session, "viewer", f"guide-{uuid.uuid4().hex}@test.local", "Guide viewer"
    )
    await create_membership(
        db_session,
        project_id=project.id,
        user_id=viewer.id,
        role="viewer",
        assigned_by=project_admin[0].id,
    )
    await db_session.commit()
    token = create_access_token(str(viewer.id), "viewer")
    assert (
        await content(httpx_client, (viewer, token), project, value["key"])
    ).status_code == 200


@pytest.mark.parametrize("stage", ["gate", "commit", "executor"])
@pytest.mark.parametrize("credential", ["key", "jwt"])
async def test_natural_expiry_after_last_wait_never_returns_content(
    httpx_client,
    project_admin,
    project,
    objects,
    db_session,
    monkeypatch,
    stage,
    credential,
):
    value = await upload(httpx_client, project_admin, project)
    project_id = project.id
    if credential == "key":
        created = await key_for(httpx_client, project_admin)
        token = created["plaintext"]
        expiration = authority.now() + timedelta(seconds=30)
        key = await db_session.get(ApiKey, uuid.UUID(created["id"]))
        key.expires_at = expiration
        await db_session.commit()
    else:
        token = create_access_token(
            str(project_admin[0].id), "project_admin", timedelta(seconds=60)
        )
        expiration = datetime.fromtimestamp(
            decode_access_token(token)["exp"], timezone.utc
        )

    def expire():
        monkeypatch.setattr(authority, "now", lambda: expiration + timedelta(seconds=1))

    if stage == "gate":
        original = storage_access.check

        async def checked(*args, **kwargs):
            result = await original(*args, **kwargs)
            if kwargs.get("lock"):
                expire()
            return result

        monkeypatch.setattr(storage_access, "check", checked)
    elif stage == "commit":
        original = db_session.commit
        commits = 0

        async def commit():
            nonlocal commits
            await original()
            commits += 1
            if commits == 3:
                expire()

        monkeypatch.setattr(db_session, "commit", commit)
    else:
        original = storage_access.run

        async def run(db, location, purpose, call, *args, **kwargs):
            def queued(*args, **kwargs):
                expire()
                return call(*args, **kwargs)

            return await original(db, location, purpose, queued, *args, **kwargs)

        monkeypatch.setattr(storage_access, "run", run)
    before = len(objects.reads)
    result = await content(
        httpx_client, (project_admin[0], token), project_id, value["key"]
    )
    assert result.status_code == 403, result.text
    assert len(objects.reads) - before == (0 if stage == "executor" else 1)


async def test_logout_generation_is_checked_after_io_without_new_cache_dependency(
    httpx_client, project_admin, project, objects, db_session, monkeypatch
):
    from app.core import token_blacklist

    value = await upload(httpx_client, project_admin, project)
    project_id = project.id
    original = storage_access.run

    async def run(*args, **kwargs):
        result = await original(*args, **kwargs)
        monkeypatch.setattr(
            token_blacklist, "get_user_generation", AsyncMock(return_value=10**6)
        )
        return result

    monkeypatch.setattr(storage_access, "run", run)
    assert (
        await content(httpx_client, project_admin, project_id, value["key"])
    ).status_code == 403


async def test_final_manager_revocation_leaves_written_pending_owner_unpublished(
    httpx_client, project_admin, super_admin, project, objects, db_session, monkeypatch
):
    project_id = project.id
    original = storage_access.run

    async def run(*args, **kwargs):
        result = await original(*args, **kwargs)
        await db_session.refresh(project)
        project.owner_id = super_admin[0].id
        await db_session.flush()
        return result

    monkeypatch.setattr(storage_access, "run", run)
    result = await httpx_client.post(
        endpoint(project_id, "upload"),
        params={"file_name": "x.png"},
        content=b"x",
        headers={**bearer(project_admin[1]), "Content-Type": "image/png"},
    )
    assert result.status_code == 404, result.text
    row = await db_session.scalar(select(Upload).where(Upload.project_id == project_id))
    assert row.state == "pending" and len(objects.puts) == 1 and not objects.deleted
    assert await db_session.get(Reference, (project_id, row.id)) is None
    await db_session.refresh(project)
    assert project.guide_assets == []


async def test_cancelled_read_keeps_real_permit_until_body_really_closes(
    httpx_client, project_admin, project, objects, db_session
):
    value = await upload(httpx_client, project_admin, project)
    project_id = project.id
    identifier = uuid.UUID(value["key"].split("/")[-1])
    row = await db_session.get(Upload, identifier)
    receipt_location = uuid.UUID(row.storage_ref["location_id"])
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()

    class Body(io.BytesIO):
        def read(self, size=-1):
            entered.set()
            assert release.wait(10)
            return super().read(size)

        def close(self):
            super().close()
            closed.set()

    objects.body_factory = Body
    request = Request(
        {
            "type": "http",
            "headers": [(b"authorization", ("Bearer " + project_admin[1]).encode())],
        }
    )
    task = asyncio.create_task(
        uploads.read(db_session, project_admin[0], request, project_id, value["key"])
    )

    async def ledger():
        async with storage_access.engine(db_session).connect() as connection:
            return set(
                await connection.scalars(
                    select(StorageOperation.id).where(
                        StorageOperation.location_id == receipt_location,
                        StorageOperation.purpose == uploads.purpose("read", identifier),
                    )
                )
            )

    try:
        assert await asyncio.to_thread(entered.wait, 5)
        assert await ledger()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not closed.is_set() and await ledger()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set() and not await ledger()


@pytest.mark.parametrize("stage", ["body", "admission", "final"])
@pytest.mark.parametrize("change", ["scopes", "owner"])
async def test_upload_captures_original_authority_before_body_and_never_publishes_after_revocation(
    httpx_client,
    project_admin,
    super_admin,
    project,
    objects,
    db_session,
    monkeypatch,
    stage,
    change,
):
    project_id = project.id
    created = await key_for(httpx_client, project_admin)
    key_id = uuid.UUID(created["id"])

    async def mutate():
        if change == "scopes":
            key = await db_session.get(ApiKey, key_id, populate_existing=True)
            key.scopes = ["*"]
        else:
            await db_session.refresh(project)
            project.owner_id = super_admin[0].id
        await db_session.flush()

    if stage == "body":

        async def body():
            yield b"first image chunk"
            await mutate()
            yield b"last image chunk"

        data = body()
    else:
        data = b"image"
        if stage == "admission":
            original = storage_access.acquire

            async def acquired(*args, **kwargs):
                permit = await original(*args, **kwargs)
                await mutate()
                return permit

            monkeypatch.setattr(storage_access, "acquire", acquired)
        else:
            original = storage_access.run

            async def run(*args, **kwargs):
                result = await original(*args, **kwargs)
                await mutate()
                return result

            monkeypatch.setattr(storage_access, "run", run)
    response = await httpx_client.post(
        endpoint(project_id, "upload"),
        params={"file_name": "x.png"},
        content=data,
        headers={**bearer(created["plaintext"]), "Content-Type": "image/png"},
    )
    assert response.status_code == (403 if change == "scopes" else 404), response.text
    row = await db_session.scalar(select(Upload).where(Upload.project_id == project_id))
    assert (row is None) is (stage == "body")
    if row is not None:
        assert row.state == "pending"
        assert await db_session.get(Reference, (project_id, row.id)) is None
    assert bool(objects.puts) is (stage == "final") and not objects.deleted
