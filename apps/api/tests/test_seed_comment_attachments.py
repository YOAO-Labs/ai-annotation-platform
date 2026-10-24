"""Real seed HTTP/database flows retain attachment destruction responsibility."""

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import io
import threading
from types import SimpleNamespace
import uuid

from botocore.exceptions import ClientError
import httpx
import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.v1 import _test_seed as seed, _test_seed_attachments as cleanup
from app.db.models.annotation import Annotation
from app.db.models.annotation_comment import AnnotationComment
from app.db.models.comment_attachment_upload import CommentAttachmentUpload as Upload
from app.db.models.project import Project
from app.db.models.storage_maintenance import StorageOperation
from app.deps import get_db
from app.services import comment_attachment_storage as storage
from app.services import comment_attachment_uploads as uploads, storage_access
from app.services import storage as storage_module


class Objects:
    """Fake only the object provider; all HTTP, locks and owner SQL are real."""

    def __init__(self, original):
        for name in (
            "bucket",
            "datasets_bucket",
            "bug_reports_bucket",
            "media_cache_bucket",
            "audit_archive_bucket",
            "import_bucket",
            "export_bucket",
            "avatars_bucket",
            "training_bucket",
        ):
            setattr(self, name, getattr(original, name))
        self.client = self
        self.meta = SimpleNamespace(endpoint_url=original.client.meta.endpoint_url)
        self.data, self.deleted = {}, []
        self.fail_delete = False
        self.body_factory = io.BytesIO
        self.bodies = []
        self.probe = None

    def network(self):
        if self.probe is not None:
            self.probe()

    def put_object(self, *, Bucket, Key, Body=b"", **kwargs):  # noqa: N803
        self.network()
        if kwargs.get("IfNoneMatch") == "*" and (Bucket, Key) in self.data:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        self.data[Bucket, Key] = Body.read() if hasattr(Body, "read") else bytes(Body)
        return {}

    def head_object(self, *, Bucket, Key):  # noqa: N803
        self.network()
        if (Bucket, Key) not in self.data:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "HeadObject")
        data = self.data[Bucket, Key]
        return {
            "ContentLength": len(data),
            "ETag": '"' + hashlib.sha256(data).hexdigest() + '"',
        }

    def get_object(self, *, Bucket, Key, IfMatch, **kwargs):  # noqa: N803
        self.network()
        metadata = self.head_object(Bucket=Bucket, Key=Key)
        assert metadata["ETag"] == IfMatch
        body = self.body_factory(self.data[Bucket, Key])
        self.bodies.append(body)
        return {"Body": body, **metadata}

    def delete_object(self, *, Bucket, Key):  # noqa: N803
        self.network()
        if self.fail_delete:
            raise OSError("private provider failure")
        self.data.pop((Bucket, Key), None)
        self.deleted.append((Bucket, Key))
        return {}

    def delete_objects(self, *, Bucket, Delete):  # noqa: N803
        self.network()
        for item in Delete["Objects"]:
            self.delete_object(Bucket=Bucket, Key=item["Key"])
        return {}

    def list_objects_v2(self, *, Bucket, Prefix, **kwargs):  # noqa: N803
        self.network()
        values = [
            {"Key": key}
            for bucket, key in sorted(self.data)
            if bucket == Bucket and key.startswith(Prefix)
        ]
        return {
            "Contents": values[: kwargs.get("MaxKeys", len(values))],
            "IsTruncated": False,
        }


@pytest.fixture
async def context(test_engine, app_module, monkeypatch):
    """Independent commits are required to observe genuine lock release."""
    db = async_sessionmaker(test_engine, expire_on_commit=False)()
    objects = Objects(storage_module.storage_service)
    monkeypatch.setattr(storage_module, "storage_service", objects)
    monkeypatch.setattr(storage, "storage_service", objects)
    monkeypatch.setattr(uploads, "storage_service", objects)
    monkeypatch.setattr(cleanup, "storage_service", objects)
    names = []
    operations = set()

    async def session():
        yield db

    previous = app_module.dependency_overrides.get(get_db)
    app_module.dependency_overrides[get_db] = session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_module), base_url="http://test"
    ) as client:
        value = SimpleNamespace(
            db=db,
            http=client,
            objects=objects,
            names=names,
            operations=operations,
            engine=test_engine,
        )
        try:
            yield value
        finally:
            try:
                objects.probe = None
                await db.rollback()
                # Test-created simulated permits are removed only after each test
                # has closed its own bodies/tasks in its finally block.
                async with test_engine.begin() as connection:
                    if operations:
                        await connection.execute(
                            delete(StorageOperation).where(
                                StorageOperation.id.in_(operations)
                            )
                        )
                for name in reversed(names):
                    scope = await seed._resolve_fixture_scope(
                        db, seed._parse_owned_namespace(name)
                    )
                    await db.execute(
                        delete(Upload).where(Upload.project_id.in_(scope.project_ids))
                    )
                    await db.commit()
                    objects.data = {
                        identity: raw
                        for identity, raw in objects.data.items()
                        if not any(
                            identity[1].startswith(f"comment-attachments/{identifier}/")
                            for identifier in scope.annotation_ids
                        )
                    }
                    await seed._cleanup_e2e_fixtures(
                        db, seed._parse_owned_namespace(name)
                    )
            finally:
                await db.close()
                if previous is None:
                    app_module.dependency_overrides.pop(get_db, None)
                else:
                    app_module.dependency_overrides[get_db] = previous


async def build(ctx):
    name = "sa" + uuid.uuid4().hex[:8]
    ctx.names.append(name)
    response = await ctx.http.post(
        "/api/v1/__test/seed/owned", json={"namespace": name}
    )
    assert response.status_code == 200, response.text
    world = response.json()
    login = await ctx.http.post(
        "/api/v1/__test/seed/login", json={"email": world["admin_email"]}
    )
    assert login.status_code == 200, login.text
    world.update(
        namespace=name,
        actor_id=login.json()["user"]["id"],
        headers={"Authorization": "Bearer " + login.json()["access_token"]},
    )
    response = await ctx.http.post(
        f"/api/v1/tasks/{world['task_ids'][0]}/annotations",
        headers=world["headers"],
        json={
            "annotation_type": "bbox",
            "tool_unit_id": "bbox",
            "class_name": "car",
            "geometry": {"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
        },
    )
    assert response.status_code == 201, response.text
    world["annotation_id"] = response.json()["id"]
    return world


async def upload(ctx, world, raw=b"generic\x00bytes", *, bind=True, actor=None):
    headers = (actor or world)["headers"]
    response = await ctx.http.post(
        f"/api/v1/annotations/{world['annotation_id']}/comment-attachments/upload",
        params={"file_name": "general.dat"},
        headers={**headers, "Content-Type": "application/octet-stream"},
        content=raw,
    )
    assert response.status_code == 200, response.text
    attachment = response.json()
    comment_id = None
    if bind:
        response = await ctx.http.post(
            f"/api/v1/annotations/{world['annotation_id']}/comments",
            headers=headers,
            json={"body": "", "attachments": [attachment]},
        )
        assert response.status_code == 201, response.text
        comment_id = response.json()["id"]
    row = await ctx.db.get(Upload, uuid.UUID(attachment["storageKey"].split("/")[-1]))
    await ctx.db.commit()
    return row, attachment, comment_id


async def clean(ctx, world):
    return await ctx.http.post(
        "/api/v1/__test/seed/owned-cleanup", json={"namespace": world["namespace"]}
    )


async def present(ctx, row):
    current = await ctx.db.get(Upload, row.id, populate_existing=True)
    await ctx.db.commit()
    return current


@pytest.mark.parametrize(
    "kind", ["bound", "unbound", "retired", "empty", "missing_children"]
)
async def test_terminal_owner_is_explicitly_destroyed_before_fixture_and_rebuild(
    context, kind
):
    ctx = context
    a, b = await build(ctx), await build(ctx)
    raw = b"" if kind == "empty" else b"generic\x00bytes"
    row, _, comment_id = await upload(ctx, a, raw, bind=kind != "unbound")
    neighbour, _, _ = await upload(ctx, b)
    if kind == "retired":
        result = await ctx.http.delete(
            f"/api/v1/comments/{comment_id}", headers=a["headers"]
        )
        assert result.status_code == 204
    if kind == "missing_children":
        from app.db.models.annotation_feedback import AnnotationFeedback

        await ctx.db.execute(
            delete(AnnotationFeedback).where(
                AnnotationFeedback.annotation_id == row.annotation_id
            )
        )
        await ctx.db.execute(
            delete(AnnotationComment).where(
                AnnotationComment.annotation_id == row.annotation_id
            )
        )
        await ctx.db.execute(
            delete(Annotation).where(Annotation.id == row.annotation_id)
        )
        await ctx.db.commit()
    saved = dict(row.storage_ref)
    assert (await clean(ctx, a)).status_code == 200
    assert await present(ctx, row) is None
    assert (saved["bucket"], saved["object_key"]) not in ctx.objects.data
    assert (await present(ctx, neighbour)).state == "ready"
    assert (
        ctx.objects.data[
            neighbour.storage_ref["bucket"], neighbour.storage_ref["object_key"]
        ]
        == b"generic\x00bytes"
    )
    assert (await clean(ctx, a)).status_code == 200
    rebuilt = await ctx.http.post(
        "/api/v1/__test/seed/owned", json={"namespace": a["namespace"]}
    )
    assert rebuilt.status_code == 200, rebuilt.text


async def test_uploader_in_another_scope_is_not_selected(context):
    a, b = await build(context), await build(context)
    own, _, _ = await upload(context, a)
    foreign, _, _ = await upload(context, b, bind=False, actor=a)
    assert foreign.owner_id == own.owner_id
    assert (await clean(context, a)).status_code == 200
    assert (await present(context, foreign)).state == "ready"
    assert context.objects.data[
        foreign.storage_ref["bucket"], foreign.storage_ref["object_key"]
    ]


@pytest.mark.parametrize("has_bytes", [False, True])
async def test_any_pending_refuses_with_zero_physical_delete_and_keeps_ready(
    context, has_bytes
):
    ctx = context
    world = await build(ctx)
    ready, _, _ = await upload(ctx, world)
    created = datetime.now(timezone.utc) - timedelta(days=200)
    pending = Upload(
        id=uuid.uuid4(),
        owner_id=ready.owner_id,
        annotation_id=ready.annotation_id,
        task_id=ready.task_id,
        project_id=ready.project_id,
        sha256=hashlib.sha256(b"pending").hexdigest(),
        size_bytes=7,
        mime_type="application/octet-stream",
        file_name="pending.dat",
        created_at=created,
        expires_at=created + timedelta(days=90),
        state="pending",
    )
    pending.storage_ref = {
        **ready.storage_ref,
        "object_key": storage.key_for(pending.annotation_id, pending.id),
    }
    ctx.db.add(pending)
    await ctx.db.commit()
    if has_bytes:
        ctx.objects.data[
            pending.storage_ref["bucket"], pending.storage_ref["object_key"]
        ] = b"pending"
    before = dict(ctx.objects.data)
    response = await clean(ctx, world)
    assert (
        response.status_code == 409
        and response.json()["detail"]["code"] == "e2e_seed_attachment_pending"
    )
    assert ctx.objects.data == before and not ctx.objects.deleted
    assert (await present(ctx, ready)).state == "ready"
    assert await ctx.db.get(Project, uuid.UUID(world["project_id"])) is not None


@pytest.mark.parametrize("purpose", ["read", "write", "delete", "seed_delete"])
async def test_durable_operation_blocks_fixture_destroy_then_exact_settlement_allows_retry(
    context, purpose
):
    ctx = context
    world = await build(ctx)
    row, _, _ = await upload(ctx, world)
    label = "comment_attachment_" + purpose + "_" + row.id.hex
    permit = await storage_access.acquire(
        ctx.db, uuid.UUID(row.storage_ref["location_id"]), label
    )
    ctx.operations.add(permit.id)
    try:
        response = await clean(ctx, world)
        assert response.status_code == 409
        assert not ctx.objects.deleted and (await present(ctx, row)).state == "ready"
    finally:
        await permit.__aexit__(None, None, None)
    assert (await clean(ctx, world)).status_code == 200


async def test_saved_bucket_trap_and_unknown_canonical_are_never_prefix_deleted(
    context,
):
    ctx = context
    world = await build(ctx)
    row, value, _ = await upload(ctx, world)
    saved = dict(row.storage_ref)
    target = {
        **saved,
        "bucket": ctx.objects.export_bucket,
        "storage_identity": storage.managed_storage_identity(
            saved["endpoint_url"], ctx.objects.export_bucket
        ),
    }
    from sqlalchemy import text

    await ctx.db.execute(text("SET LOCAL aap.restore_training_storage='on'"))
    row.storage_ref = target
    await ctx.db.commit()
    ctx.objects.data[target["bucket"], target["object_key"]] = b"generic\x00bytes"
    ctx.objects.data[saved["bucket"], saved["object_key"]] = b"current bucket trap"
    response = await clean(ctx, world)
    assert response.status_code == 409
    assert (
        ctx.objects.data[saved["bucket"], saved["object_key"]] == b"current bucket trap"
    )
    assert (target["bucket"], target["object_key"]) not in ctx.objects.data
    assert (await present(ctx, row)).state == "retired"
    unknown = f"comment-attachments/{world['annotation_id']}/{uuid.uuid4()}"
    ctx.objects.data[saved["bucket"], unknown] = b"unknown canonical"
    del ctx.objects.data[saved["bucket"], saved["object_key"]]
    assert (await clean(ctx, world)).status_code == 409
    assert ctx.objects.data[saved["bucket"], unknown] == b"unknown canonical"
    del ctx.objects.data[saved["bucket"], unknown]
    assert (await clean(ctx, world)).status_code == 200


async def test_delete_failure_keeps_owner_original_json_and_retry_anchor(context):
    ctx = context
    world = await build(ctx)
    row, value, comment_id = await upload(ctx, world)
    before = storage.snapshot(row)
    ctx.objects.fail_delete = True
    response = await clean(ctx, world)
    assert response.status_code == 503
    current = await present(ctx, row)
    assert current.state == "retired" and storage.same(current, before)
    original = await ctx.db.get(AnnotationComment, uuid.UUID(comment_id))
    assert original.attachments == [value]
    assert await ctx.db.get(Project, uuid.UUID(world["project_id"])) is not None
    ctx.objects.fail_delete = False
    assert (await clean(ctx, world)).status_code == 200


@pytest.mark.parametrize("state", ["pending", "ready"])
async def test_new_owner_during_network_rejects_final_scope_instead_of_escaping(
    context, monkeypatch, state
):
    ctx = context
    world = await build(ctx)
    row, _, _ = await upload(ctx, world)
    real_storage_cleanup = seed._cleanup_fixture_storage

    async def inject():
        created = datetime.now(timezone.utc)
        async with async_sessionmaker(ctx.engine, expire_on_commit=False)() as other:
            new = Upload(
                id=uuid.uuid4(),
                owner_id=row.owner_id,
                annotation_id=row.annotation_id,
                task_id=row.task_id,
                project_id=row.project_id,
                sha256=hashlib.sha256(b"").hexdigest(),
                size_bytes=0,
                mime_type="application/octet-stream",
                file_name="late.dat",
                state=state,
                created_at=created,
                expires_at=created + timedelta(days=90),
            )
            new.storage_ref = {
                **row.storage_ref,
                "object_key": storage.key_for(new.annotation_id, new.id),
            }
            other.add(new)
            await other.commit()

    loop = asyncio.get_running_loop()

    def storage_cleanup(*args):
        real_storage_cleanup(*args)
        asyncio.run_coroutine_threadsafe(inject(), loop).result(timeout=10)

    monkeypatch.setattr(seed, "_cleanup_fixture_storage", storage_cleanup)
    response = await clean(ctx, world)
    assert response.status_code == 409
    assert (await present(ctx, row)).state == "retired"
    assert await ctx.db.get(Project, uuid.UUID(world["project_id"])) is not None


async def test_neighbour_read_permit_does_not_block_exact_owner_cleanup(context):
    ctx = context
    a, b = await build(ctx), await build(ctx)
    await upload(ctx, a)
    neighbour, _, _ = await upload(ctx, b)
    permit = await storage_access.acquire(
        ctx.db,
        uuid.UUID(neighbour.storage_ref["location_id"]),
        uploads.read_purpose(neighbour.id),
    )
    ctx.operations.add(permit.id)
    try:
        assert (await clean(ctx, a)).status_code == 200
        assert (await present(ctx, neighbour)).state == "ready"
    finally:
        await permit.__aexit__(None, None, None)


async def test_maintenance_refusal_preserves_retired_owner_and_fixture(context):
    from app.db.models.storage_maintenance import StorageMaintenanceGate

    ctx = context
    world = await build(ctx)
    row, _, _ = await upload(ctx, world)
    gate = await ctx.db.get(
        StorageMaintenanceGate, uuid.UUID(row.storage_ref["location_id"])
    )
    gate.state, gate.revision = "stopped", gate.revision + 1
    await ctx.db.commit()
    try:
        response = await clean(ctx, world)
        assert response.status_code == 409 and not ctx.objects.deleted
        assert (await present(ctx, row)).state == "retired"
        assert await ctx.db.get(Project, uuid.UUID(world["project_id"])) is not None
    finally:
        await ctx.db.rollback()
        gate = await ctx.db.get(
            StorageMaintenanceGate, uuid.UUID(row.storage_ref["location_id"])
        )
        gate.state, gate.revision = "active", gate.revision + 1
        await ctx.db.commit()
    assert (await clean(ctx, world)).status_code == 200


async def test_network_and_rebuild_never_hold_cleanup_business_locks(
    context, monkeypatch
):
    ctx = context
    world = await build(ctx)
    await upload(ctx, world)
    loop = asyncio.get_running_loop()
    probes = []
    scope = await seed._resolve_fixture_scope(
        ctx.db, seed._parse_owned_namespace(world["namespace"])
    )
    await ctx.db.commit()

    async def observe():
        from sqlalchemy import text

        async with ctx.engine.begin() as connection:
            for table, ids in (
                ("users", scope.user_ids),
                ("projects", scope.project_ids),
            ):
                await connection.execute(
                    text(
                        f"SELECT id FROM {table} WHERE id = ANY(:ids) FOR UPDATE NOWAIT"
                    ),
                    {"ids": list(ids)},
                )
            for table in ("annotation_comments", "comment_attachment_uploads"):
                await connection.execute(
                    text(
                        f"SELECT id FROM {table} WHERE project_id = :pid FOR UPDATE NOWAIT"
                    ),
                    {"pid": world["project_id"]},
                )
        probes.append(True)

    def probe():
        assert not ctx.db.in_transaction()
        asyncio.run_coroutine_threadsafe(observe(), loop).result(timeout=10)

    ctx.objects.probe = probe
    assert (await clean(ctx, world)).status_code == 200
    ctx.objects.probe = None
    real_builder = seed._build_image_workbench_fixture

    async def builder(db, ids):
        assert not db.in_transaction()
        return await real_builder(db, ids)

    monkeypatch.setattr(seed, "_build_image_workbench_fixture", builder)
    result = await ctx.http.post(
        "/api/v1/__test/seed/owned", json={"namespace": world["namespace"]}
    )
    assert result.status_code == 200, result.text
    assert probes


async def test_real_open_body_and_unknown_close_keep_durable_read_responsibility(
    context,
):
    ctx = context
    world = await build(ctx)
    row, _, _ = await upload(ctx, world)
    opened, release = threading.Event(), threading.Event()

    class Body(io.BytesIO):
        def read(self, size=-1):
            opened.set()
            assert release.wait(10)
            return super().read(size)

        def close(self):
            raise OSError("unknown physical close")

    ctx.objects.body_factory = Body
    purpose = uploads.read_purpose(row.id)
    task = asyncio.create_task(
        storage_access.run(
            ctx.db,
            uuid.UUID(row.storage_ref["location_id"]),
            purpose,
            storage.read,
            ctx.objects.client,
            storage.snapshot(row),
        )
    )
    try:
        assert await asyncio.to_thread(opened.wait, 10)
        assert (await clean(ctx, world)).status_code == 409
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        assert not task.done() and not ctx.objects.deleted
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        # First assert uncertainty remains. Cleanup cannot infer a closed body.
        assert (await clean(ctx, world)).status_code == 409
        for body in ctx.objects.bodies:
            io.BytesIO.close(body)
        async with ctx.engine.begin() as connection:
            identifiers = list(
                await connection.scalars(
                    select(StorageOperation.id).where(
                        StorageOperation.location_id
                        == uuid.UUID(row.storage_ref["location_id"]),
                        StorageOperation.purpose == purpose,
                    )
                )
            )
            assert identifiers
            ctx.operations.update(identifiers)
            await connection.execute(
                delete(StorageOperation).where(StorageOperation.id.in_(identifiers))
            )
    assert (await clean(ctx, world)).status_code == 200
