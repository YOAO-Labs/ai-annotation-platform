"""Real snapshots, persistent ownership, derivative isolation and lifecycle fences."""

import hashlib

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.db.models.dataset import DatasetItem
from app.db.models.dataset_asset import DatasetAsset, DatasetAssetLocation
from app.db.models.dataset_source import DatasetSourceItem, DatasetSourcePreview
from app.schemas.dataset_source import SourceLifecycleChange
from app.services import directory_previews as previews
from app.services import dataset_source_lifecycle as lifecycle
from app.services import dataset_registration as registration
from app.services.content_snapshot import ContentError
from app.services.dataset_references import register_entry, reference_snapshot
from app.services.dataset_content import stored_content
from app.services.storage import storage_service
from tests.test_dataset_content import content_world as content_world
from tests.test_dataset_registration import start, drain, begin_confirmed


@pytest.fixture
def object_store(monkeypatch):
    from app.services import managed_original_creation as creations
    from tests.fakes.managed_creation_storage import CreationStorage

    fake = CreationStorage(keyed_by_bucket=True)
    monkeypatch.setattr(storage_service, "client", fake)
    monkeypatch.setattr(creations, "create_client", lambda **_: fake)
    monkeypatch.setattr(
        storage_service,
        "delete_object",
        lambda key, *, bucket: fake.objects.pop((bucket, key), None),
    )
    return fake.objects


@pytest.mark.parametrize("ownership", ["reference", "managed_copy"])
@pytest.mark.parametrize("cache", ["none", "preview", "on_demand"])
async def test_six_policies_and_source_outage(
    db_session, content_world, object_store, ownership, cache
):
    db = db_session
    root, data, actor, dataset, location, source, scan, entry = content_world
    source.ownership_mode, source.cache_policy = ownership, cache
    if cache == "on_demand":
        location.config = {
            **location.config,
            "cache_policy": "on_demand",
            "original_cache_quota_bytes": len(data) * 2,
        }
    await db.commit()
    run, task_id, fence = await start(db, content_world)
    await drain(db, run, task_id, fence)
    assert run.cache_policy == cache and run.ownership_mode == ownership
    assert run.counts["preview_estimated_count"] == 1
    assert (run.counts["preview_estimated_bytes"] > 0) == (cache != "none")
    assert run.counts["original_estimated_bytes"] == (
        len(data) if ownership == "managed_copy" else 0
    )
    task_id, fence = await begin_confirmed(db, run, content_world)
    await drain(db, run, task_id, fence)
    mapping = await db.get(DatasetSourceItem, (source.id, entry.path_digest))
    item = await db.get(DatasetItem, mapping.dataset_item_id)
    asset = await db.get(DatasetAsset, mapping.asset_id)
    assert asset.checksum_value == hashlib.sha256(data).hexdigest()
    assert item.thumbnail_path is None and item.blurhash is None
    replicas = list(await db.scalars(select(DatasetAssetLocation)))
    assert len(replicas) == int(ownership == "managed_copy")
    assert run.counts["original_copy_bytes"] == (
        len(data) if ownership == "managed_copy" else 0
    )
    preview = await db.get(DatasetSourcePreview, (source.id, entry.path_digest))
    assert bool(preview) == (cache != "none")
    if preview:
        assert preview.state == "ready"
        body = object_store[preview.bucket, preview.object_key]
        assert (
            preview.size_bytes == len(body)
            and preview.sha256 == hashlib.sha256(body).hexdigest()
        )
        assert run.counts["preview_retained_bytes"] == len(body)
    result = await register_entry(
        db, scan.id, entry.path_digest, actor, node_id="worker:content@test"
    )
    assert not result["created"] and result["item_id"] == item.id
    assert len(object_store) == int(ownership == "managed_copy") + int(cache != "none")
    before = (root / entry.relative_path).read_bytes()
    (root / entry.relative_path).unlink()
    if cache != "none":
        snapshot = await previews.read_preview(db, source.id, entry.path_digest, actor)
        snapshot.close()
    if ownership == "reference":
        with pytest.raises((ContentError, OSError, ValueError)):
            await reference_snapshot(db, item.id, actor)
        await db.rollback()
        await db.refresh(actor)
        await db.refresh(source)
    else:
        replica = replicas[0]
        with stored_content(replica.bucket, replica.object_key, len(data)) as (body, _):
            assert body.read() == before
    observed = await lifecycle.impact(db, source, actor)
    assert observed["item_count"] == 1 and observed["preview_count"] == int(
        cache != "none"
    )
    await lifecycle.change(
        db,
        source.id,
        SourceLifecycleChange(
            action="unregister",
            expected_revision=source.config_revision,
            impact_proof=observed["proof"],
        ),
        actor,
    )
    await db.commit()
    assert source.unregistered_at and not source.enabled
    assert await db.scalar(select(func.count()).select_from(DatasetItem)) == 1
    assert len(object_store) == int(ownership == "managed_copy") + int(cache != "none")


async def test_location_ceiling_and_lease_cancel(
    db_session, content_world, object_store
):
    db = db_session
    _, _, actor, _, location, source, _, entry = content_world
    source.cache_policy = "preview"
    location.config = {**location.config, "cache_policy": "none"}
    await db.commit()
    run, task_id, fence = await start(db, content_world)
    assert run.cache_policy == "none"
    impact = await lifecycle.impact(db, source, actor)
    assert impact["active_attempt_count"] == 1
    await lifecycle.change(
        db,
        source.id,
        SourceLifecycleChange(
            action="pause",
            expected_revision=source.config_revision,
            impact_proof=impact["proof"],
        ),
        actor,
    )
    await db.commit()
    assert run.status == "cancelled" and run.lease_token is None
    with pytest.raises(registration.LostRegistrationLease):
        await registration.next_entry(db, run.id, task_id, fence)
    await db.rollback()
    assert not object_store
    await db.refresh(actor)
    await db.refresh(source)
    await db.refresh(run)
    observed = await lifecycle.impact(db, source, actor)
    await lifecycle.change(
        db,
        source.id,
        SourceLifecycleChange(
            action="enable",
            expected_revision=source.config_revision,
            impact_proof=observed["proof"],
        ),
        actor,
    )
    await db.commit()
    await db.refresh(source)
    assert source.enabled and source.unregistered_at is None
    with pytest.raises(Exception, match="configuration_changed"):
        await registration.resume(db, run.id, actor)
    await db.rollback()


async def test_preview_failure_does_not_undo_original(
    db_session, content_world, object_store, monkeypatch
):
    db = db_session
    _, _, actor, _, _, source, scan, entry = content_world
    source.cache_policy = "preview"
    await db.commit()

    async def failed(_):
        raise OSError("encoder unavailable")

    monkeypatch.setattr(previews, "encode", failed)
    run, task_id, fence = await start(db, content_world)
    await drain(db, run, task_id, fence)
    assert (
        run.counts["ready_count"] == 1
        and run.counts["preview_estimate_error_count"] == 1
    )
    task_id, fence = await begin_confirmed(db, run, content_world)
    await drain(db, run, task_id, fence)
    assert (
        run.counts["registered_count"] == 1 and run.counts["preview_failed_count"] == 1
    )
    row = await db.get(DatasetSourcePreview, (source.id, entry.path_digest))
    assert row.state == "failed" and row.size_bytes is None
    assert not object_store
    assert await db.scalar(select(func.count()).select_from(DatasetItem)) == 1


async def test_stale_impact_is_rejected_and_source_tree_is_untouched(
    db_session, content_world, object_store
):
    db = db_session
    root, original, actor, _, _, source, scan, entry = content_world
    before = await lifecycle.impact(db, source, actor)
    await register_entry(db, scan.id, entry.path_digest, actor)
    with pytest.raises(HTTPException) as exc:
        await lifecycle.change(
            db,
            source.id,
            SourceLifecycleChange(
                action="unregister",
                expected_revision=source.config_revision,
                impact_proof=before["proof"],
            ),
            actor,
        )
    assert exc.value.status_code == 409
    await db.rollback()
    await db.refresh(entry)
    assert (root / entry.relative_path).read_bytes() == original
    await db.refresh(source)
    assert source.enabled and source.unregistered_at is None


@pytest.mark.parametrize("stage", ["original", "preview"])
async def test_native_cancellation_waits_for_owned_put_before_cleanup(
    db_session, content_world, object_store, monkeypatch, stage
):
    import asyncio
    import threading

    db = db_session
    _, _, actor, dataset, _, source, scan, entry = content_world
    dataset_id = dataset.id
    source.ownership_mode = "managed_copy" if stage == "original" else "reference"
    source.cache_policy = "none" if stage == "original" else "preview"
    await db.commit()
    entered, release = threading.Event(), threading.Event()
    method = "complete_multipart_upload" if stage == "original" else "put_object"
    put = getattr(storage_service.client, method)
    file_open_at_finish = []
    original_streams = []
    if stage == "original":
        from app.services import managed_original_creation as creations

        create_stream = creations.create_stream

        async def retain_source(db, allocation, stream, **kwargs):
            original_streams.append(stream)
            return await create_stream(db, allocation, stream, **kwargs)

        monkeypatch.setattr(creations, "create_stream", retain_source)

    def blocking_put(**kwargs):
        entered.set()
        assert release.wait(5)
        body = original_streams[0] if original_streams else kwargs["Body"]
        file_open_at_finish.append(not body.closed if hasattr(body, "closed") else True)
        return put(**kwargs)

    monkeypatch.setattr(storage_service.client, method, blocking_put)
    task = asyncio.create_task(
        register_entry(
            db, scan.id, entry.path_digest, actor, node_id="worker:content@test"
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await db.rollback()
    assert file_open_at_finish == [True]
    if stage == "preview":
        assert object_store == {}
    else:
        from app.db.models.managed_original_creation import ManagedOriginalCreation

        creation = await db.scalar(
            select(ManagedOriginalCreation).where(
                ManagedOriginalCreation.dataset_id == dataset_id
            )
        )
        assert creation.status == "verified"
        assert len(object_store) == 1
        assert (creation.bucket, creation.object_key) in object_store
        assert not await db.scalar(select(DatasetItem.id))


async def test_missing_preview_regenerates_and_delete_cleans_exact_keys(
    db_session, content_world, object_store
):
    from app.services.dataset import DatasetService

    db = db_session
    _, _, actor, dataset, _, source, scan, entry = content_world
    source.cache_policy = "preview"
    await db.commit()
    first = await register_entry(
        db, scan.id, entry.path_digest, actor, node_id="worker:content@test"
    )
    row = await db.get(DatasetSourcePreview, (source.id, entry.path_digest))
    old_key = row.object_key
    object_store.clear()
    second = await register_entry(
        db, scan.id, entry.path_digest, actor, node_id="worker:content@test"
    )
    assert first["item_id"] == second["item_id"]
    assert row.state == "ready" and row.object_key != old_key and row.generation == 2
    object_store[(storage_service.media_cache_bucket, "other-owner.webp")] = b"preserve"
    service = DatasetService(db)
    assert await service.delete_item(first["item_id"], dataset_id=dataset.id)
    await db.commit()
    await service.cleanup_directory_previews()
    assert object_store == {
        (storage_service.media_cache_bucket, "other-owner.webp"): b"preserve"
    }


async def test_directory_media_never_uses_legacy_urls_or_pyramids(
    db_session, content_world, object_store
):
    from app.services.dataset import DatasetService
    from app.services.image_pyramid import load_owner, ImagePyramidError
    from app.services.directory_policy import legacy_media_column

    db = db_session
    _, _, actor, dataset, _, source, scan, entry = content_world
    source.ownership_mode = "managed_copy"
    await db.commit()
    result = await register_entry(
        db, scan.id, entry.path_digest, actor, node_id="worker:content@test"
    )
    item = await db.get(DatasetItem, result["item_id"])
    item.thumbnail_path = "thumbnails/stale.webp"
    item.blurhash = "stale"
    await db.commit()
    rows, _ = await DatasetService(db).list_items(dataset.id)
    assert rows[0]["thumbnail_url"] is None and rows[0]["blurhash"] is None
    row = (
        await db.execute(
            select(
                legacy_media_column(DatasetItem.thumbnail_path),
                legacy_media_column(DatasetItem.blurhash),
            ).where(DatasetItem.id == item.id)
        )
    ).one()
    assert row == (None, None)
    with pytest.raises(ImagePyramidError, match="directory_cache_not_supported"):
        await load_owner(db, "dataset_item", item.id)


async def test_failed_replacement_keeps_published_preview(
    db_session, content_world, object_store, monkeypatch
):
    db = db_session
    _, _, actor, _, _, source, scan, entry = content_world
    source.cache_policy = "preview"
    await db.commit()
    await register_entry(
        db, scan.id, entry.path_digest, actor, node_id="worker:content@test"
    )
    row = await db.get(DatasetSourcePreview, (source.id, entry.path_digest))
    identity = (source.id, entry.path_digest)
    old = (row.bucket, row.object_key)
    original = dict(object_store)
    monkeypatch.setattr(previews, "cached_bytes_match", lambda _: False)
    calls = 0

    async def revoked(_):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise HTTPException(403, "revoked after replacement PUT")

    with pytest.raises(HTTPException):
        await register_entry(
            db,
            scan.id,
            entry.path_digest,
            actor,
            node_id="worker:content@test",
            before_apply=revoked,
        )
    await db.rollback()
    row = await db.get(DatasetSourcePreview, identity, populate_existing=True)
    assert row.state == "ready" and (row.bucket, row.object_key) == old
    assert object_store == original
