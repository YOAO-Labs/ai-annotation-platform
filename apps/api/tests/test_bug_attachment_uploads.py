"""Independent bug attachment receipts, authorization windows and retirement races."""

import asyncio
from dataclasses import FrozenInstanceError
from datetime import timedelta
import hashlib
import io
from types import SimpleNamespace
import threading
from unittest.mock import AsyncMock, Mock
import uuid

from botocore.exceptions import ClientError
from botocore.config import Config
from fastapi import HTTPException
from PIL import Image
import pytest
from sqlalchemy import delete, select
from starlette.requests import Request

from app.config import settings
from app.core.security import hash_password
from app.db.models.api_key import ApiKey
from app.db.models.bug_attachment_upload import BugAttachmentUpload
from app.db.models.bug_report import BugReport
from app.db.models.storage_maintenance import StorageMaintenanceGate, StorageOperation
from app.services import bug_attachment_storage as storage
from app.services import bug_attachment_uploads as uploads, storage_access

UPLOAD = "/api/v1/bug_reports/screenshot/upload"


def bearer(token):
    return {"Authorization": "Bearer " + token}


def image_bytes(format="PNG"):
    stream = io.BytesIO()
    Image.new("RGB", (8, 8), (12, 40, 80)).save(stream, format=format)
    return stream.getvalue()


class BugObjects:
    """Exercise real conditional reads and immutable PUTs with independent bytes."""

    def __init__(self):
        scheme = "https" if settings.minio_use_ssl else "http"
        self.client = SimpleNamespace(
            meta=SimpleNamespace(endpoint_url=f"{scheme}://{settings.minio_endpoint}"),
            head_object=self.head,
            get_object=self.get,
            put_object=self.put,
        )
        self.bug_reports_bucket = settings.minio_bug_reports_bucket
        self.objects, self.reads, self.puts, self.deleted, self.bodies = (
            {},
            [],
            [],
            [],
            [],
        )
        self.fail_put = self.fail_after_put = self.fail_delete = False
        self.body_factory = io.BytesIO

    def head(self, *, Bucket, Key):  # noqa: N803
        try:
            data = self.objects[Bucket, Key]
        except KeyError:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "HeadObject") from None
        return {
            "ContentLength": len(data),
            "ETag": '"' + hashlib.sha256(data).hexdigest() + '"',
        }

    def get(self, *, Bucket, Key, IfMatch):  # noqa: N803
        metadata = self.head(Bucket=Bucket, Key=Key)
        assert metadata["ETag"] == IfMatch
        self.reads.append((Bucket, Key))
        body = self.body_factory(self.objects[Bucket, Key])
        self.bodies.append(body)
        return {"Body": body, **metadata}

    def put(self, *, Bucket, Key, Body, ContentType, IfNoneMatch):  # noqa: N803
        assert ContentType in {"image/png", "image/jpeg", "image/webp"}
        assert IfNoneMatch == "*"
        if self.fail_put:
            raise OSError("private storage credential")
        if (Bucket, Key) in self.objects:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        self.objects[Bucket, Key] = bytes(Body)
        self.puts.append((Bucket, Key))
        if self.fail_after_put:
            raise OSError("private unknown PUT response")
        return {}

    def delete_object(self, key, *, bucket):
        if self.fail_delete:
            raise OSError("private delete failure")
        self.objects.pop((bucket, key), None)
        self.deleted.append((bucket, key))


def patch_objects(monkeypatch):
    objects = BugObjects()
    monkeypatch.setattr(storage, "storage_service", objects)
    from app.services import storage as module

    objects.client.meta.config = Config(retries={"max_attempts": 3})

    def owned(**kwargs):
        assert kwargs["client_config"] is objects.client.meta.config
        return SimpleNamespace(
            client=SimpleNamespace(
                put_object=lambda **params: objects.client.put_object(**params),
                head_object=lambda **params: objects.client.head_object(**params),
                get_object=lambda **params: objects.client.get_object(**params),
                delete_object=lambda **params: (
                    objects.client.delete_object(**params)
                    if hasattr(objects.client, "delete_object")
                    else objects.delete_object(params["Key"], bucket=params["Bucket"])
                ),
                close=lambda: None,
            )
        )

    monkeypatch.setattr(module, "StorageService", owned)
    return objects


@pytest.fixture
def objects(monkeypatch):
    return patch_objects(monkeypatch)


async def seed_receipt(
    db, objects, owner_id, *, state="ready", expired=False, bound=None
):
    data = image_bytes()
    started = uploads.now() - timedelta(days=181 if expired else 1)
    row = BugAttachmentUpload(
        id=uuid.uuid4(),
        owner_id=owner_id,
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        mime_type="image/png",
        file_name="evidence.png",
        state=state,
        created_at=started,
        expires_at=started + timedelta(days=180),
        retired_at=uploads.now() if state == "retired" else None,
        bound_report_id=None,
    )
    row.storage_ref = await storage.allocate(db, row)
    db.add(row)
    await db.flush()
    if bound is not None:
        row.bound_report_id = bound
        await db.flush()
    objects.objects[row.storage_ref["bucket"], row.storage_ref["object_key"]] = data
    return row


async def uploaded(client, actor, *, data=None, mime="image/png", name="evidence.png"):
    response = await client.post(
        UPLOAD,
        params={"file_name": name},
        content=data or image_bytes(),
        headers={**bearer(actor[1]), "Content-Type": mime},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def created(client, actor, attachments=None, **extra):
    response = await client.post(
        "/api/v1/bug_reports",
        headers=bearer(actor[1]),
        json={
            "title": "Receipt evidence",
            "description": "d",
            "attachments": attachments or [],
            **extra,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def downloaded(client, actor, report, key):
    return await client.get(
        f"/api/v1/bug_reports/{report['id']}/attachments/download",
        params={"key": key},
        headers=bearer(actor[1]),
    )


async def api_key(client, actor):
    result = await client.post(
        "/api/v1/me/api-keys",
        headers=bearer(actor[1]),
        json={"name": "Attachment authority", "scopes": ["datasets:read"]},
    )
    assert result.status_code == 201, result.text
    return result.json()


def operation_filter(location_id, purpose):
    return (
        StorageOperation.location_id == location_id,
        StorageOperation.purpose == purpose,
    )


async def owned_operations(db, location_id, purpose):
    """Inspect only this UUID purpose, outside the business SAVEPOINT."""
    async with storage_access.engine(db).connect() as connection:
        return set(
            await connection.scalars(
                select(StorageOperation.id).where(
                    *operation_filter(location_id, purpose)
                )
            )
        )


async def clear_closed_test_operations(db, location_id, purpose):
    """The caller has attested closure of its synthetic physical handles."""
    async with storage_access.engine(db).begin() as connection:
        identifiers = set(
            await connection.scalars(
                select(StorageOperation.id).where(
                    *operation_filter(location_id, purpose)
                )
            )
        )
        if identifiers:
            await connection.execute(
                delete(StorageOperation).where(
                    *operation_filter(location_id, purpose),
                    StorageOperation.id.in_(identifiers),
                )
            )


@pytest.mark.parametrize(
    "format,mime",
    [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")],
)
async def test_upload_preserves_exact_image_and_actual_receipt(
    format, mime, httpx_client, annotator, objects, db_session
):
    data = image_bytes(format)
    value = await uploaded(httpx_client, annotator, data=data, mime=mime)
    row = await db_session.scalar(select(BugAttachmentUpload))
    assert row.state == "ready" and row.bound_report_id is None
    assert value == uploads.attachment(row).model_dump(by_alias=True)
    assert (
        row.size_bytes == len(data) and row.sha256 == hashlib.sha256(data).hexdigest()
    )
    assert row.expires_at - row.created_at == timedelta(days=180)
    assert objects.objects[row.storage_ref["bucket"], value["storageKey"]] == data
    assert all(body.closed for body in objects.bodies)
    receipt = uploads.snapshot(row)
    with pytest.raises(FrozenInstanceError):
        receipt.id = uuid.uuid4()
    receipt.storage_ref["bucket"] = "caller-mutation"
    assert receipt.storage_ref == row.storage_ref


@pytest.mark.parametrize(
    "mime,data,status",
    [
        ("text/plain", b"plain", 422),
        ("image/png", b"invalid", 422),
        ("image/png", image_bytes("JPEG"), 422),
    ],
)
async def test_invalid_image_has_no_receipt_or_io(
    mime, data, status, httpx_client, annotator, objects, db_session
):
    response = await httpx_client.post(
        UPLOAD, content=data, headers={**bearer(annotator[1]), "Content-Type": mime}
    )
    assert response.status_code == status, response.text
    assert await db_session.scalar(select(BugAttachmentUpload)) is None
    assert not objects.puts and not objects.deleted


async def test_chunked_body_limit_does_not_trust_content_length(
    httpx_client, annotator, objects, db_session
):
    async def chunks():
        for _ in range(11):
            yield b"x" * (1024 * 1024)

    response = await httpx_client.post(
        UPLOAD,
        content=chunks(),
        headers={
            **bearer(annotator[1]),
            "Content-Type": "image/png",
            "Content-Length": "1",
        },
    )
    assert response.status_code == 413
    assert await db_session.scalar(select(BugAttachmentUpload)) is None
    assert not objects.puts


@pytest.mark.parametrize("after_put", [False, True])
async def test_failed_or_unknown_put_keeps_pending(
    after_put, httpx_client, annotator, objects, db_session
):
    objects.fail_put, objects.fail_after_put = not after_put, after_put
    response = await httpx_client.post(
        UPLOAD,
        content=image_bytes(),
        headers={**bearer(annotator[1]), "Content-Type": "image/png"},
    )
    assert response.status_code == 503 and "private" not in response.text
    row = await db_session.scalar(select(BugAttachmentUpload))
    assert row.state == "pending" and not objects.deleted
    assert bool(objects.objects) is after_put
    assert (await uploads.cleanup(db_session))["deleted"] == 0


async def test_pending_commit_failure_never_starts_put(
    db_session, annotator, objects, monkeypatch
):
    actor = annotator[0]
    authority = uploads.Authority(actor.id, None, None)
    monkeypatch.setattr(
        db_session,
        "commit",
        AsyncMock(side_effect=RuntimeError("unknown pending commit")),
    )
    with pytest.raises(RuntimeError, match="unknown pending commit"):
        await uploads.upload(db_session, authority, image_bytes(), "image/png", "x.png")
    assert not objects.puts and not objects.deleted


async def test_ready_commit_unknown_never_deletes_candidate(
    db_session, annotator, objects, monkeypatch
):
    commit = db_session.commit
    calls = 0

    async def unknown():
        nonlocal calls
        calls += 1
        await commit()
        if calls == 3:
            raise RuntimeError("unknown ready commit")

    monkeypatch.setattr(db_session, "commit", unknown)
    with pytest.raises(HTTPException) as caught:
        await uploads.upload(
            db_session,
            uploads.Authority(annotator[0].id, None, None),
            image_bytes(),
            "image/png",
            "x.png",
        )
    assert caught.value.status_code == 503
    assert objects.puts and not objects.deleted
    row = await db_session.scalar(select(BugAttachmentUpload))
    assert row.state == "ready"


@pytest.mark.parametrize("stage", ["validation", "io"])
@pytest.mark.parametrize("change", ["disabled", "hash", "scopes", "expiry", "revoked"])
async def test_original_authority_rechecked_in_both_windows(
    stage, change, httpx_client, annotator, objects, db_session, monkeypatch
):
    created_key = await api_key(httpx_client, annotator)
    key = await db_session.get(ApiKey, uuid.UUID(created_key["id"]))

    async def mutate():
        if change == "disabled":
            annotator[0].is_active = False
        elif change == "hash":
            key.key_hash = hash_password("rotated-credential")
        elif change == "scopes":
            key.scopes = ["*"]
        elif change == "expiry":
            key.expires_at = uploads.now() - timedelta(seconds=1)
        else:
            key.revoked_at = uploads.now()
        await db_session.flush()

    if stage == "validation":
        original = uploads.validated_image

        async def validation(*args):
            result = await original(*args)
            await mutate()
            return result

        monkeypatch.setattr(uploads, "validated_image", validation)
    else:
        original = storage_access.run

        async def run(*args, **kwargs):
            result = await original(*args, **kwargs)
            if args[2].startswith("bug_attachment_write_"):
                await mutate()
            return result

        monkeypatch.setattr(storage_access, "run", run)
    response = await httpx_client.post(
        UPLOAD,
        content=image_bytes(),
        headers={**bearer(created_key["plaintext"]), "Content-Type": "image/png"},
    )
    assert response.status_code == 403, response.text
    row = await db_session.scalar(select(BugAttachmentUpload))
    assert (row is None) is (stage == "validation")
    if row is not None:
        assert row.state == "pending"
    assert bool(objects.puts) is (stage == "io")
    assert not objects.deleted


async def test_presented_original_key_checked_at_capture(db_session, annotator):
    actor = annotator[0]
    key = ApiKey(
        id=uuid.uuid4(),
        user_id=actor.id,
        name="rotated",
        key_prefix="ak_original",
        key_hash=hash_password("ak_new"),
        scopes=["datasets:read"],
    )
    db_session.add(key)
    await db_session.flush()
    request = Request(
        {"type": "http", "headers": [(b"authorization", b"Bearer ak_original")]}
    )
    request.state.api_key_id = str(key.id)
    request.state.api_key_scopes = ["datasets:read"]
    with pytest.raises(HTTPException) as caught:
        await uploads.capture_authority(db_session, actor, request)
    assert caught.value.status_code == 403


async def test_key_natural_expiry_during_final_gate_wait(
    db_session, annotator, objects, monkeypatch
):
    actor = annotator[0]
    initial = uploads.now()
    key = ApiKey(
        id=uuid.uuid4(),
        user_id=actor.id,
        name="expiring",
        key_prefix="ak_expiring",
        key_hash=hash_password("ak_expiring"),
        scopes=[],
        expires_at=initial + timedelta(seconds=5),
    )
    db_session.add(key)
    await db_session.flush()
    authority = uploads.Authority(actor.id, key.id, (key.key_hash, (), key.expires_at))
    check = storage_access.check

    async def waited(*args, **kwargs):
        result = await check(*args, **kwargs)
        if kwargs.get("lock"):
            monkeypatch.setattr(uploads, "now", lambda: initial + timedelta(seconds=6))
        return result

    monkeypatch.setattr(storage_access, "check", waited)
    with pytest.raises(HTTPException) as caught:
        await uploads.upload(db_session, authority, image_bytes(), "image/png", "x.png")
    assert caught.value.status_code == 403
    assert (await db_session.scalar(select(BugAttachmentUpload))).state == "pending"
    assert objects.puts and not objects.deleted


@pytest.mark.parametrize("stage", ["acquire", "final"])
async def test_maintenance_fences_keep_pending(
    stage, httpx_client, annotator, objects, db_session, monkeypatch
):
    if stage == "acquire":

        async def reject(*args, **kwargs):
            raise storage_access.error("storage_maintenance")

        monkeypatch.setattr(storage_access, "acquire", reject)
    else:
        original = storage_access.check

        async def check(*args, **kwargs):
            if kwargs.get("lock"):
                raise storage_access.error("storage_maintenance_changed")
            return await original(*args, **kwargs)

        monkeypatch.setattr(storage_access, "check", check)
    response = await httpx_client.post(
        UPLOAD,
        content=image_bytes(),
        headers={**bearer(annotator[1]), "Content-Type": "image/png"},
    )
    assert response.status_code == 409, response.text
    assert (await db_session.scalar(select(BugAttachmentUpload))).state == "pending"
    assert bool(objects.puts) is (stage == "final") and not objects.deleted


async def test_saved_bucket_download_and_delete_ignore_same_key_trap(
    httpx_client, annotator, super_admin, objects, db_session
):
    value = await uploaded(httpx_client, annotator)
    report = await created(httpx_client, annotator, [value])
    row = await db_session.scalar(select(BugAttachmentUpload))
    saved_bucket, key = row.storage_ref["bucket"], value["storageKey"]
    original = objects.objects[saved_bucket, key]
    objects.bug_reports_bucket = "changed-current-bug-bucket"
    objects.objects[objects.bug_reports_bucket, key] = b"wrong bucket trap"
    response = await downloaded(httpx_client, super_admin, report, key)
    assert response.status_code == 200 and response.content == original
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["content-disposition"].startswith(
        "attachment; filename*=UTF-8''"
    )
    assert "location" not in response.headers and all(
        body.closed for body in objects.bodies
    )
    deleted = await httpx_client.delete(
        f"/api/v1/bug_reports/{report['id']}", headers=bearer(annotator[1])
    )
    assert deleted.status_code == 204, deleted.text
    assert (saved_bucket, key) in objects.deleted
    assert objects.objects[objects.bug_reports_bucket, key] == b"wrong bucket trap"
    assert await db_session.get(BugAttachmentUpload, row.id) is None


@pytest.mark.parametrize(
    "change,status", [("missing", 404), ("corrupt", 409), ("endpoint", 409)]
)
async def test_download_integrity_errors_do_not_sign_or_fallback(
    change, status, httpx_client, annotator, objects, db_session
):
    value = await uploaded(httpx_client, annotator)
    report = await created(httpx_client, annotator, [value])
    row = await db_session.scalar(select(BugAttachmentUpload))
    target = row.storage_ref["bucket"], value["storageKey"]
    objects.reads.clear()
    if change == "missing":
        del objects.objects[target]
    elif change == "corrupt":
        objects.objects[target] = b"x" * row.size_bytes
    else:
        objects.client.meta.endpoint_url = "http://wrong.invalid:9000"
    response = await downloaded(httpx_client, annotator, report, value["storageKey"])
    assert response.status_code == status, response.text
    assert "location" not in response.headers
    assert bool(objects.reads) is (change == "corrupt")
    assert all(body.closed for body in objects.bodies)


@pytest.mark.parametrize(
    "change", ["actor", "key", "visibility", "registration", "gate"]
)
async def test_download_final_authority_and_relationship_fences(
    change, httpx_client, annotator, reviewer, objects, db_session, monkeypatch
):
    key = await api_key(httpx_client, annotator)
    actor = (annotator[0], key["plaintext"])
    value = await uploaded(httpx_client, actor)
    report = await created(httpx_client, actor, [value])
    original = storage_access.run

    async def run(*args, **kwargs):
        data = await original(*args, **kwargs)
        if args[2].startswith("bug_attachment_read_"):
            if change == "actor":
                actor[0].is_active = False
            elif change == "key":
                (
                    await db_session.get(ApiKey, uuid.UUID(key["id"]))
                ).revoked_at = uploads.now()
            elif change in {"visibility", "registration"}:
                current = await db_session.get(BugReport, uuid.UUID(report["id"]))
                if change == "visibility":
                    current.reporter_id = reviewer[0].id
                else:
                    current.attachments, current.screenshot_url = [], None
            else:
                check = storage_access.check

                async def changed(*args, **kwargs):
                    if kwargs.get("lock"):
                        raise storage_access.error("storage_maintenance_changed")
                    return await check(*args, **kwargs)

                monkeypatch.setattr(storage_access, "check", changed)
            await db_session.flush()
        return data

    monkeypatch.setattr(storage_access, "run", run)
    response = await downloaded(httpx_client, actor, report, value["storageKey"])
    assert (
        response.status_code
        == {
            "actor": 403,
            "key": 403,
            "visibility": 403,
            "registration": 400,
            "gate": 409,
        }[change]
    ), response.text
    assert all(body.closed for body in objects.bodies)


async def test_expired_ready_cleanup_and_pending_never_reaped(
    db_session, annotator, objects, httpx_client
):
    expired = await seed_receipt(db_session, objects, annotator[0].id, expired=True)
    pending = await seed_receipt(
        db_session, objects, annotator[0].id, state="pending", expired=True
    )
    report = await created(httpx_client, annotator)
    current = await db_session.get(BugReport, uuid.UUID(report["id"]))
    current.attachments = [uploads.attachment(expired).model_dump(by_alias=True)]
    current.screenshot_url = uploads.object_key(expired)
    expired.bound_report_id = current.id
    await db_session.flush()
    response = await downloaded(
        httpx_client, annotator, report, uploads.object_key(expired)
    )
    assert response.status_code == 410
    result = await uploads.cleanup(db_session)
    assert result["deleted"] == 1
    assert await db_session.get(BugAttachmentUpload, expired.id) is None
    assert await db_session.get(BugAttachmentUpload, pending.id) is not None
    assert await db_session.get(BugReport, current.id) is not None


@pytest.mark.parametrize("read_location", ["source", "migrated"])
async def test_cleanup_skips_exact_read_permit_and_nonexpired_binding(
    db_session, annotator, objects, httpx_client, read_location
):
    value = await uploaded(httpx_client, annotator)
    await created(httpx_client, annotator, [value])
    bound = await db_session.scalar(select(BugAttachmentUpload))
    bound.state, bound.retired_at = "retired", uploads.now()
    old = await seed_receipt(db_session, objects, annotator[0].id, expired=True)
    location_id = uuid.UUID(old.storage_ref["location_id"])
    if read_location == "migrated":
        location_id = uuid.uuid4()
        db_session.add(
            StorageMaintenanceGate(location_id=location_id, state="active", revision=1)
        )
        await db_session.flush()
    operation = StorageOperation(
        id=uuid.uuid4(),
        location_id=location_id,
        revision=1,
        purpose=uploads.read_purpose(old.id),
        node_id="test:bug-read",
    )
    db_session.add(operation)
    await db_session.commit()
    result = await uploads.cleanup(db_session)
    assert result == {"deleted": 0, "retained": 2, "failed": 0}
    assert not objects.deleted
    await db_session.execute(
        delete(StorageOperation).where(StorageOperation.id == operation.id)
    )
    await db_session.commit()
    assert (await uploads.cleanup(db_session))["deleted"] == 1
    assert await db_session.get(BugAttachmentUpload, bound.id) is not None


async def test_report_deletion_commits_retirement_even_when_cleanup_fails(
    httpx_client, annotator, objects, db_session
):
    value = await uploaded(httpx_client, annotator)
    report = await created(httpx_client, annotator, [value])
    objects.fail_delete = True
    response = await httpx_client.delete(
        f"/api/v1/bug_reports/{report['id']}", headers=bearer(annotator[1])
    )
    assert response.status_code == 204
    assert await db_session.get(BugReport, uuid.UUID(report["id"])) is None
    row = await db_session.scalar(select(BugAttachmentUpload))
    assert row.state == "retired" and row.bound_report_id == uuid.UUID(report["id"])
    assert row.last_attempted_at is not None and not objects.deleted
    objects.fail_delete = False
    assert (await uploads.cleanup(db_session))["deleted"] == 1


@pytest.mark.parametrize("replaced", [False, True])
async def test_null_slot_retirement_keeps_owner_when_content_changed(
    db_session, annotator, objects, monkeypatch, replaced
):
    row = await seed_receipt(db_session, objects, annotator[0].id, state="pending")
    row.object_version_id = "null"
    await db_session.flush()
    row.state, row.retired_at = "retired", uploads.now()
    await db_session.flush()
    identifier, saved_ref = row.id, dict(row.storage_ref)
    key = saved_ref["bucket"], saved_ref["object_key"]
    saved_bytes = objects.objects[key]
    if replaced:
        objects.objects[key] = b"x" * len(saved_bytes)

    def head(**params):
        assert params.pop("VersionId") == "null"
        return objects.head(**params)

    def get(**params):
        assert params.pop("VersionId") == "null"
        return objects.get(**params)

    def delete_object(**params):
        assert params == {"Bucket": key[0], "Key": key[1], "VersionId": "null"}
        objects.delete_object(params["Key"], bucket=params["Bucket"])

    deleting = Mock(side_effect=delete_object)
    monkeypatch.setattr(objects.client, "head_object", head)
    monkeypatch.setattr(objects.client, "get_object", get)
    monkeypatch.setattr(objects.client, "delete_object", deleting, raising=False)
    result = await uploads.cleanup(db_session, identifiers=[identifier])
    retained = await db_session.get(BugAttachmentUpload, identifier)
    if replaced:
        assert result == {"deleted": 0, "retained": 0, "failed": 1}
        assert retained.state == "retired"
        assert retained.storage_ref == saved_ref
        assert retained.object_version_id == "null"
        assert retained.last_attempted_at is not None
        assert objects.objects[key] == b"x" * len(saved_bytes)
        deleting.assert_not_called()
    else:
        assert result == {"deleted": 1, "retained": 0, "failed": 0}
        assert retained is None and key not in objects.objects
        deleting.assert_called_once()
    assert all(body.closed for body in objects.bodies)


async def test_delete_transaction_failure_has_zero_physical_io(
    db_session, annotator, objects, monkeypatch
):
    from app.api.v1.bug_reports import delete_bug_report
    from app.services.bug_report import BugReportService
    from tests.test_bug_reports import _seed_bug

    report = await _seed_bug(db_session, annotator[0].id, "new")
    row = await seed_receipt(db_session, objects, annotator[0].id, bound=report.id)
    await db_session.commit()
    request = Request(
        {
            "type": "http",
            "scheme": "http",
            "method": "DELETE",
            "path": f"/api/v1/bug_reports/{report.id}",
            "query_string": b"",
            "headers": [],
            "server": ("test", 80),
            "client": ("127.0.0.1", 12345),
        }
    )
    monkeypatch.setattr(
        BugReportService,
        "delete",
        AsyncMock(side_effect=RuntimeError("delete transaction failed")),
    )
    with pytest.raises(RuntimeError):
        await delete_bug_report(report.id, request, db_session, annotator[0])
    assert not objects.deleted
    await db_session.rollback()
    await db_session.refresh(row)
    assert row.state == "ready"


async def test_unknown_close_keeps_read_permit_and_blocks_cleanup(
    httpx_client, annotator, objects, db_session
):
    value = await uploaded(httpx_client, annotator)
    report = await created(httpx_client, annotator, [value])
    row = await db_session.scalar(select(BugAttachmentUpload))
    location_id, purpose = (
        uuid.UUID(row.storage_ref["location_id"]),
        uploads.read_purpose(row.id),
    )

    class UnknownClose(io.BytesIO):
        def close(self):
            super().close()
            raise OSError("unconfirmed physical close")

    objects.body_factory = UnknownClose
    try:
        response = await downloaded(
            httpx_client, annotator, report, value["storageKey"]
        )
        assert response.status_code == 503, response.text
        assert len(await owned_operations(db_session, location_id, purpose)) == 1
        # Delete retires business state, but uncertain read closure retains bytes.
        response = await httpx_client.delete(
            f"/api/v1/bug_reports/{report['id']}", headers=bearer(annotator[1])
        )
        assert response.status_code == 204 and not objects.deleted
        assert all(body.closed for body in objects.bodies)
        await clear_closed_test_operations(db_session, location_id, purpose)
        assert (await uploads.cleanup(db_session))["deleted"] == 1
    finally:
        # This fake closes its BytesIO before raising. Its test alone can attest
        # closure; a business SAVEPOINT cannot retire the independent ledger.
        assert all(body.closed for body in objects.bodies)
        await clear_closed_test_operations(db_session, location_id, purpose)


async def test_repeated_cancellation_waits_for_actual_image_validation(monkeypatch):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def validate(*args):
        entered.set()
        release.wait(10)
        finished.set()

    monkeypatch.setattr(storage, "validate_image", validate)
    task = asyncio.create_task(uploads.validated_image(b"candidate", "image/png"))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not finished.is_set()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert finished.is_set()


async def test_repeated_cancellation_drains_put_and_preserves_pending(
    db_session, annotator, objects, monkeypatch
):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = objects.put

    def put(**kwargs):
        entered.set()
        release.wait(10)
        try:
            return original(**kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(objects.client, "put_object", put)
    task = asyncio.create_task(
        uploads.upload(
            db_session,
            uploads.Authority(annotator[0].id, None, None),
            image_bytes(),
            "image/png",
            "x.png",
        )
    )
    receipt = None
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        receipt = uploads.snapshot(await db_session.scalar(select(BugAttachmentUpload)))
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not finished.is_set()
    finally:
        release.set()
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            if receipt is not None:
                location_id = uuid.UUID(receipt.storage_ref["location_id"])
                purpose = "bug_attachment_write_" + receipt.id.hex
                assert finished.is_set() and all(body.closed for body in objects.bodies)
                try:
                    assert not await owned_operations(db_session, location_id, purpose)
                finally:
                    await clear_closed_test_operations(db_session, location_id, purpose)
    assert finished.is_set()
    assert (await db_session.scalar(select(BugAttachmentUpload))).state == "pending"
    assert not objects.deleted


@pytest.mark.parametrize("scopes", [[], ["*"]])
async def test_capture_rejects_key_scopes_without_original_key_id(
    scopes, db_session, annotator
):
    request = Request({"type": "http", "headers": []})
    request.state.api_key_scopes = scopes
    with pytest.raises(HTTPException) as caught:
        await uploads.capture_authority(db_session, annotator[0], request)
    assert caught.value.status_code == 403


async def test_download_revoked_between_snapshot_and_admission_has_no_io(
    httpx_client, annotator, objects, db_session, monkeypatch
):
    value = await uploaded(httpx_client, annotator)
    report = await created(httpx_client, annotator, [value])
    row = await db_session.scalar(select(BugAttachmentUpload))
    location_id, purpose = (
        uuid.UUID(row.storage_ref["location_id"]),
        uploads.read_purpose(row.id),
    )
    objects.reads.clear()
    original = storage_access.acquire

    async def acquire(*args, **kwargs):
        permit = await original(*args, **kwargs)
        if args[2].startswith("bug_attachment_read_"):
            annotator[0].is_active = False
            await db_session.flush()
        return permit

    monkeypatch.setattr(storage_access, "acquire", acquire)
    try:
        response = await downloaded(
            httpx_client, annotator, report, value["storageKey"]
        )
        assert response.status_code == 403, response.text
        assert not objects.reads
        assert not await owned_operations(db_session, location_id, purpose)
    finally:
        # No handle is expected; attest closure even if that assertion failed.
        assert all(body.closed for body in objects.bodies)
        await clear_closed_test_operations(db_session, location_id, purpose)


async def test_actual_read_blocks_delete_until_repeated_cancellation_settles(
    httpx_client, annotator, objects, db_session
):
    value = await uploaded(httpx_client, annotator)
    report = await created(httpx_client, annotator, [value])
    row = await db_session.scalar(select(BugAttachmentUpload))
    location_id, purpose = (
        uuid.UUID(row.storage_ref["location_id"]),
        uploads.read_purpose(row.id),
    )
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    class BlockingBody(io.BytesIO):
        def read(self, count=-1):
            entered.set()
            release.wait(10)
            return super().read(count)

        def close(self):
            super().close()
            finished.set()

    objects.body_factory = BlockingBody
    request = Request({"type": "http", "headers": []})
    task = asyncio.create_task(
        uploads.read(
            db_session,
            annotator[0],
            request,
            uuid.UUID(report["id"]),
            value["storageKey"],
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        response = await httpx_client.delete(
            f"/api/v1/bug_reports/{report['id']}", headers=bearer(annotator[1])
        )
        assert response.status_code == 204 and not objects.deleted
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not finished.is_set()
        await db_session.refresh(row)
        assert row.state == "retired"
        assert len(await owned_operations(db_session, location_id, purpose)) == 1
    finally:
        release.set()
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            assert finished.is_set() and all(body.closed for body in objects.bodies)
            try:
                assert not await owned_operations(db_session, location_id, purpose)
            finally:
                await clear_closed_test_operations(db_session, location_id, purpose)
    assert finished.is_set()
    assert (await uploads.cleanup(db_session))["deleted"] == 1


def test_cleanup_task_uses_cleanup_queue_and_daily_schedule():
    from app.workers.bug_attachment_cleanup import purge_bug_attachments
    from app.workers.celery_app import celery_app

    name = "app.workers.bug_attachment_cleanup.purge_bug_attachments"
    assert purge_bug_attachments.name == name
    assert celery_app.conf.task_routes[name] == {"queue": "cleanup"}
    assert celery_app.conf.beat_schedule["purge-bug-attachments"]["task"] == name
    assert "app.workers.bug_attachment_cleanup" in celery_app.conf.include


def test_storage_removes_only_bug_bucket_lifecycle():
    from app.services.storage import StorageService

    service = StorageService.__new__(StorageService)
    service.bucket = "anno-lifecycle-test"
    service.bug_reports_bucket = "bug-lifecycle-test"
    service.media_cache_bucket = "media-lifecycle-test"
    service.import_bucket = "import-lifecycle-test"
    service.export_bucket = "export-lifecycle-test"
    service.client = SimpleNamespace(
        delete_bucket_lifecycle=Mock(),
        get_bucket_lifecycle_configuration=Mock(
            side_effect=ClientError(
                {"Error": {"Code": "NoSuchLifecycleConfiguration"}},
                "GetBucketLifecycleConfiguration",
            )
        ),
        put_bucket_lifecycle_configuration=Mock(),
    )
    service._ensure_lifecycle()
    service.client.delete_bucket_lifecycle.assert_called_once_with(
        Bucket=service.bug_reports_bucket
    )
    targets = {
        call.kwargs["Bucket"]
        for call in service.client.put_bucket_lifecycle_configuration.call_args_list
    }
    service.client.get_bucket_lifecycle_configuration.assert_any_call(
        Bucket=service.bucket
    )
    service.client.get_bucket_lifecycle_configuration.assert_any_call(
        Bucket=service.import_bucket
    )
    assert targets == {service.media_cache_bucket, service.export_bucket}
