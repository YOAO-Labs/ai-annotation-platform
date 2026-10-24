"""Original-comment receipts, authorization windows and durable retirement."""

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import io
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import uuid

from botocore.exceptions import ClientError
import pytest
from sqlalchemy import delete, select
from starlette.requests import Request

from app.config import settings
from app.core.security import create_access_token, decode_access_token, hash_password
from app.db.models.annotation_comment import AnnotationComment
from app.db.models.annotation_feedback import AnnotationFeedback
from app.db.models.api_key import ApiKey
from app.db.models.comment_attachment_upload import CommentAttachmentUpload
from app.db.models.project_member import ProjectMember
from app.db.models.storage_maintenance import StorageOperation
from app.db.models.task import Task
from app.services import comment_attachment_authority as authority
from app.services import comment_attachment_storage as storage
from app.services import comment_attachment_uploads as uploads, storage_access
from tests.test_comment_polish import _seed_project_with_annotation


def bearer(token):
    return {"Authorization": "Bearer " + token}


def path(annotation_id, suffix):
    return f"/api/v1/annotations/{annotation_id}/comment-attachments/{suffix}"


class Objects:
    def __init__(self):
        self.bucket = settings.minio_bucket
        self.client = SimpleNamespace(
            meta=SimpleNamespace(
                endpoint_url=f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}"
            ),
            head_object=self.head,
            get_object=self.get,
            put_object=self.put,
            delete_object=self.delete,
        )
        self.objects, self.reads, self.puts, self.deleted, self.bodies = (
            {},
            [],
            [],
            [],
            [],
        )
        self.body_factory = io.BytesIO
        self.fail_put = self.fail_delete = False

    def head(self, *, Bucket, Key):  # noqa: N803
        try:
            data = self.objects[Bucket, Key]
        except KeyError:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "HeadObject") from None
        return {
            "ContentLength": len(data),
            "ETag": '"' + hashlib.sha256(data).hexdigest() + '"',
        }

    def get(self, *, Bucket, Key, IfMatch, **kwargs):  # noqa: N803
        metadata = self.head(Bucket=Bucket, Key=Key)
        assert IfMatch == metadata["ETag"]
        self.reads.append((Bucket, Key))
        body = self.body_factory(self.objects[Bucket, Key])
        self.bodies.append(body)
        return {"Body": body, **metadata}

    def put(self, *, Bucket, Key, Body, IfNoneMatch, **kwargs):  # noqa: N803
        assert IfNoneMatch == "*"
        if self.fail_put:
            raise OSError("private PUT failure")
        if (Bucket, Key) in self.objects:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        self.objects[Bucket, Key] = bytes(Body)
        self.puts.append((Bucket, Key))
        return {}

    def delete(self, *, Bucket, Key):  # noqa: N803
        if self.fail_delete:
            raise OSError("private delete failure")
        self.objects.pop((Bucket, Key), None)
        self.deleted.append((Bucket, Key))
        return {}


@pytest.fixture
def objects(monkeypatch):
    value = Objects()
    monkeypatch.setattr(storage, "storage_service", value)
    monkeypatch.setattr(uploads, "storage_service", value)
    return value


@pytest.fixture
async def discussion(db_session, super_admin):
    return await _seed_project_with_annotation(db_session, super_admin[0].id)


async def upload(
    client,
    actor,
    annotation,
    *,
    data=b"general\x00file",
    name="说明.pdf",
    mime="Application/PDF; charset=UTF-8",
):
    response = await client.post(
        path(annotation.id, "upload"),
        params={"file_name": name},
        content=data,
        headers={**bearer(actor[1]), "Content-Type": mime},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def comment(client, actor, annotation, attachments):
    response = await client.post(
        f"/api/v1/annotations/{annotation.id}/comments",
        json={"body": "", "attachments": attachments},
        headers=bearer(actor[1]),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def download(client, actor, annotation, key, **params):
    return await client.get(
        path(annotation.id, "download"),
        params={"key": key, **params},
        headers=bearer(actor[1]),
    )


async def key_for(client, actor):
    response = await client.post(
        "/api/v1/me/api-keys",
        headers=bearer(actor[1]),
        json={"name": "Comment authority", "scopes": ["datasets:read"]},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def operation_ids(db, row):
    async with storage_access.engine(db).connect() as connection:
        return set(
            await connection.scalars(
                select(StorageOperation.id).where(
                    StorageOperation.location_id
                    == uuid.UUID(row.storage_ref["location_id"]),
                    StorageOperation.purpose == uploads.read_purpose(row.id),
                )
            )
        )


async def clear_closed_operations(db, row):
    async with storage_access.engine(db).begin() as connection:
        await connection.execute(
            delete(StorageOperation).where(
                StorageOperation.location_id
                == uuid.UUID(row.storage_ref["location_id"]),
                StorageOperation.purpose == uploads.read_purpose(row.id),
            )
        )


@pytest.mark.parametrize("data", [b"", b"plain\x00general\xff", b"x" * (20 * 1024**2)])
async def test_general_and_empty_upload_preserves_exact_receipt_and_bytes(
    data, httpx_client, super_admin, discussion, objects, db_session
):
    value = await upload(httpx_client, super_admin, discussion[1], data=data)
    row = await db_session.scalar(select(CommentAttachmentUpload))
    assert row.state == "ready" and row.bound_comment_id is None
    assert value == uploads.attachment(row).model_dump(by_alias=True)
    assert value["mimeType"] == "application/pdf" and value["fileName"] == "说明.pdf"
    assert row.sha256 == hashlib.sha256(data).hexdigest() and row.size_bytes == len(
        data
    )
    assert row.expires_at - row.created_at == timedelta(days=90)
    assert (row.annotation_id, row.project_id, row.owner_id) == (
        discussion[1].id,
        discussion[0].id,
        super_admin[0].id,
    )
    assert (
        objects.objects[row.storage_ref["bucket"], row.storage_ref["object_key"]]
        == data
    )
    assert all(body.closed for body in objects.bodies)


async def test_oversize_body_has_no_owner_or_io(
    httpx_client, super_admin, discussion, objects, db_session
):
    response = await httpx_client.post(
        path(discussion[1].id, "upload"),
        params={"file_name": "large.bin"},
        content=b"x" * (20 * 1024**2 + 1),
        headers=bearer(super_admin[1]),
    )
    assert response.status_code == 413, response.text
    assert await db_session.scalar(select(CommentAttachmentUpload.id)) is None
    assert not objects.puts


async def test_old_initializer_is_deprecated_without_issuing_url(
    httpx_client, super_admin, discussion, objects
):
    url = path(discussion[1].id, "upload-init")
    response = await httpx_client.post(
        url,
        json={"file_name": "general.bin", "content_type": "application/octet-stream"},
        headers=bearer(super_admin[1]),
    )
    assert response.status_code == 409 and "upload_path" in response.json()["detail"]
    assert "upload_url" not in response.text and not objects.puts
    response = await httpx_client.post(
        url, json={"file_name": ""}, headers=bearer(super_admin[1])
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "change", ["duplicate", "name", "mime", "size", "foreign", "annotation"]
)
async def test_binding_requires_exact_once_only_server_receipt(
    change, httpx_client, super_admin, discussion, objects, db_session, reviewer
):
    value = await upload(httpx_client, super_admin, discussion[1])
    owner = await db_session.scalar(select(CommentAttachmentUpload))
    values = [dict(value)]
    target = discussion[1]
    if change == "duplicate":
        values *= 2
    elif change in {"name", "mime", "size"}:
        field, changed = {
            "name": ("fileName", "false.bin"),
            "mime": ("mimeType", "text/plain"),
            "size": ("size", 999),
        }[change]
        values[0][field] = changed
    elif change == "foreign":
        # A current project member can discuss the annotation but cannot claim
        # another uploader's receipt.
        db_session.add(
            ProjectMember(
                project_id=discussion[0].id,
                user_id=reviewer[0].id,
                assigned_by=super_admin[0].id,
                role="reviewer",
            )
        )
        task = await db_session.get(Task, target.task_id)
        task.reviewer_id = reviewer[0].id
        await db_session.flush()
    else:
        _, target = await _seed_project_with_annotation(db_session, super_admin[0].id)
    actor = reviewer if change == "foreign" else super_admin
    response = await httpx_client.post(
        f"/api/v1/annotations/{target.id}/comments",
        json={"body": "attachment", "attachments": values},
        headers=bearer(actor[1]),
    )
    assert response.status_code == (403 if change == "foreign" else 409), response.text
    await db_session.refresh(owner)
    assert owner.bound_comment_id is None and owner.state == "ready"


async def test_binding_download_saved_bucket_and_mirror_metadata_only(
    httpx_client, super_admin, discussion, objects, db_session
):
    value = await upload(httpx_client, super_admin, discussion[1])
    created = await comment(httpx_client, super_admin, discussion[1], [value])
    row = await db_session.scalar(select(CommentAttachmentUpload))
    assert row.bound_comment_id == uuid.UUID(created["id"])
    mirror = await db_session.scalar(select(AnnotationFeedback))
    assert mirror.attachments == [value]
    objects.bucket = "new-write-bucket"
    objects.objects[objects.bucket, value["storageKey"]] = b"wrong replacement"
    for as_json in (False, True):
        response = await download(
            httpx_client,
            super_admin,
            discussion[1],
            value["storageKey"],
            as_json=as_json,
        )
        assert response.status_code == 200 and response.content == b"general\x00file", (
            response.text
        )
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["content-disposition"].startswith("attachment;")
        assert "location" not in response.headers
    repeated = await httpx_client.post(
        f"/api/v1/annotations/{discussion[1].id}/comments",
        json={"body": "again", "attachments": [value]},
        headers=bearer(super_admin[1]),
    )
    assert repeated.status_code == 409
    assert all(body.closed for body in objects.bodies)


async def test_unknown_legacy_metadata_is_not_downloaded_or_cleaned(
    httpx_client, super_admin, discussion, objects, db_session
):
    key = f"comment-attachments/{discussion[1].id}/{uuid.uuid4()}-old.pdf"
    row = AnnotationComment(
        annotation_id=discussion[1].id,
        project_id=discussion[0].id,
        author_id=super_admin[0].id,
        body="old",
        attachments=[
            {
                "storageKey": key,
                "fileName": "old.pdf",
                "mimeType": "application/pdf",
                "size": 4,
            }
        ],
        is_active=False,
    )
    db_session.add(row)
    await db_session.flush()
    response = await download(httpx_client, super_admin, discussion[1], key)
    assert response.status_code == 409, response.text
    original = list(row.attachments)
    assert (await uploads.cleanup(db_session))["objects"] == 0
    await db_session.refresh(row)
    assert row.attachments == original and not objects.reads and not objects.deleted


@pytest.mark.parametrize(
    "change,status", [("missing", 404), ("corrupt", 409), ("endpoint", 409)]
)
async def test_known_read_errors_do_not_redirect(
    change, status, httpx_client, super_admin, discussion, objects, db_session
):
    value = await upload(httpx_client, super_admin, discussion[1])
    await comment(httpx_client, super_admin, discussion[1], [value])
    row = await db_session.scalar(select(CommentAttachmentUpload))
    identity = row.storage_ref["bucket"], value["storageKey"]
    objects.reads.clear()
    if change == "missing":
        objects.objects.pop(identity)
    elif change == "corrupt":
        objects.objects[identity] = b"different bytes"
    else:
        objects.client.meta.endpoint_url = "http://wrong.invalid"
    response = await download(
        httpx_client, super_admin, discussion[1], value["storageKey"]
    )
    assert response.status_code == status, response.text
    assert "location" not in response.headers
    assert all(body.closed for body in objects.bodies)


@pytest.mark.parametrize("stage", ["body", "admission", "final"])
@pytest.mark.parametrize(
    "change", ["inactive", "key", "hash", "scopes", "expiry", "context"]
)
async def test_original_authority_and_context_windows(
    stage,
    change,
    httpx_client,
    super_admin,
    discussion,
    objects,
    db_session,
    monkeypatch,
):
    created = await key_for(httpx_client, super_admin)
    actor = super_admin[0], created["plaintext"]
    key = await db_session.get(ApiKey, uuid.UUID(created["id"]))

    async def mutate():
        if change == "inactive":
            actor[0].is_active = False
        elif change == "key":
            key.revoked_at = uploads.now()
        elif change == "hash":
            key.key_hash = hash_password("rotated")
        elif change == "scopes":
            key.scopes = ["*"]
        elif change == "expiry":
            key.expires_at = uploads.now() - timedelta(seconds=1)
        else:
            (await db_session.get(Task, discussion[1].task_id)).status = "pending"
        await db_session.flush()

    if stage == "body":
        original = uploads.capture_upload

        async def capture(*args, **kwargs):
            proof = await original(*args, **kwargs)
            await mutate()
            return proof

        monkeypatch.setattr(uploads, "capture_upload", capture)
    elif stage == "admission":
        original = storage_access.acquire

        async def acquire(*args, **kwargs):
            permit = await original(*args, **kwargs)
            await mutate()
            return permit

        monkeypatch.setattr(storage_access, "acquire", acquire)
    else:
        original = storage_access.run

        async def run(*args, **kwargs):
            result = await original(*args, **kwargs)
            await mutate()
            return result

        monkeypatch.setattr(storage_access, "run", run)
    response = await httpx_client.post(
        path(discussion[1].id, "upload"),
        params={"file_name": "file.bin"},
        content=b"general",
        headers=bearer(actor[1]),
    )
    assert response.status_code == (409 if change == "context" else 403), response.text
    row = await db_session.scalar(select(CommentAttachmentUpload))
    assert (row is None) is (stage == "body")
    if row is not None:
        assert row.state == "pending"
    assert bool(objects.puts) is (stage == "final")


@pytest.mark.parametrize("change", ["actor", "member", "binding", "comment", "gate"])
async def test_read_final_rechecks_current_authority_and_original_binding(
    change,
    httpx_client,
    super_admin,
    reviewer,
    discussion,
    objects,
    db_session,
    monkeypatch,
):
    value = await upload(httpx_client, super_admin, discussion[1])
    created = await comment(httpx_client, super_admin, discussion[1], [value])
    db_session.add(
        ProjectMember(
            project_id=discussion[0].id,
            user_id=reviewer[0].id,
            assigned_by=super_admin[0].id,
            role="reviewer",
        )
    )
    task = await db_session.get(Task, discussion[1].task_id)
    task.reviewer_id = reviewer[0].id
    await db_session.flush()
    original = storage_access.run

    async def run(*args, **kwargs):
        result = await original(*args, **kwargs)
        if change == "actor":
            reviewer[0].is_active = False
        elif change == "member":
            member = await db_session.scalar(
                select(ProjectMember).where(ProjectMember.user_id == reviewer[0].id)
            )
            await db_session.delete(member)
        elif change in {"binding", "comment"}:
            current = await db_session.get(AnnotationComment, uuid.UUID(created["id"]))
            if change == "binding":
                current.attachments = []
            else:
                current.is_active = False
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
    response = await download(
        httpx_client, reviewer, discussion[1], value["storageKey"]
    )
    assert (
        response.status_code
        == {"actor": 403, "member": 404, "binding": 409, "comment": 404, "gate": 409}[
            change
        ]
    ), response.text
    assert all(body.closed for body in objects.bodies)


@pytest.mark.parametrize("credential", ["key", "jwt"])
async def test_natural_expiry_during_final_gate_wait(
    credential, httpx_client, super_admin, discussion, objects, db_session, monkeypatch
):
    token = create_access_token(
        str(super_admin[0].id), "super_admin", timedelta(seconds=60)
    )
    expiration = authority.now() + timedelta(seconds=30)
    if credential == "key":
        created = await key_for(httpx_client, super_admin)
        key = await db_session.get(ApiKey, uuid.UUID(created["id"]))
        key.expires_at = expiration
        await db_session.flush()
        token = created["plaintext"]
    else:
        from datetime import datetime, timezone

        expiration = datetime.fromtimestamp(
            decode_access_token(token)["exp"], timezone.utc
        )
    original = storage_access.check

    async def check(*args, **kwargs):
        result = await original(*args, **kwargs)
        if kwargs.get("lock"):
            monkeypatch.setattr(
                authority, "now", lambda: expiration + timedelta(seconds=1)
            )
        return result

    monkeypatch.setattr(storage_access, "check", check)
    response = await httpx_client.post(
        path(discussion[1].id, "upload"),
        params={"file_name": "file.bin"},
        content=b"general",
        headers=bearer(token),
    )
    assert response.status_code == 403, response.text
    assert (await db_session.scalar(select(CommentAttachmentUpload))).state == "pending"
    assert objects.puts


@pytest.mark.parametrize("commit_number", [1, 2, 3])
async def test_read_rechecks_frozen_expiry_after_each_real_commit_wait(
    commit_number,
    httpx_client,
    super_admin,
    discussion,
    objects,
    db_session,
    monkeypatch,
):
    value = await upload(httpx_client, super_admin, discussion[1])
    created = await comment(httpx_client, super_admin, discussion[1], [value])
    row = await db_session.scalar(select(CommentAttachmentUpload))
    receipt = storage.snapshot(row)
    objects.reads.clear()
    objects.bodies.clear()
    monkeypatch.setattr(
        uploads, "now", lambda: receipt.expires_at - timedelta(seconds=1)
    )
    acquire = AsyncMock(wraps=storage_access.acquire)
    monkeypatch.setattr(storage_access, "acquire", acquire)
    original_commit, commits = db_session.commit, 0

    async def commit():
        nonlocal commits
        commits += 1
        await original_commit()
        if commits == commit_number:
            # A post-commit check must use frozen facts, even if ORM fields expire.
            db_session.expire(row, ["expires_at"])
            monkeypatch.setattr(uploads, "now", lambda: receipt.expires_at)

    monkeypatch.setattr(db_session, "commit", commit)
    response = await download(
        httpx_client, super_admin, discussion[1], value["storageKey"]
    )
    assert response.status_code == 410, response.text
    assert response.json()["detail"]["code"] == "comment_attachment_expired"
    assert commits == commit_number
    assert acquire.await_count == (0 if commit_number == 1 else 1)
    assert len(objects.reads) == (1 if commit_number == 3 else 0)
    assert all(body.closed for body in objects.bodies)
    assert not objects.deleted and not await operation_ids(db_session, receipt)
    current = await db_session.scalar(
        select(CommentAttachmentUpload)
        .where(CommentAttachmentUpload.id == receipt.id)
        .execution_options(populate_existing=True)
    )
    assert current.state == "ready" and storage.same(current, receipt)
    assert current.bound_comment_id == uuid.UUID(created["id"])


@pytest.mark.parametrize("commit_number", [1, 2, 3])
async def test_upload_rechecks_frozen_expiry_after_pending_acquired_and_final_commit(
    commit_number,
    httpx_client,
    super_admin,
    discussion,
    objects,
    db_session,
    monkeypatch,
):
    capture = uploads.capture_upload
    original_commit, commits = db_session.commit, 0
    acquire = AsyncMock(wraps=storage_access.acquire)
    monkeypatch.setattr(storage_access, "acquire", acquire)
    frozen = {}

    async def commit():
        nonlocal commits
        commits += 1
        row = next(
            value
            for value in db_session.identity_map.values()
            if isinstance(value, CommentAttachmentUpload)
        )
        receipt = storage.snapshot(row)
        await original_commit()
        if commits == commit_number:
            frozen["receipt"] = receipt
            db_session.expire(row, ["expires_at"])
            monkeypatch.setattr(uploads, "now", lambda: receipt.expires_at)

    async def captured(*args, **kwargs):
        result = await capture(*args, **kwargs)
        # Only count the three owner commits, after the body preflight's commit.
        monkeypatch.setattr(db_session, "commit", commit)
        return result

    monkeypatch.setattr(uploads, "capture_upload", captured)
    response = await httpx_client.post(
        path(discussion[1].id, "upload"),
        params={"file_name": "file.bin"},
        content=b"general",
        headers=bearer(super_admin[1]),
    )
    assert response.status_code == 410, response.text
    assert response.json()["detail"]["code"] == "comment_attachment_expired"
    assert commits == commit_number
    assert acquire.await_count == (0 if commit_number == 1 else 1)
    assert len(objects.puts) == len(objects.reads) == (1 if commit_number == 3 else 0)
    assert all(body.closed for body in objects.bodies)
    assert not objects.deleted
    receipt = frozen["receipt"]
    current = await db_session.scalar(
        select(CommentAttachmentUpload)
        .where(CommentAttachmentUpload.id == receipt.id)
        .execution_options(populate_existing=True)
    )
    assert current.state == ("ready" if commit_number == 3 else "pending")
    assert storage.same(current, receipt) and current.bound_comment_id is None
    assert current.retired_at is None


@pytest.mark.parametrize("stage", ["commit", "notification", "refresh"])
@pytest.mark.parametrize("deadline", ["attachment", "key", "jwt"])
async def test_binding_last_wait_expiry_preserves_the_committed_comment_and_owner(
    stage,
    deadline,
    httpx_client,
    super_admin,
    discussion,
    objects,
    db_session,
    monkeypatch,
):
    from app.services.notification import NotificationService

    value = await upload(httpx_client, super_admin, discussion[1])
    row = await db_session.scalar(select(CommentAttachmentUpload))
    receipt = storage.snapshot(row)
    token = super_admin[1]
    expiration = receipt.expires_at
    if deadline == "key":
        created = await key_for(httpx_client, super_admin)
        token = created["plaintext"]
        key = await db_session.get(ApiKey, uuid.UUID(created["id"]))
        expiration = authority.now() + timedelta(seconds=60)
        key.expires_at = expiration
        await db_session.flush()
    elif deadline == "jwt":
        token = create_access_token(
            str(super_admin[0].id), "super_admin", timedelta(seconds=60)
        )
        expiration = datetime.fromtimestamp(
            decode_access_token(token)["exp"], timezone.utc
        )
    expired = []

    def expire():
        expired.append(stage)
        monkeypatch.setattr(
            uploads if deadline == "attachment" else authority,
            "now",
            lambda: expiration,
        )

    if stage == "commit":
        original_commit = db_session.commit

        async def commit():
            bound = row.bound_comment_id is not None
            await original_commit()
            if bound:
                # Preserve the committed first binding; expiry cannot undo it.
                db_session.expire(row, ["expires_at"])
                expire()

        monkeypatch.setattr(db_session, "commit", commit)
    elif stage == "notification":
        publish = NotificationService.publish_committed

        async def published(self, *args, **kwargs):
            result = await publish(self, *args, **kwargs)
            db_session.expire(row, ["expires_at"])
            expire()
            return result

        monkeypatch.setattr(NotificationService, "publish_committed", published)
    else:
        original_refresh = db_session.refresh

        async def refresh(instance, *args, **kwargs):
            result = await original_refresh(instance, *args, **kwargs)
            if isinstance(instance, AnnotationComment):
                db_session.expire(row, ["expires_at"])
                expire()
            return result

        monkeypatch.setattr(db_session, "refresh", refresh)

    response = await httpx_client.post(
        f"/api/v1/annotations/{discussion[1].id}/comments",
        json={"body": "", "attachments": [value]},
        headers=bearer(token),
    )
    assert response.status_code == (410 if deadline == "attachment" else 403), (
        response.text
    )
    assert response.json()["detail"]["code"] == (
        "comment_attachment_expired"
        if deadline == "attachment"
        else "comment_attachment_authority_revoked"
    )
    assert expired == [stage]
    comments = list(await db_session.scalars(select(AnnotationComment)))
    mirrors = list(await db_session.scalars(select(AnnotationFeedback)))
    assert len(comments) == len(mirrors) == 1
    assert comments[0].attachments == mirrors[0].attachments == [value]
    current = await db_session.scalar(
        select(CommentAttachmentUpload)
        .where(CommentAttachmentUpload.id == receipt.id)
        .execution_options(populate_existing=True)
    )
    assert current.state == "ready" and current.retired_at is None
    assert current.bound_comment_id == comments[0].id
    assert (
        current.storage_ref == receipt.storage_ref and current.sha256 == receipt.sha256
    )
    assert current.expires_at == receipt.expires_at
    assert len(objects.puts) == 1 and not objects.deleted


async def test_body_only_comment_does_not_enter_attachment_deadline_protocol(
    httpx_client, super_admin, discussion, db_session, monkeypatch
):
    check = Mock(
        side_effect=AssertionError("body-only comments have no attachment owner")
    )
    monkeypatch.setattr(uploads, "check_deadlines", check)
    response = await httpx_client.post(
        f"/api/v1/annotations/{discussion[1].id}/comments",
        json={"body": "Body only", "attachments": []},
        headers=bearer(super_admin[1]),
    )
    assert response.status_code == 201, response.text
    assert response.json()["body"] == "Body only"
    check.assert_not_called()
    assert await db_session.scalar(select(CommentAttachmentUpload.id)) is None


@pytest.mark.parametrize("operation", ["upload", "read"])
async def test_executor_queue_wait_rechecks_expiry_before_actual_storage_io(
    operation,
    httpx_client,
    super_admin,
    discussion,
    objects,
    db_session,
    monkeypatch,
):
    value = None
    if operation == "read":
        value = await upload(httpx_client, super_admin, discussion[1])
        await comment(httpx_client, super_admin, discussion[1], [value])
    objects.reads.clear()
    objects.bodies.clear()
    initial_puts = len(objects.puts)
    original_run = storage_access.run
    entered, release = threading.Event(), threading.Event()
    frozen = {}

    async def run(db, location_id, purpose, call, *args, **kwargs):
        row = next(
            item
            for item in db_session.identity_map.values()
            if isinstance(item, CommentAttachmentUpload)
        )
        frozen["receipt"] = storage.snapshot(row)

        def queued(*call_args, **call_kwargs):
            entered.set()
            assert release.wait(10)
            return call(*call_args, **call_kwargs)

        return await original_run(db, location_id, purpose, queued, *args, **kwargs)

    monkeypatch.setattr(storage_access, "run", run)
    if operation == "upload":
        request = asyncio.create_task(
            httpx_client.post(
                path(discussion[1].id, "upload"),
                params={"file_name": "queued.bin"},
                content=b"general",
                headers=bearer(super_admin[1]),
            )
        )
    else:
        request = asyncio.create_task(
            download(httpx_client, super_admin, discussion[1], value["storageKey"])
        )
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        expiration = frozen["receipt"].expires_at
        monkeypatch.setattr(uploads, "now", lambda: expiration)
    finally:
        release.set()
        response = await request
    assert response.status_code == 410, response.text
    assert response.json()["detail"]["code"] == "comment_attachment_expired"
    assert not objects.reads and not objects.bodies and not objects.deleted
    assert len(objects.puts) == initial_puts
    receipt = frozen["receipt"]
    current = await db_session.scalar(
        select(CommentAttachmentUpload)
        .where(CommentAttachmentUpload.id == receipt.id)
        .execution_options(populate_existing=True)
    )
    assert current.state == ("pending" if operation == "upload" else "ready")
    assert storage.same(current, receipt)


async def test_retirement_grace_expiry_and_feedback_do_not_keep_owner_alive(
    httpx_client, super_admin, discussion, objects, db_session, monkeypatch
):
    value = await upload(httpx_client, super_admin, discussion[1])
    created = await comment(httpx_client, super_admin, discussion[1], [value])
    row = await db_session.scalar(select(CommentAttachmentUpload))
    original = storage.snapshot(row)
    response = await httpx_client.delete(
        f"/api/v1/comments/{created['id']}", headers=bearer(super_admin[1])
    )
    assert response.status_code == 204, response.text
    await db_session.refresh(row)
    assert row.state == "retired" and row.retired_at is not None
    retired_at = row.retired_at
    mirror = await db_session.scalar(select(AnnotationFeedback))
    assert mirror.is_active and mirror.attachments == [value]
    assert (await uploads.cleanup(db_session))["objects"] == 0
    assert not objects.deleted
    monkeypatch.setattr(
        uploads, "now", lambda: retired_at + timedelta(days=7, seconds=1)
    )
    objects.fail_delete = True
    assert (await uploads.cleanup(db_session))["failed"] == 1
    await db_session.refresh(row)
    assert storage.same(row, original) and row.retired_at == retired_at
    current = await db_session.get(AnnotationComment, uuid.UUID(created["id"]))
    assert current.attachments == [value]
    objects.fail_delete = False
    assert (await uploads.cleanup(db_session))["objects"] == 1
    assert await db_session.get(CommentAttachmentUpload, row.id) is None
    await db_session.refresh(mirror)
    assert mirror.attachments == [value]


async def test_active_original_protects_until_expiry_but_native_metadata_does_not(
    httpx_client, super_admin, discussion, objects, db_session, monkeypatch
):
    value = await upload(httpx_client, super_admin, discussion[1])
    created = await comment(httpx_client, super_admin, discussion[1], [value])
    row = await db_session.scalar(select(CommentAttachmentUpload))
    assert (await uploads.cleanup(db_session, identifiers=[row.id]))["retained"] == 1
    expiration = row.expires_at
    monkeypatch.setattr(uploads, "now", lambda: expiration + timedelta(seconds=1))
    assert (
        await download(httpx_client, super_admin, discussion[1], value["storageKey"])
    ).status_code == 410
    assert (await uploads.cleanup(db_session))["objects"] == 1
    assert (await db_session.get(AnnotationComment, uuid.UUID(created["id"]))).is_active


async def test_hard_delete_hook_accepts_select_without_commit(
    httpx_client, super_admin, discussion, objects, db_session
):
    value = await upload(httpx_client, super_admin, discussion[1])
    created = await comment(httpx_client, super_admin, discussion[1], [value])
    commit = db_session.commit
    db_session.commit = AsyncMock(side_effect=AssertionError("hook must not commit"))
    try:
        ids = await uploads.retire_for_comments(
            db_session,
            select(AnnotationComment.id).where(
                AnnotationComment.id == uuid.UUID(created["id"])
            ),
        )
    finally:
        db_session.commit = commit
    assert len(ids) == 1
    row = await db_session.get(CommentAttachmentUpload, ids[0])
    assert row.state == "retired" and not objects.deleted


async def test_unknown_read_close_retains_permit_and_blocks_expired_cleanup(
    httpx_client, super_admin, discussion, objects, db_session, monkeypatch
):
    value = await upload(httpx_client, super_admin, discussion[1])
    await comment(httpx_client, super_admin, discussion[1], [value])
    row = await db_session.scalar(select(CommentAttachmentUpload))

    class UnknownClose(io.BytesIO):
        def close(self):
            super().close()
            raise OSError("unconfirmed close")

    objects.body_factory = UnknownClose
    try:
        response = await download(
            httpx_client, super_admin, discussion[1], value["storageKey"]
        )
        assert response.status_code == 503, response.text
        assert len(await operation_ids(db_session, row)) == 1
        monkeypatch.setattr(
            uploads, "now", lambda: row.expires_at + timedelta(seconds=1)
        )
        assert (await uploads.cleanup(db_session))["objects"] == 0
        assert not objects.deleted
    finally:
        assert all(body.closed for body in objects.bodies)
        await clear_closed_operations(db_session, row)


async def test_cancelled_read_waits_for_actual_body_close(
    httpx_client, super_admin, discussion, objects, db_session
):
    value = await upload(httpx_client, super_admin, discussion[1])
    await comment(httpx_client, super_admin, discussion[1], [value])
    row = await db_session.scalar(select(CommentAttachmentUpload))
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()

    class BlockingBody(io.BytesIO):
        def read(self, size=-1):
            entered.set()
            assert release.wait(10)
            return super().read(size)

        def close(self):
            super().close()
            closed.set()

    objects.body_factory = BlockingBody
    request = Request(
        {
            "type": "http",
            "headers": [(b"authorization", ("Bearer " + super_admin[1]).encode())],
        }
    )
    task = asyncio.create_task(
        uploads.read(
            db_session, super_admin[0], request, discussion[1].id, value["storageKey"]
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not closed.is_set()
    finally:
        release.set()
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            assert closed.is_set()
            try:
                assert not await operation_ids(db_session, row)
            finally:
                await clear_closed_operations(db_session, row)
