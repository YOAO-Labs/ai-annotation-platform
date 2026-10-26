"""Real seed cleanup preserves independent guide owners and current references."""

import asyncio
from datetime import datetime, timezone
import hashlib
from types import SimpleNamespace
import uuid

import httpx
import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.v1 import _test_seed as seed, _test_seed_attachments as attachments
from app.db.models.annotation_comment import AnnotationComment
from app.db.models.comment_attachment_upload import CommentAttachmentUpload
from app.db.models.guide_asset_upload import GuideAssetUpload as Upload
from app.db.models.project import Project
from app.db.models.project_guide_asset import ProjectGuideAsset as Reference
from app.db.models.storage_maintenance import StorageOperation
from app.db.models.user import User
from app.deps import get_db
from app.services import comment_attachment_storage, comment_attachment_uploads
from app.services import guide_asset_references as references
from app.services import guide_asset_storage as storage, guide_asset_uploads as uploads
from app.services import storage as storage_module, storage_access
from app.services.project_delete import delete_project_records
from tests._owned_s3 import patch_owned_client
from tests.test_seed_comment_attachments import Objects, build as build_comments
from tests.test_seed_comment_attachments import upload as upload_comment


@pytest.fixture
async def context(test_engine, app_module, monkeypatch):
    """Commit real SQL, fake only provider bytes, and clean every owned UUID."""
    db = async_sessionmaker(test_engine, expire_on_commit=False)()
    objects = Objects(storage_module.storage_service)
    patch_owned_client(monkeypatch, objects.client)
    for module in (
        storage_module,
        storage,
        uploads,
        comment_attachment_storage,
        comment_attachment_uploads,
        attachments,
    ):
        monkeypatch.setattr(module, "storage_service", objects)
    ctx = SimpleNamespace(
        db=db,
        engine=test_engine,
        objects=objects,
        names=[],
        project_ids=set(),
        guide_ids=set(),
        operations=set(),
    )

    async def session():
        yield db

    previous = app_module.dependency_overrides.get(get_db)
    app_module.dependency_overrides[get_db] = session
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_module), base_url="http://test"
        ) as client:
            ctx.http = client
            try:
                yield ctx
            finally:
                objects.probe = None
                await db.rollback()
                # A failed HTTP request may have committed a new pending owner.
                # Select only this test's exact original projects, never a prefix.
                ctx.guide_ids.update(
                    await db.scalars(
                        select(Upload.id).where(Upload.project_id.in_(ctx.project_ids))
                    )
                )
                await db.execute(
                    delete(Reference).where(
                        Reference.upload_id.in_(ctx.guide_ids)
                        | Reference.project_id.in_(ctx.project_ids)
                    )
                )
                await db.execute(delete(Upload).where(Upload.id.in_(ctx.guide_ids)))
                purposes = [
                    uploads.purpose(kind, identifier)
                    for identifier in ctx.guide_ids
                    for kind in ("read", "write", "delete")
                ]
                await db.execute(
                    delete(StorageOperation).where(
                        StorageOperation.id.in_(ctx.operations)
                        | StorageOperation.purpose.in_(purposes)
                    )
                )
                await db.commit()
                for name in reversed(ctx.names):
                    owned = seed._parse_owned_namespace(name)
                    scope = await seed._resolve_fixture_scope(db, owned)
                    comments = list(
                        await db.scalars(
                            select(CommentAttachmentUpload).where(
                                CommentAttachmentUpload.project_id.in_(
                                    scope.project_ids
                                )
                            )
                        )
                    )
                    for comment in comments:
                        saved = comment_attachment_storage.resolve(comment)
                        objects.data.pop((saved.bucket, saved.object_key), None)
                    await db.execute(
                        delete(CommentAttachmentUpload).where(
                            CommentAttachmentUpload.project_id.in_(scope.project_ids)
                        )
                    )
                    await db.commit()
                    await seed._cleanup_e2e_fixtures(db, owned)
                assert (
                    await db.scalar(
                        select(Upload.id).where(Upload.id.in_(ctx.guide_ids)).limit(1)
                    )
                    is None
                )
                assert all(body.closed for body in objects.bodies)
                objects.data.clear()
                assert (
                    await db.scalar(
                        select(Reference.upload_id)
                        .where(Reference.upload_id.in_(ctx.guide_ids))
                        .limit(1)
                    )
                    is None
                )
                assert (
                    await db.scalar(
                        select(StorageOperation.id)
                        .where(
                            StorageOperation.id.in_(ctx.operations)
                            | StorageOperation.purpose.in_(purposes)
                        )
                        .limit(1)
                    )
                    is None
                )
    finally:
        await db.close()
        if previous is None:
            app_module.dependency_overrides.pop(get_db, None)
        else:
            app_module.dependency_overrides[get_db] = previous


async def build(ctx):
    world = await build_comments(ctx)
    ctx.project_ids.add(uuid.UUID(world["project_id"]))
    return world


async def upload(ctx, world, raw=b"<svg>independent guide</svg>"):
    response = await ctx.http.post(
        f"/api/v1/projects/{world['project_id']}/guide-assets/upload",
        params={"file_name": "guide.svg"},
        headers={**world["headers"], "Content-Type": "image/svg+xml"},
        content=raw,
    )
    assert response.status_code == 200, response.text
    value = response.json()
    identifier = uuid.UUID(value["key"].rsplit("/", 1)[1])
    ctx.guide_ids.add(identifier)
    row = await ctx.db.get(Upload, identifier, populate_existing=True)
    receipt = storage.snapshot(row)
    await ctx.db.commit()
    return receipt, value, raw


async def pending(ctx, world, *, has_bytes=True):
    raw, identifier = b"pending guide bytes", uuid.uuid4()
    ctx.guide_ids.add(identifier)
    row = Upload(
        id=identifier,
        owner_id=uuid.UUID(world["actor_id"]),
        project_id=uuid.UUID(world["project_id"]),
        sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw),
        mime_type="image/png",
        file_name="pending.png",
        created_at=datetime.now(timezone.utc),
        state="pending",
    )
    row.storage_ref = await storage.allocate(ctx.db, row)
    ctx.db.add(row)
    await ctx.db.commit()
    receipt = storage.snapshot(row)
    if has_bytes:
        owner = storage.resolve(receipt)
        ctx.objects.data[owner.bucket, owner.object_key] = raw
    return receipt


async def clean(ctx, world):
    return await ctx.http.post(
        "/api/v1/__test/seed/owned-cleanup", json={"namespace": world["namespace"]}
    )


async def current(ctx, receipt):
    row = await ctx.db.get(Upload, receipt.id, populate_existing=True)
    await ctx.db.commit()
    return row


async def share(ctx, source, target):
    source_id, target_id = (
        uuid.UUID(source["project_id"]),
        uuid.UUID(target["project_id"]),
    )
    projects = {
        row.id: row
        for row in await ctx.db.scalars(
            select(Project)
            .where(Project.id.in_((source_id, target_id)))
            .order_by(Project.id)
            .with_for_update(nowait=True)
        )
    }
    await references.copy_references(ctx.db, projects[source_id], projects[target_id])
    await ctx.db.commit()


@pytest.mark.parametrize("open_read", [False, True])
async def test_last_ref_cleanup_retires_keeps_owner_defers_bytes_and_gc_can_finish(
    context, open_read
):
    ctx = context
    world = await build(ctx)
    receipt, _, raw = await upload(ctx, world)
    owner = storage.resolve(receipt)
    permit = None
    if open_read:
        permit = await storage_access.acquire(
            ctx.db,
            uuid.UUID(receipt.storage_ref["location_id"]),
            uploads.purpose("read", receipt.id),
        )
        ctx.operations.add(permit.id)
    try:
        result = await clean(ctx, world)
        assert result.status_code == 200, result.text
        assert await ctx.db.get(Project, uuid.UUID(world["project_id"])) is None
        row = await current(ctx, receipt)
        assert row.state == "retired" and row.retired_at is not None
        assert storage.same(row, receipt)
        retired_at = row.retired_at
        assert (
            await ctx.db.scalar(
                select(Reference.upload_id).where(Reference.upload_id == receipt.id)
            )
            is None
        )
        assert ctx.objects.data[owner.bucket, owner.object_key] == raw
        assert (owner.bucket, owner.object_key) not in ctx.objects.deleted
        result = await clean(ctx, world)
        assert result.status_code == 200, result.text
        assert (await current(ctx, receipt)).retired_at == retired_at
        if open_read:
            result = await uploads.cleanup(ctx.db, identifiers=[receipt.id])
            assert result == {"objects": 0, "retired": 0, "retained": 1, "failed": 0}
            assert ctx.objects.data[owner.bucket, owner.object_key] == raw
    finally:
        if permit is not None:
            await permit.__aexit__(None, None, None)
    result = await uploads.cleanup(ctx.db, identifiers=[receipt.id])
    assert result == {"objects": 1, "retired": 0, "retained": 0, "failed": 0}
    assert await current(ctx, receipt) is None
    assert (owner.bucket, owner.object_key) not in ctx.objects.data


async def test_outside_reference_survives_origin_namespace_and_stays_readable(context):
    ctx = context
    source, outside = await build(ctx), await build(ctx)
    receipt, value, raw = await upload(ctx, source)
    await share(ctx, source, outside)
    result = await clean(ctx, source)
    assert result.status_code == 200, result.text
    assert await ctx.db.get(Project, receipt.project_id) is None
    assert await ctx.db.get(User, receipt.owner_id) is None
    assert (await current(ctx, receipt)).state == "ready"
    assert (
        await ctx.db.get(Reference, (uuid.UUID(outside["project_id"]), receipt.id))
        is not None
    )
    result = await ctx.http.get(
        f"/api/v1/projects/{outside['project_id']}/guide-assets/content",
        params={"key": value["key"]},
        headers=outside["headers"],
    )
    assert result.status_code == 200 and result.content == raw
    assert all(body.closed for body in ctx.objects.bodies)
    assert await uploads.cleanup(ctx.db, identifiers=[receipt.id]) == {
        "objects": 0,
        "retired": 0,
        "retained": 1,
        "failed": 0,
    }
    assert (await clean(ctx, outside)).status_code == 200
    assert (await current(ctx, receipt)).state == "retired"


async def test_unknown_legacy_json_never_authorizes_physical_guide_deletion(context):
    ctx = context
    world = await build(ctx)
    receipt, _, raw = await upload(ctx, world)
    legacy_id = uuid.uuid4()
    key = storage.key_for(uuid.UUID(world["project_id"]), legacy_id)
    legacy_bytes = b"unknown canonical guide bytes"
    project = await ctx.db.get(Project, uuid.UUID(world["project_id"]))
    project.guide_assets = [
        *project.guide_assets,
        {
            "key": key,
            "original_name": "legacy.png",
            "content_type": "image/png",
            "size": len(legacy_bytes),
            "uploaded_at": "2026-01-01T00:00:00Z",
        },
    ]
    await ctx.db.commit()
    ctx.objects.data[ctx.objects.bucket, key] = legacy_bytes
    result = await clean(ctx, world)
    assert result.status_code == 200, result.text
    assert await ctx.db.get(Upload, legacy_id) is None
    assert await ctx.db.get(Reference, (receipt.project_id, legacy_id)) is None
    assert ctx.objects.data[ctx.objects.bucket, key] == legacy_bytes
    assert (ctx.objects.bucket, key) not in ctx.objects.deleted
    assert (await current(ctx, receipt)).state == "retired"
    owner = storage.resolve(receipt)
    assert ctx.objects.data[owner.bucket, owner.object_key] == raw
    assert (await clean(ctx, world)).status_code == 200
    assert (await uploads.cleanup(ctx.db, identifiers=[receipt.id]))["objects"] == 1
    assert ctx.objects.data[ctx.objects.bucket, key] == legacy_bytes


async def test_outside_json_without_ref_neither_retains_nor_authorizes_owner(context):
    ctx = context
    source, outside = await build(ctx), await build(ctx)
    receipt, value, raw = await upload(ctx, source)
    project = await ctx.db.get(Project, uuid.UUID(outside["project_id"]))
    project.guide_assets = [value]
    await ctx.db.commit()
    assert await ctx.db.get(Reference, (project.id, receipt.id)) is None
    result = await clean(ctx, source)
    assert result.status_code == 200, result.text
    row = await current(ctx, receipt)
    assert row.state == "retired" and storage.same(row, receipt)
    retired_at = row.retired_at
    owner = storage.resolve(receipt)
    assert ctx.objects.data[owner.bucket, owner.object_key] == raw
    result = await ctx.http.get(
        f"/api/v1/projects/{outside['project_id']}/guide-assets/content",
        params={"key": value["key"]},
        headers=outside["headers"],
    )
    assert result.status_code == 409, result.text
    assert result.json()["detail"]["code"] == "guide_asset_storage_unpinned"
    assert (await clean(ctx, outside)).status_code == 200
    assert (await current(ctx, receipt)).retired_at == retired_at
    assert (await uploads.cleanup(ctx.db, identifiers=[receipt.id]))["objects"] == 1


@pytest.mark.parametrize("has_bytes", [False, True])
async def test_live_pending_preflight_precedes_any_comment_or_legacy_physical_io(
    context, has_bytes
):
    ctx = context
    world = await build(ctx)
    comment, value, comment_id = await upload_comment(ctx, world)
    comment_receipt = comment_attachment_storage.snapshot(comment)
    receipt = await pending(ctx, world, has_bytes=has_bytes)
    before, deleted = dict(ctx.objects.data), list(ctx.objects.deleted)
    result = await clean(ctx, world)
    assert result.status_code == 409, result.text
    assert result.json()["detail"]["code"] == "e2e_seed_guide_asset_pending"
    assert ctx.objects.data == before and ctx.objects.deleted == deleted
    assert (await current(ctx, receipt)).state == "pending"
    original = await ctx.db.get(
        CommentAttachmentUpload, comment_receipt.id, populate_existing=True
    )
    assert original.state == "ready" and comment_attachment_storage.same(
        original, comment_receipt
    )
    comment = await ctx.db.get(AnnotationComment, uuid.UUID(comment_id))
    assert comment.attachments == [value]
    assert await ctx.db.get(Project, receipt.project_id) is not None


async def test_late_pending_final_recheck_rolls_back_guide_unlink_and_keeps_fixture(
    context, monkeypatch
):
    ctx = context
    world = await build(ctx)
    receipt, value, raw = await upload(ctx, world)
    comment, attachment, comment_id = await upload_comment(ctx, world)
    comment_receipt = comment_attachment_storage.snapshot(comment)
    identifier = uuid.uuid4()
    ctx.guide_ids.add(identifier)
    loop = asyncio.get_running_loop()
    real_cleanup = seed._cleanup_fixture_storage

    async def inject():
        async with async_sessionmaker(ctx.engine, expire_on_commit=False)() as other:
            row = Upload(
                id=identifier,
                owner_id=receipt.owner_id,
                project_id=receipt.project_id,
                sha256=hashlib.sha256(b"late").hexdigest(),
                size_bytes=4,
                mime_type="image/png",
                file_name="late.png",
                state="pending",
                created_at=datetime.now(timezone.utc),
                storage_ref={
                    **receipt.storage_ref,
                    "object_key": storage.key_for(receipt.project_id, identifier),
                },
            )
            other.add(row)
            await other.commit()
            ctx.objects.data[
                row.storage_ref["bucket"], row.storage_ref["object_key"]
            ] = b"late"

    def cleanup_then_insert(*args):
        real_cleanup(*args)
        asyncio.run_coroutine_threadsafe(inject(), loop).result(timeout=10)

    with monkeypatch.context() as patch:
        patch.setattr(seed, "_cleanup_fixture_storage", cleanup_then_insert)
        result = await clean(ctx, world)
    assert result.status_code == 409, result.text
    assert result.json()["detail"]["code"] == "e2e_seed_guide_asset_pending"
    row = await current(ctx, receipt)
    assert row.state == "ready" and storage.same(row, receipt)
    assert await ctx.db.get(Reference, (receipt.project_id, receipt.id)) is not None
    assert (await ctx.db.get(Project, receipt.project_id)).guide_assets == [value]
    owner = storage.resolve(receipt)
    assert ctx.objects.data[owner.bucket, owner.object_key] == raw
    late = await ctx.db.get(Upload, identifier, populate_existing=True)
    assert late.state == "pending"
    assert (
        ctx.objects.data[late.storage_ref["bucket"], late.storage_ref["object_key"]]
        == b"late"
    )
    original = await ctx.db.get(
        CommentAttachmentUpload, comment_receipt.id, populate_existing=True
    )
    assert original.state == "retired" and comment_attachment_storage.same(
        original, comment_receipt
    )
    assert (await ctx.db.get(AnnotationComment, uuid.UUID(comment_id))).attachments == [
        attachment
    ]


async def test_missing_origin_pending_is_retained_without_guessing_namespace(context):
    ctx = context
    world = await build(ctx)
    receipt = await pending(ctx, world)
    project = await ctx.db.get(Project, receipt.project_id)
    await delete_project_records(ctx.db, project)
    await ctx.db.commit()
    assert await ctx.db.get(Project, receipt.project_id) is None
    result = await clean(ctx, world)
    assert result.status_code == 200, result.text
    assert await ctx.db.get(User, receipt.owner_id) is None
    row = await current(ctx, receipt)
    assert row.state == "pending" and storage.same(row, receipt)
    owner = storage.resolve(receipt)
    assert ctx.objects.data[owner.bucket, owner.object_key] == b"pending guide bytes"
    assert (owner.bucket, owner.object_key) not in ctx.objects.deleted
    result = await uploads.cleanup(ctx.db, identifiers=[receipt.id])
    assert result == {"objects": 0, "retired": 0, "retained": 1, "failed": 0}
    assert await current(ctx, receipt) is not None
