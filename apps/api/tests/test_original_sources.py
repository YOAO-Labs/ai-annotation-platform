"""Source resolution against real, isolated rows without object-store I/O."""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
import hashlib
import uuid

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.db.models.dataset import DatasetItem
from app.db.models.dataset_asset import (
    DatasetAsset,
    DatasetAssetLocation,
    StorageBinding,
    StorageLocation,
)
from app.db.models.storage_maintenance import StorageMaintenanceGate
from app.db.models.task import Task
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.services import original_sources as sources, task_originals
from app.services.content_snapshot import ContentError
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.reference_keys import reference_key
from app.services.storage import storage_service
from tests.test_task_originals import (
    original_client as original_client,
    original_db as original_db,
    verified,
)


async def dataset_rows(db, *, file_type="image", bucket="saved-original-bucket"):
    item = DatasetItem(
        id=uuid.uuid4(),
        dataset_id=uuid.uuid4(),
        file_name="original.bin",
        file_path="originals/" + uuid.uuid4().hex,
        file_type=file_type,
        file_size=999,
        content_hash="a" * 32,
        width=24,
        height=16,
    )
    db.add(item)
    await db.flush()
    asset = DatasetAsset(
        id=uuid.uuid4(),
        dataset_item_id=item.id,
        media_type=file_type,
        verification_state="verified",
        checksum_algorithm="sha256",
        checksum_value=hashlib.sha256(b"saved").hexdigest(),
        size_bytes=5,
    )
    db.add(asset)
    await db.flush()
    replica = DatasetAssetLocation(
        asset_id=asset.id,
        location_id=LEGACY_LOCATION_ID,
        bucket=bucket,
        object_key=item.file_path,
        role="managed_original",
        reference_state="present",
        etag="historical-etag",
        verified_at=datetime.now(timezone.utc),
    )
    task = Task(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        display_id="src-" + uuid.uuid4().hex[:20],
        file_name=item.file_name,
        file_path=item.file_path,
        file_type=file_type,
        dataset_item_id=item.id,
    )
    db.add_all([replica, task])
    await db.flush()
    return item, asset, replica, task


async def add_link(db, task_id, item_id, *, role="primary_lidar"):
    link = TaskDatasetItemLink(
        id=uuid.uuid4(),
        task_id=task_id,
        dataset_item_id=item_id,
        role=role,
        sensor_name="lidar" if role == "primary_lidar" else "camera",
    )
    db.add(link)
    await db.flush()
    return link


@pytest.mark.parametrize("lock", [False, True])
async def test_verified_history_uses_saved_bucket_without_creation_or_bindings(
    original_db, monkeypatch, lock
):
    db = original_db
    item, asset, replica, task = await dataset_rows(db)
    await db.execute(delete(StorageBinding))
    await db.commit()
    monkeypatch.setattr(storage_service, "datasets_bucket", "same-key-trap")
    monkeypatch.setattr(storage_service, "bucket", "another-trap")

    def forbidden(*args, **kwargs):
        pytest.fail("resolution must not validate current bindings or do physical I/O")

    monkeypatch.setattr(storage_service.client, "get_object", forbidden)
    monkeypatch.setattr(storage_service.client, "head_object", forbidden)
    monkeypatch.setattr(task_originals.creation, "_validate", forbidden)
    result = await sources.resolve_task(db, task.id, lock=lock)
    assert result.owner_kind == "dataset_asset" and result.content_id == asset.id
    assert result.item_id == item.id and result.bucket == replica.bucket
    assert result.sha256 == asset.checksum_value and result.size_bytes == 5
    assert result.size_bytes != item.file_size and result.sha256 != item.content_hash
    assert result.replica_facts.creation_id is None
    assert result.replica_facts.etag == "historical-etag"
    assert result.location_id == LEGACY_LOCATION_ID
    assert result.location_revision == result.maintenance_revision == 1
    assert result.endpoint_url == (
        f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}"
    )
    assert await sources.resolve_item(db, item.id, lock=lock) == replace(
        result, relation=None
    )
    with pytest.raises(FrozenInstanceError):
        result.bucket = "changed"
    with pytest.raises(FrozenInstanceError):
        result.item_facts.file_path = "changed"
    with pytest.raises(FrozenInstanceError):
        result.location_facts.endpoint = "changed"


@pytest.mark.parametrize("file_type", ["image", "video", "point_cloud", "other"])
async def test_task_original_saved_receipt_and_media_survive_current_bucket_changes(
    original_db, original_client, monkeypatch, file_type
):
    db = original_db
    task, allocation, created = await verified(db, original_client)
    task.file_type = file_type
    await task_originals.publish(db, task, allocation, created)
    await db.commit()
    calls = list(original_client.calls)
    monkeypatch.setattr(storage_service, "bucket", "wrong-current-annotations")
    monkeypatch.setattr(storage_service, "datasets_bucket", "wrong-current-datasets")

    def forbidden(*args, **kwargs):
        pytest.fail("a published owner does not revalidate its historical Creation")

    monkeypatch.setattr(task_originals.creation, "_validate", forbidden)
    result = await sources.resolve_task(db, task.id, lock=True)
    assert result.owner_kind == "task_original" and result.item_id is None
    assert result.content_id == task.original_id == created.creation_id
    assert (
        result.bucket == allocation["bucket"]
        and result.object_key == allocation["object_key"]
    )
    assert result.sha256 == created.sha256 and result.size_bytes == created.size_bytes
    assert result.media_type == file_type and original_client.calls == calls
    assert result.ownership_facts.creation_id == created.creation_id
    assert result.relation.original_id == task.original_id
    assert result.relation.links == ()
    with pytest.raises(FrozenInstanceError):
        result.ownership_facts.bucket = "changed"


@pytest.mark.parametrize("direct", [True, False])
async def test_direct_and_primary_same_or_primary_only_are_exact_frozen_relations(
    original_db, direct
):
    db = original_db
    item, _, _, task = await dataset_rows(db, file_type="point_cloud")
    if not direct:
        task.dataset_item_id = None
        await db.flush()
    primary = await add_link(db, task.id, item.id)
    camera = await add_link(db, task.id, uuid.uuid4(), role="camera_front")
    relation = await sources.resolve_task_relation(db, task.id, lock=True)
    assert relation.item_id == relation.primary_item_id == item.id
    assert relation.direct_item_id == (item.id if direct else None)
    assert relation.primary_link_id == primary.id
    assert {link.id for link in relation.links} == {primary.id, camera.id}
    assert {link.item_id for link in relation.links} == {
        item.id,
        camera.dataset_item_id,
    }
    result = await sources.resolve_task(db, task.id, lock=True)
    assert result.relation == relation and result.item_id == item.id
    with pytest.raises(FrozenInstanceError):
        relation.links[0].role = "other"


async def test_different_primary_never_silently_selects_direct_item(original_db):
    db = original_db
    _, _, _, task = await dataset_rows(db)
    await add_link(db, task.id, uuid.uuid4())
    for function in (sources.resolve_task_relation, sources.resolve_task):
        with pytest.raises(ContentError) as error:
            await function(db, task.id, lock=True)
        assert error.value.code == "original_relation_conflict"
        assert error.value.status == 409
        assert await db.scalar(select(1)) == 1


@pytest.mark.parametrize("camera", [False, True])
async def test_no_owner_and_camera_only_are_not_original_relations(original_db, camera):
    db = original_db
    _, _, _, task = await dataset_rows(db)
    task.dataset_item_id = None
    await db.flush()
    if camera:
        await add_link(db, task.id, uuid.uuid4(), role="camera_front")
    with pytest.raises(ContentError) as error:
        await sources.resolve_task(db, task.id)
    assert error.value.code == "no_readable_original" and error.value.status == 409


@pytest.mark.parametrize("state", ["unverified", "missing", "changed"])
async def test_only_present_managed_original_qualifies(original_db, state):
    db = original_db
    item, asset, replica, _ = await dataset_rows(db)
    replica.reference_state = state
    db.add(
        DatasetAssetLocation(
            asset_id=asset.id,
            location_id=LEGACY_LOCATION_ID,
            bucket="cache-only",
            object_key=item.file_path,
            role="original_cache",
            reference_state="present",
        )
    )
    await db.flush()
    with pytest.raises(ContentError) as error:
        await sources.resolve_item(db, item.id, lock=True)
    assert error.value.code == "no_readable_original"


@pytest.mark.parametrize("other_location", [False, True])
async def test_multiple_present_managed_locations_are_ambiguous(
    original_db, other_location
):
    db = original_db
    item, asset, _, _ = await dataset_rows(db)
    location_id = LEGACY_LOCATION_ID
    if other_location:
        location_id = uuid.uuid4()
        db.add(
            StorageLocation(
                id=location_id,
                name="other-" + uuid.uuid4().hex,
                kind="s3",
                config={"endpoint": "different:9000", "use_ssl": False},
            )
        )
        await db.flush()
    db.add(
        DatasetAssetLocation(
            asset_id=asset.id,
            location_id=location_id,
            bucket="second-original",
            object_key=item.file_path,
            role="managed_original",
            reference_state="present",
        )
    )
    await db.flush()
    for lock in (False, True):
        with pytest.raises(ContentError) as error:
            await sources.resolve_item(db, item.id, lock=lock)
        assert error.value.code == "original_location_ambiguous"


async def test_cache_does_not_make_unique_original_ambiguous(original_db):
    db = original_db
    item, asset, replica, _ = await dataset_rows(db)
    db.add(
        DatasetAssetLocation(
            asset_id=asset.id,
            location_id=LEGACY_LOCATION_ID,
            bucket="cache",
            object_key="cache/copied",
            role="original_cache",
            reference_state="present",
        )
    )
    await db.flush()
    assert (await sources.resolve_item(db, item.id)).bucket == replica.bucket


@pytest.mark.parametrize("change", ["disabled", "revision", "endpoint", "kind"])
async def test_platform_location_must_match_actual_configuration(original_db, change):
    db = original_db
    item, _, _, _ = await dataset_rows(db)
    location = await db.get(StorageLocation, LEGACY_LOCATION_ID)
    if change == "disabled":
        location.enabled = False
    elif change == "revision":
        location.config_revision = 2
    elif change == "kind":
        location.kind = "filesystem"
    else:
        location.config = {"endpoint": "another:9000", "use_ssl": False}
    await db.flush()
    with pytest.raises(ContentError) as error:
        await sources.resolve_item(db, item.id)
    assert error.value.code == "location_unavailable"


@pytest.mark.parametrize("change", ["path", "item_type", "task_type"])
async def test_task_item_asset_and_saved_locator_must_match(original_db, change):
    db = original_db
    item, _, replica, task = await dataset_rows(db)
    if change == "path":
        replica.object_key = "different-key"
    elif change == "item_type":
        item.file_type = "video"
    else:
        task.file_type = "video"
    await db.flush()
    with pytest.raises(ContentError) as error:
        await sources.resolve_task(db, task.id, lock=True)
    assert error.value.code == "content_binding_changed"


async def test_unverified_legacy_hash_is_never_adopted(original_db):
    db = original_db
    item, asset, _, task = await dataset_rows(db)
    asset.verification_state = "unverified"
    asset.checksum_algorithm = None
    asset.checksum_value = None
    await db.flush()
    with pytest.raises(ContentError) as error:
        await sources.resolve_task(db, task.id)
    assert error.value.code == "content_unverified"
    assert item.content_hash is not None and asset.checksum_value is None


async def test_reference_source_defers_location_and_gate_to_reference_contract(
    original_db,
):
    db = original_db
    item, asset, replica, task = await dataset_rows(db)
    await db.delete(replica)
    item.file_path = reference_key(item.id, asset.checksum_value)
    task.file_path = item.file_path
    db.add(
        StorageMaintenanceGate(
            location_id=LEGACY_LOCATION_ID, state="stopped", revision=3
        )
    )
    await db.flush()
    result = await sources.resolve_task(db, task.id, lock=True)
    assert result.bucket is None and result.object_key == item.file_path
    assert result.endpoint_url is result.location_id is None
    assert result.location_revision is result.maintenance_revision is None
    assert result.ownership_facts.id == asset.id and result.relation.item_id == item.id


@pytest.mark.parametrize("state", ["draining", "stopped"])
async def test_managed_source_observes_maintenance_without_committing(
    original_db, state
):
    db = original_db
    item, _, _, _ = await dataset_rows(db)
    db.add(
        StorageMaintenanceGate(location_id=LEGACY_LOCATION_ID, state=state, revision=4)
    )
    await db.flush()
    with pytest.raises(ContentError) as error:
        await sources.resolve_item(db, item.id, lock=True)
    assert error.value.code == "storage_maintenance"
    assert await db.scalar(select(1)) == 1


async def test_resolver_neither_commits_nor_flushes_pending_writes(
    original_db, monkeypatch
):
    db = original_db
    item, _, _, task = await dataset_rows(db)
    task_id, item_id = task.id, item.id
    await db.commit()
    pending = DatasetItem(
        id=uuid.uuid4(), dataset_id=uuid.uuid4(), file_name="invalid-pending"
    )
    db.add(pending)  # A flush would violate the required file_path column.

    async def forbidden():
        pytest.fail("source helpers must not commit the caller transaction")

    monkeypatch.setattr(db, "commit", forbidden)
    for lock in (False, True):
        assert (await sources.resolve_task(db, task_id, lock=lock)).item_id == item_id
        assert pending in db.new
    db.expunge(pending)
    await db.execute(
        update(DatasetItem).where(DatasetItem.id == item_id).values(width=77)
    )
    await db.rollback()
    assert (await db.get(DatasetItem, item_id)).width == 24


@pytest.mark.parametrize(
    "target", ["task", "link", "item", "asset", "location", "replica", "gate"]
)
async def test_nowait_conflict_recovers_savepoint_and_preserves_caller_work(
    original_db, target
):
    db = original_db
    item, asset, replica, task = await dataset_rows(db)
    link = await add_link(db, task.id, item.id)
    db.add(
        StorageMaintenanceGate(
            location_id=LEGACY_LOCATION_ID, state="active", revision=1
        )
    )
    await db.commit()
    task_id, item_id = task.id, item.id
    statements = {
        "task": select(Task).where(Task.id == task_id),
        "link": select(TaskDatasetItemLink).where(TaskDatasetItemLink.id == link.id),
        "item": select(DatasetItem).where(DatasetItem.id == item_id),
        "asset": select(DatasetAsset).where(DatasetAsset.id == asset.id),
        "location": select(StorageLocation).where(
            StorageLocation.id == LEGACY_LOCATION_ID
        ),
        "replica": select(DatasetAssetLocation).where(
            DatasetAssetLocation.asset_id == asset.id,
            DatasetAssetLocation.bucket == replica.bucket,
        ),
        "gate": select(StorageMaintenanceGate).where(
            StorageMaintenanceGate.location_id == LEGACY_LOCATION_ID
        ),
    }
    pending = DatasetItem(
        id=uuid.uuid4(), dataset_id=uuid.uuid4(), file_name="not-flushed"
    )
    db.add(pending)
    async with async_sessionmaker(db.bind, expire_on_commit=False)() as blocker:
        await blocker.scalar(statements[target].with_for_update(nowait=True))
        try:
            with pytest.raises(ContentError) as error:
                await sources.resolve_task(db, task_id, lock=True)
            assert error.value.code == "storage_original_busy"
            assert pending in db.new
            with db.no_autoflush:
                assert await db.scalar(text("SELECT 1")) == 1
            db.expunge(pending)
        finally:
            await blocker.rollback()
    await db.execute(
        update(DatasetItem).where(DatasetItem.id == item_id).values(height=31)
    )
    await db.commit()
    assert (await sources.resolve_task(db, task_id)).item_facts.height == 31


async def test_unregistered_external_location_is_not_a_platform_original(original_db):
    db = original_db
    item, asset, replica, _ = await dataset_rows(db)
    await db.delete(replica)
    location_id = uuid.uuid4()
    db.add(
        StorageLocation(
            id=location_id,
            name="external-" + uuid.uuid4().hex,
            kind="s3",
            config={
                "endpoint": settings.minio_endpoint,
                "use_ssl": settings.minio_use_ssl,
            },
        )
    )
    await db.flush()
    db.add(
        DatasetAssetLocation(
            asset_id=asset.id,
            location_id=location_id,
            bucket="saved-bucket",
            object_key=item.file_path,
            role="managed_original",
            reference_state="present",
        )
    )
    await db.flush()
    with pytest.raises(ContentError) as error:
        await sources.resolve_item(db, item.id, lock=True)
    assert error.value.code == "location_unavailable"


async def test_missing_task_is_explicit_and_keeps_transaction_usable(original_db):
    with pytest.raises(ContentError) as error:
        await sources.resolve_task_relation(original_db, uuid.uuid4(), lock=True)
    assert error.value.code == "content_task_missing" and error.value.status == 404
    assert await original_db.scalar(select(1)) == 1


@pytest.mark.parametrize("primary_missing", [False, True])
async def test_camera_uses_exact_link_without_borrowing_primary_path(
    original_db, primary_missing
):
    db = original_db
    item, asset, _, task = await dataset_rows(db)
    if primary_missing:
        task.dataset_item_id = None
        task.file_path = "unknown-primary.pcd"
        task.file_type = "point_cloud"
        await db.flush()
    link = await add_link(db, task.id, item.id, role="camera_front")
    result = await sources.resolve_task_link(db, task.id, link.id, lock=True)
    assert result.item_id == item.id and result.content_id == asset.id
    assert result.relation.item_id == (None if primary_missing else item.id)
    assert result.relation.links[0].id == link.id
    if primary_missing:
        assert result.object_key != result.relation.file_path
        with pytest.raises(ContentError, match="no_readable_original"):
            await sources.resolve_task(db, task.id)


@pytest.mark.parametrize("role", ["primary_lidar", "camera_"])
async def test_camera_role_is_not_just_an_arbitrary_task_link(original_db, role):
    db = original_db
    item, _, _, task = await dataset_rows(db)
    link = await add_link(db, task.id, item.id, role=role)
    with pytest.raises(ContentError, match="original_link_invalid"):
        await sources.resolve_task_link(db, task.id, link.id, lock=True)


async def test_camera_link_must_belong_to_exact_task_and_name_an_image(original_db):
    db = original_db
    item, _, _, task = await dataset_rows(db, file_type="video")
    link = await add_link(db, task.id, item.id, role="camera_front")
    with pytest.raises(ContentError, match="camera_original_not_image"):
        await sources.resolve_task_link(db, task.id, link.id)
    _, _, _, other_task = await dataset_rows(db)
    with pytest.raises(ContentError) as error:
        await sources.resolve_task_link(db, other_task.id, link.id, lock=True)
    assert error.value.code == "content_link_missing" and error.value.status == 404


async def test_camera_keeps_primary_conflict_and_all_link_snapshot_checks(original_db):
    db = original_db
    item, _, _, task = await dataset_rows(db)
    selected = await add_link(db, task.id, item.id, role="camera_front")
    neighbor = await add_link(db, task.id, item.id, role="camera_back")
    before = await sources.resolve_task_link(db, task.id, selected.id)
    neighbor.sensor_name = "changed-neighbor"
    await db.flush()
    assert await sources.resolve_task_link(db, task.id, selected.id) != before
    await add_link(db, task.id, uuid.uuid4())
    with pytest.raises(ContentError, match="original_relation_conflict"):
        await sources.resolve_task_link(db, task.id, selected.id, lock=True)
