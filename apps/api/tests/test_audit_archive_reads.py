"""Archive HTTP authority, independent ownership and physical-read boundaries."""

import asyncio
from datetime import date, datetime, timedelta, timezone
import gzip
import hashlib
import io
import json
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
from app.db.enums import UserRole
from app.db.models.api_key import ApiKey
from app.db.models.audit_archive_output import AuditArchiveOutput
from app.db.models.storage_maintenance import StorageOperation
from app.services import audit_archive_reads as reads
from app.services import audit_archive_storage as storage, storage_access
from tests.feedback_committed_fixtures import (
    rollback_audit_world as rollback_audit_world,
)
from tests.test_reference_original_cache import referenced_cache as referenced_cache

ARCHIVES = "/api/v1/audit-logs/archives"


def bearer(token):
    return {"Authorization": "Bearer " + token}


def request(token, key=None):
    value = Request(
        {"type": "http", "headers": [(b"authorization", ("Bearer " + token).encode())]}
    )
    value.state.api_key_id = str(key.id) if key else None
    value.state.api_key_scopes = list(key.scopes) if key else None
    if key is not None:
        value.state.identity_principal = SimpleNamespace(
            actor_id=key.user_id,
            credential_type="api_key",
            credential_id=key.id,
            scopes=tuple(key.scopes),
            expires_at=key.expires_at,
            generation=0,
        )
    else:
        payload = decode_access_token(token)
        value.state.identity_principal = SimpleNamespace(
            actor_id=uuid.UUID(payload["sub"]),
            credential_type="jwt",
            credential_id=payload.get("jti"),
            scopes=None,
            expires_at=datetime.fromtimestamp(payload["exp"], timezone.utc),
            generation=int(payload.get("gen", 0)),
        )
    return value


class ArchiveObjects:
    def __init__(self):
        self.client = SimpleNamespace(
            meta=SimpleNamespace(
                endpoint_url=f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}"
            ),
            head_object=self.head,
            get_object=self.get,
            list_objects_v2=Mock(side_effect=AssertionError("unexpected S3 listing")),
        )
        self.audit_archive_bucket = settings.minio_audit_archive_bucket
        self.objects, self.heads, self.reads, self.bodies = {}, [], [], []
        self.body_factory = io.BytesIO

    def head(self, *, Bucket, Key):  # noqa: N803
        self.heads.append((Bucket, Key))
        try:
            value = self.objects[Bucket, Key]
        except KeyError:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "HeadObject") from None
        return {
            "ContentLength": len(value),
            "ETag": '"' + hashlib.sha256(value).hexdigest() + '"',
        }

    def get(self, *, Bucket, Key, IfMatch):  # noqa: N803
        metadata = self.head(Bucket=Bucket, Key=Key)
        assert metadata["ETag"] == IfMatch
        self.reads.append((Bucket, Key))
        body = self.body_factory(self.objects[Bucket, Key])
        self.bodies.append(body)
        return {"Body": body, **metadata}


@pytest.fixture
def objects(monkeypatch):
    value = ArchiveObjects()
    monkeypatch.setattr(storage, "storage_service", value)
    return value


def source_rows():
    return [
        {
            "id": index,
            "actor_id": None,
            "actor_email": "historical@example.test",
            "actor_role": "super_admin",
            "action": "audit.export",
            "target_type": "audit_log",
            "target_id": None,
            "method": "GET",
            "path": "/api/v1/audit-logs/export",
            "status_code": 200,
            "ip": "127.0.0.1",
            "detail_json": {"ordinal": index},
            "request_id": None,
            "created_at": datetime(2024, 1, index, tzinfo=timezone.utc),
        }
        for index in (1, 2)
    ]


async def seed_archive(db, objects, *, ready=True, month=1, tail=None):
    canonical = [storage.canonical_line(row) for row in source_rows()]
    source = b"".join(canonical)
    started = reads.now() - timedelta(seconds=2)
    row = AuditArchiveOutput(
        id=uuid.uuid4(),
        job_id=uuid.uuid4(),
        year=2024,
        month=month,
        source_schema="public",
        source_table=f"audit_logs_y2024m{month:02d}",
        parent_oid=1234,
        source_oid=5678,
        month_start=date(2024, month, 1),
        month_end=date(2024, month + 1, 1),
        format_revision=1,
        row_count=len(canonical),
        source_size_bytes=len(source),
        source_sha256=hashlib.sha256(source).hexdigest(),
        state="pending",
        created_at=started,
        published_at=None,
    )
    payload = source + storage.encode_meta(row)
    if tail is not None:
        payload += tail
    compressed = gzip.compress(payload, mtime=0)
    row.sha256, row.size_bytes = hashlib.sha256(compressed).hexdigest(), len(compressed)
    row.storage_ref = await storage.allocate(db, row)
    db.add(row)
    await db.flush()
    if ready:
        row.state, row.published_at = "ready", reads.now()
        await db.flush()
    objects.objects[row.storage_ref["bucket"], row.storage_ref["object_key"]] = (
        compressed
    )
    return row, [json.loads(line) for line in canonical]


async def read(client, token, **params):
    return await client.get(ARCHIVES + "/2024/1", params=params, headers=bearer(token))


def operation_filter(row):
    return (
        StorageOperation.location_id == uuid.UUID(row.storage_ref["location_id"]),
        StorageOperation.purpose == reads.read_purpose(row.id),
    )


async def owned_operations(db, row):
    async with storage_access.engine(db).connect() as connection:
        return set(
            await connection.scalars(
                select(StorageOperation.id).where(*operation_filter(row))
            )
        )


async def clear_closed_test_operations(db, row):
    async with storage_access.engine(db).begin() as connection:
        await connection.execute(delete(StorageOperation).where(*operation_filter(row)))


async def test_list_is_ready_database_ownership_without_bucket_probe(
    httpx_client, super_admin, db_session, objects, monkeypatch
):
    baseline = await httpx_client.get(ARCHIVES, headers=bearer(super_admin[1]))
    assert baseline.status_code == 200, baseline.text
    ready, _ = await seed_archive(db_session, objects)
    await seed_archive(db_session, objects, ready=False, month=2)
    objects.audit_archive_bucket = "new-write-bucket"
    objects.client.meta.endpoint_url = "http://unavailable-endpoint.invalid"
    monkeypatch.setattr(
        storage, "read", Mock(side_effect=AssertionError("unexpected I/O"))
    )
    response = await httpx_client.get(ARCHIVES, headers=bearer(super_admin[1]))
    assert response.status_code == 200, response.text
    expected = [
        *baseline.json(),
        {
            "year": 2024,
            "month": 1,
            "object_key": ready.storage_ref["object_key"],
            "size_bytes": ready.size_bytes,
            "archived_at": ready.published_at.isoformat(),
        },
    ]
    assert response.json() == sorted(
        expected, key=lambda row: (row["year"], row["month"]), reverse=True
    )
    assert not objects.heads and not objects.reads
    objects.client.list_objects_v2.assert_not_called()


@pytest.mark.parametrize("pending", [False, True])
async def test_unknown_or_pending_month_has_zero_io(
    pending, httpx_client, super_admin, db_session, objects
):
    if pending:
        await seed_archive(db_session, objects, ready=False)
    response = await read(httpx_client, super_admin[1])
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "audit_archive_storage_unpinned"
    assert not objects.heads and not objects.reads


@pytest.mark.parametrize("path", [ARCHIVES, ARCHIVES + "/2024/1"])
async def test_employee_cannot_list_or_read(path, httpx_client, annotator, objects):
    response = await httpx_client.get(path, headers=bearer(annotator[1]))
    assert response.status_code == 403, response.text
    assert not objects.heads


async def test_saved_bucket_and_bounded_page_include_literal_metadata(
    httpx_client, super_admin, db_session, objects
):
    row, expected = await seed_archive(db_session, objects)
    original = (row.storage_ref["bucket"], row.storage_ref["object_key"])
    objects.audit_archive_bucket = "new-write-bucket"
    objects.objects[objects.audit_archive_bucket, original[1]] = (
        b"unrelated replacement"
    )
    response = await read(httpx_client, super_admin[1], limit=1, offset=1)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "items": [expected[1]],
        "total": 3,
        "limit": 1,
        "offset": 1,
        "year": 2024,
        "month": 1,
    }
    trailer = await read(httpx_client, super_admin[1], limit=1, offset=2)
    assert trailer.status_code == 200, trailer.text
    assert trailer.json()["items"] == [storage.metadata(row)]
    beyond = await read(httpx_client, super_admin[1], limit=1, offset=300)
    assert beyond.status_code == 200 and beyond.json()["items"] == []
    assert objects.reads == [original] * 3
    assert all(body.closed for body in objects.bodies)
    assert not await owned_operations(db_session, row)


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 1001}, {"offset": -1}])
async def test_page_validation_has_zero_io(params, httpx_client, super_admin, objects):
    response = await read(httpx_client, super_admin[1], **params)
    assert response.status_code == 422, response.text
    assert not objects.heads


@pytest.mark.parametrize(
    "change,status,code",
    [
        ("missing", 404, "missing"),
        ("corrupt", 409, "integrity_mismatch"),
        ("tail", 409, "integrity_mismatch"),
        ("budget", 409, "budget"),
        ("unavailable", 503, "unavailable"),
    ],
)
async def test_no_partial_page_escapes_full_validation(
    change, status, code, httpx_client, super_admin, db_session, objects, monkeypatch
):
    row, _ = await seed_archive(
        db_session,
        objects,
        tail=b'{"secret":"invalid trailing row"}\n' if change == "tail" else None,
    )
    identity = row.storage_ref["bucket"], row.storage_ref["object_key"]
    if change == "missing":
        objects.objects.pop(identity)
    elif change == "corrupt":
        value = bytearray(objects.objects[identity])
        value[-5] ^= 1
        objects.objects[identity] = bytes(value)
    elif change == "budget":
        monkeypatch.setattr(storage, "VERIFY_SECONDS", -1)
    elif change == "unavailable":
        monkeypatch.setattr(
            objects.client,
            "head_object",
            Mock(side_effect=OSError("private token/endpoint")),
        )
    response = await read(httpx_client, super_admin[1], limit=1)
    assert response.status_code == status, response.text
    assert response.json() == {"detail": {"code": "audit_archive_storage_" + code}}
    assert "location" not in response.headers
    assert all(body.closed for body in objects.bodies)
    assert not await owned_operations(db_session, row)


@pytest.mark.parametrize("path", [ARCHIVES, ARCHIVES + "/2024/1"])
async def test_narrow_admin_key_preserves_existing_role_only_contract(
    path, rollback_audit_world, objects
):
    world = rollback_audit_world
    await seed_archive(world.db, objects)
    key = await world.make_key()
    response = await world.client.get(path, headers=bearer(key["plaintext"]))
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("stage", ["admission", "final"])
@pytest.mark.parametrize(
    "change", ["inactive", "role", "hash", "scopes", "expiry", "revoked"]
)
async def test_original_authority_rechecked_before_and_after_physical_read(
    stage, change, rollback_audit_world, objects, monkeypatch
):
    httpx_client = rollback_audit_world.client
    super_admin = rollback_audit_world.super_admin
    db_session = rollback_audit_world.db
    await seed_archive(db_session, objects)
    created = await rollback_audit_world.make_key()
    key = await db_session.get(ApiKey, uuid.UUID(created["id"]))

    async def mutate():
        if change == "inactive":
            super_admin[0].is_active = False
        elif change == "role":
            super_admin[0].role = UserRole.EMPLOYEE
        elif change == "hash":
            key.key_hash = hash_password("rotated-key")
        elif change == "scopes":
            key.scopes = ["*"]
        elif change == "expiry":
            key.expires_at = reads.now() - timedelta(seconds=1)
        else:
            key.revoked_at = reads.now()
        await db_session.flush()

    if stage == "admission":
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
    response = await read(httpx_client, created["plaintext"])
    assert response.status_code == 403, response.text
    assert bool(objects.reads) is (stage == "final")
    assert all(body.closed for body in objects.bodies)


async def test_list_rechecks_current_role_after_query(
    httpx_client, super_admin, db_session, objects, monkeypatch
):
    await seed_archive(db_session, objects)
    original = reads.list_ready_outputs

    async def list_ready_outputs(db, *args, **kwargs):
        result = await original(db, *args, **kwargs)
        super_admin[0].role = UserRole.EMPLOYEE
        await db_session.flush()
        return result

    monkeypatch.setattr(reads, "list_ready_outputs", list_ready_outputs)
    response = await httpx_client.get(ARCHIVES, headers=bearer(super_admin[1]))
    assert response.status_code == 403, response.text
    assert not objects.heads


@pytest.mark.parametrize("credential", ["jwt", "key"])
async def test_natural_expiry_after_final_gate_wait(
    credential, rollback_audit_world, objects, monkeypatch
):
    httpx_client = rollback_audit_world.client
    super_admin = rollback_audit_world.super_admin
    db_session = rollback_audit_world.db
    await seed_archive(db_session, objects)
    token = create_access_token(
        str(super_admin[0].id), "super_admin", timedelta(seconds=30)
    )
    expiration = datetime.fromtimestamp(decode_access_token(token)["exp"], timezone.utc)
    if credential == "key":
        created = await rollback_audit_world.make_key(expires_at=expiration)
        token = created["plaintext"]
    original = storage_access.check

    async def check(*args, **kwargs):
        value = await original(*args, **kwargs)
        if kwargs.get("lock"):
            monkeypatch.setattr(reads, "now", lambda: expiration + timedelta(seconds=1))
        return value

    monkeypatch.setattr(storage_access, "check", check)
    response = await read(httpx_client, token)
    assert response.status_code == 403, response.text
    assert objects.reads and all(body.closed for body in objects.bodies)


@pytest.mark.parametrize("change", ["logout", "generation", "owner", "gate"])
async def test_final_jwt_owner_and_maintenance_fences(
    change, httpx_client, super_admin, db_session, objects, monkeypatch
):
    await seed_archive(db_session, objects)
    original = storage_access.run

    async def run(*args, **kwargs):
        result = await original(*args, **kwargs)
        if change == "logout":
            monkeypatch.setattr(
                reads.token_blacklist, "is_blacklisted", AsyncMock(return_value=True)
            )
        elif change == "generation":
            monkeypatch.setattr(
                reads.token_blacklist,
                "get_user_generation",
                AsyncMock(return_value=999),
            )
        elif change == "owner":
            monkeypatch.setattr(storage, "same", Mock(return_value=False))
        else:
            check = storage_access.check

            async def changed(*args, **kwargs):
                if kwargs.get("lock"):
                    raise storage_access.error("storage_maintenance_changed")
                return await check(*args, **kwargs)

            monkeypatch.setattr(storage_access, "check", changed)
        return result

    monkeypatch.setattr(storage_access, "run", run)
    response = await read(httpx_client, super_admin[1])
    assert response.status_code == (
        403 if change in {"logout", "generation"} else 409
    ), response.text
    assert objects.reads and all(body.closed for body in objects.bodies)


async def test_unknown_body_close_keeps_independent_permit(
    httpx_client, super_admin, db_session, objects
):
    row, _ = await seed_archive(db_session, objects)

    class UnknownClose(io.BytesIO):
        def close(self):
            super().close()
            raise OSError("private close failure")

    objects.body_factory = UnknownClose
    try:
        response = await read(httpx_client, super_admin[1])
        assert response.status_code == 503, response.text
        assert response.json() == {
            "detail": {"code": "audit_archive_storage_unavailable"}
        }
        assert len(await owned_operations(db_session, row)) == 1
    finally:
        assert objects.bodies and all(body.closed for body in objects.bodies)
        await clear_closed_test_operations(db_session, row)


async def test_repeated_cancellation_waits_for_physical_read_and_closure(
    db_session, super_admin, objects, monkeypatch
):
    row, _ = await seed_archive(db_session, objects)
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()

    class BlockingBody(io.BytesIO):
        def read(self, size=-1):
            entered.set()
            assert release.wait(10), "test did not release physical read"
            return super().read(size)

        def close(self):
            super().close()
            closed.set()

    objects.body_factory = BlockingBody
    task = asyncio.create_task(
        reads.read_archive_rows(
            db_session, super_admin[0], request(super_admin[1]), 2024, 1
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        assert len(await owned_operations(db_session, row)) == 1
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
            assert closed.is_set() and all(body.closed for body in objects.bodies)
            try:
                assert not await owned_operations(db_session, row)
            finally:
                await clear_closed_test_operations(db_session, row)
