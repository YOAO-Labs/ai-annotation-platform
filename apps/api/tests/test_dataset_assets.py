from __future__ import annotations

import hashlib
import io
import threading
import uuid

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.dataset_asset import (
    DatasetAsset,
    DatasetAssetLocation,
    StorageBinding,
)
from app.services.dataset import DatasetService
from app.services.dataset_assets import (
    register_item_asset,
    validate_legacy_bindings,
    hash_stored_object,
    find_verified_content,
)
from app.services.dataset_storage_keys import (
    dataset_relative_path,
    dataset_item_relative_path,
    managed_dataset_key,
    managed_dataset_prefix,
)
from app.services.storage import storage_service


from tests.fakes.managed_creation_storage import CreationStorage


class ObjectClient(CreationStorage):
    def __init__(self):
        super().__init__()
        self.read_threads = []

    def get_object(self, **kwargs):
        self.read_threads.append(threading.get_ident())
        return super().get_object(**kwargs)


@pytest.fixture(autouse=True)
def local_creation_client(monkeypatch):
    from app.services import managed_original_creation as creations

    monkeypatch.setattr(creations, "create_client", lambda **_: storage_service.client)


def test_managed_paths_have_unique_identity_and_retain_logical_directories():
    dataset_id = uuid.uuid4()
    first = managed_dataset_key(dataset_id, "scene-A/camera/front/x.jpg")
    second = managed_dataset_key(dataset_id, "scene-A/camera/front/x.jpg")
    assert first != second
    assert first.startswith(managed_dataset_prefix(dataset_id))
    assert dataset_relative_path(first, "renamed-twice") == "scene-A/camera/front/x.jpg"
    assert dataset_relative_path("old/a.jpg", "new") == "old/a.jpg"
    assert (
        dataset_relative_path(managed_dataset_prefix(dataset_id) + "manual/sub/x.jpg")
        == "manual/sub/x.jpg"
    )
    scene_id = uuid.uuid4()
    assert (
        dataset_relative_path(
            managed_dataset_prefix(dataset_id) + f"{scene_id}/lidar/a.pcd"
        )
        == f"{scene_id}/lidar/a.pcd"
    )


@pytest.mark.parametrize("path", ["", "a/../b", "a//b", "./b"])
def test_invalid_managed_path_is_rejected(path):
    with pytest.raises(ValueError):
        managed_dataset_key(uuid.uuid4(), path)


@pytest.mark.asyncio
async def test_unknown_hash_and_placeholder_remain_unverified(db_session, super_admin):
    user, _ = super_admin
    dataset = Dataset(name="renamed", display_id="DS-ASSET", created_by=user.id)
    db_session.add(dataset)
    await db_session.flush()
    item = DatasetItem(
        dataset_id=dataset.id,
        file_name="old.bin",
        file_path="original-name/sub/old.bin",
        file_type="other",
        content_hash="f" * 32,
    )
    db_session.add(item)
    await db_session.flush()
    before = item.updated_at
    asset = await register_item_asset(db_session, item)
    await db_session.refresh(item)
    assert item.updated_at == before
    assert asset.verification_state == "unverified"
    assert asset.checksum_value is None
    assert asset.legacy_hash_hint == "f" * 32
    replica = await db_session.scalar(
        select(DatasetAssetLocation).where(DatasetAssetLocation.asset_id == asset.id)
    )
    assert replica.object_key == "original-name/sub/old.bin"
    assert replica.etag is None
    assert replica.reference_state == "unverified"
    assert (
        await db_session.scalar(select(func.count()).select_from(StorageBinding)) == 9
    )
    await register_item_asset(db_session, item)
    assert (
        await db_session.scalar(select(func.count()).select_from(DatasetAssetLocation))
        == 1
    )


@pytest.mark.asyncio
async def test_verified_asset_is_database_immutable(db_session, super_admin):
    user, _ = super_admin
    dataset = Dataset(name="immutable", display_id="DS-IMM", created_by=user.id)
    db_session.add(dataset)
    await db_session.flush()
    item = await DatasetService(db_session).add_item(
        dataset.id,
        "a.bin",
        "legacy/a.bin",
        "other",
        file_size=0,
        sha256=hashlib.sha256(b"").hexdigest(),
    )
    asset_id = await db_session.scalar(
        select(DatasetAsset.id).where(DatasetAsset.dataset_item_id == item.id)
    )
    with pytest.raises(IntegrityError, match="immutable"):
        async with db_session.begin_nested():
            await db_session.execute(
                update(DatasetAsset)
                .where(DatasetAsset.id == asset_id)
                .values(checksum_value="f" * 64)
            )


@pytest.mark.asyncio
async def test_same_name_uploads_etag_dedup_rename_and_zero_bytes(
    httpx_client, db_session, super_admin, monkeypatch
):
    user, token = super_admin
    fake = ObjectClient()
    monkeypatch.setattr(storage_service, "client", fake)
    headers = {"Authorization": f"Bearer {token}"}
    dataset = await DatasetService(db_session).create(
        "equal-name", "", "image", user.id
    )
    other = await DatasetService(db_session).create("equal-name", "", "image", user.id)
    original_root = dataset.storage_scan_prefix
    keys = []
    for i, data in enumerate([b"original", b"changed", b"original", b""]):
        response = await httpx_client.post(
            f"/api/v1/datasets/{dataset.id}/items/upload-init",
            headers=headers,
            json={"file_name": "a.bin", "content_type": "application/octet-stream"},
        )
        assert response.status_code == 200, response.text
        item_id = uuid.UUID(response.json()["item_id"])
        item = await db_session.get(DatasetItem, item_id)
        key = item.file_path
        keys.append(key)
        staging_key = fake.presigned[-1][1]["Key"]
        assert staging_key != key
        fake.objects[staging_key] = data
        response = await httpx_client.post(
            f"/api/v1/datasets/{dataset.id}/items/upload-complete/{item_id}",
            headers=headers,
        )
        assert response.status_code == (409 if i == 2 else 200), response.text
        if i == 0:
            await DatasetService(db_session).update(dataset.id, "second-name", None)
            await DatasetService(db_session).update(dataset.id, "third-name", None)
    assert len(set(keys)) == 4
    assert all(thread != threading.get_ident() for thread in fake.read_threads)
    assert dataset_relative_path(keys[1]) == "a-1.bin"
    assert fake.objects[keys[0]] == b"original"
    assert fake.objects[keys[1]] == b"changed"
    assert fake.objects[keys[2]] == b"original"
    assert not [call for call in fake.calls if call[0] == "delete"]
    assert dataset.storage_scan_prefix == original_root
    assets = list(await db_session.scalars(select(DatasetAsset)))
    assert {asset.checksum_value for asset in assets} == {
        hashlib.sha256(data).hexdigest() for data in [b"original", b"changed", b""]
    }
    assert managed_dataset_key(other.id, "a.bin").startswith(
        managed_dataset_prefix(other.id)
    )


@pytest.mark.asyncio
async def test_scan_uses_pinned_legacy_root_and_keeps_etag_separate(
    db_session, super_admin, monkeypatch
):
    user, _ = super_admin
    dataset = Dataset(
        name="new-name",
        display_id="DS-SCAN",
        created_by=user.id,
        storage_scan_prefix="old-name/",
    )
    db_session.add(dataset)
    await db_session.flush()
    seen = []

    def listing(prefix, **kwargs):
        seen.append(prefix)
        return (
            [{"key": "old-name/f.bin", "size": 7, "etag": "a" * 32}]
            if prefix == "old-name/"
            else []
        )

    monkeypatch.setattr(storage_service, "list_objects", listing)
    svc = DatasetService(db_session)
    assert (await svc.scan_and_import(dataset.id))[0].status == "added"
    assert (await svc.scan_and_import(dataset.id))[0].status == "skipped"
    assert set(seen) == {"old-name/", managed_dataset_prefix(dataset.id)}
    item = await db_session.scalar(
        select(DatasetItem).where(DatasetItem.dataset_id == dataset.id)
    )
    assert item.file_path == "old-name/f.bin"
    assert item.content_hash is None
    asset = await db_session.scalar(
        select(DatasetAsset).where(DatasetAsset.dataset_item_id == item.id)
    )
    replica = await db_session.scalar(
        select(DatasetAssetLocation).where(DatasetAssetLocation.asset_id == asset.id)
    )
    assert asset.checksum_value is None
    assert replica.etag == "a" * 32


@pytest.mark.asyncio
async def test_binding_validation_rejects_config_drift_and_missing_purpose(
    db_session, super_admin, monkeypatch
):
    from app.services.dataset_assets import ensure_legacy_bindings
    from app.config import settings

    await ensure_legacy_bindings(db_session)
    assert await validate_legacy_bindings(db_session) == []
    monkeypatch.setattr(settings, "minio_endpoint", "changed.example:9000")
    assert "platform_location_mismatch" in await validate_legacy_bindings(db_session)
    await db_session.execute(
        delete(StorageBinding).where(StorageBinding.purpose == "avatars")
    )
    assert "missing_binding:avatars" in await validate_legacy_bindings(db_session)


@pytest.mark.asyncio
async def test_legacy_hash_is_only_a_candidate_until_bytes_match(
    db_session, super_admin, monkeypatch
):
    user, _ = super_admin
    dataset = Dataset(name="legacy", display_id="DS-LAZY", created_by=user.id)
    db_session.add(dataset)
    await db_session.flush()
    data = b"historical bytes"
    md5 = hashlib.md5(data).hexdigest()
    item = DatasetItem(
        dataset_id=dataset.id,
        file_name="a.bin",
        file_path="original/a.bin",
        file_type="other",
        content_hash=md5,
    )
    db_session.add(item)
    await db_session.flush()
    before = item.updated_at
    fake = ObjectClient()
    monkeypatch.setattr(storage_service, "client", fake)
    fake.objects[item.file_path] = b"overwritten bytes"
    assert (
        await find_verified_content(
            db_session, dataset.id, hashlib.sha256(data).hexdigest(), legacy_md5=md5
        )
        is None
    )
    assert await db_session.scalar(select(func.count()).select_from(DatasetAsset)) == 0
    fake.objects[item.file_path] = data
    assert (
        await find_verified_content(
            db_session, dataset.id, hashlib.sha256(data).hexdigest(), legacy_md5=md5
        )
        is item
    )
    await db_session.refresh(item)
    assert item.updated_at == before and item.content_hash == md5
    asset = await db_session.scalar(
        select(DatasetAsset).where(DatasetAsset.dataset_item_id == item.id)
    )
    assert asset.verification_state == "verified"


def test_changed_head_get_identity_is_rejected_and_stream_closed(monkeypatch):
    body = io.BytesIO(b"new")

    class ChangedObject:
        def get_object(self, **kwargs):
            assert kwargs["IfMatch"] == '"old"'
            assert kwargs["VersionId"] == "v1"
            return {"Body": body, "ETag": '"new"'}

    monkeypatch.setattr(storage_service, "client", ChangedObject())
    with pytest.raises(ValueError, match="changed"):
        hash_stored_object(
            "file", {"ETag": '"old"', "VersionId": "v1", "ContentLength": 3}
        )
    assert body.closed


@pytest.mark.asyncio
async def test_scanned_aliases_are_used_by_scene_frame_grouping_and_export(
    db_session, super_admin, monkeypatch
):
    from app.services.scene_inference import _split_into_scene_groups
    from app.services.pointcloud_import import group_frames
    from app.services.exporting.packaging import relative_path_from_file_path

    user, _ = super_admin
    dataset = Dataset(name="scan-alias", display_id="DS-ALIAS", created_by=user.id)
    db_session.add(dataset)
    await db_session.flush()
    scene_uuid = str(uuid.uuid4())
    first = managed_dataset_key(dataset.id, "scene_a/lidar/000001.pcd")
    second = managed_dataset_key(dataset.id, "scene_a/lidar/000001.pcd")
    manual = managed_dataset_prefix(dataset.id) + f"{scene_uuid}/lidar/000001.pcd"
    monkeypatch.setattr(
        storage_service,
        "list_objects",
        lambda prefix, **kwargs: [
            {"key": key, "size": 1, "etag": "etag"}
            for key in (first, second, manual)
            if key.startswith(prefix)
        ],
    )
    svc = DatasetService(db_session)
    assert [o.status for o in await svc.scan_and_import(dataset.id)] == ["added"] * 3
    assert [o.status for o in await svc.scan_and_import(dataset.id)] == ["skipped"] * 3
    items = list(await db_session.scalars(select(DatasetItem)))
    expected = {
        "scene_a/lidar/000001.pcd",
        "scene_a/lidar/000001-1.pcd",
        f"{scene_uuid}/lidar/000001.pcd",
    }
    assert {item.file_path for item in items} == {first, second, manual}
    assert {dataset_item_relative_path(item) for item in items} == expected
    assert {
        relative_path_from_file_path(item.file_path, "renamed", item) for item in items
    } == expected
    groups = _split_into_scene_groups(items, "renamed")
    assert set(groups) == {"scene_a", scene_uuid}
    frames, _ = group_frames(groups["scene_a"])
    assert set(frames) == {"000001", "000001-1"}
    assert (
        await svc._unique_file_name(dataset.id, "scene_a/lidar/000001.pcd")
        == "scene_a/lidar/000001-2.pcd"
    )


@pytest.mark.asyncio
async def test_unreadable_legacy_candidate_does_not_establish_a_duplicate(
    db_session, super_admin, monkeypatch
):
    from botocore.exceptions import IncompleteReadError

    user, _ = super_admin
    dataset = Dataset(name="short-read", display_id="DS-SHORT", created_by=user.id)
    db_session.add(dataset)
    await db_session.flush()
    data = b"old bytes"
    item = DatasetItem(
        dataset_id=dataset.id,
        file_name="a.bin",
        file_path="old/a.bin",
        file_type="other",
        content_hash=hashlib.md5(data).hexdigest(),
    )
    db_session.add(item)
    await db_session.flush()
    await register_item_asset(db_session, item)

    class ShortBody(io.BytesIO):
        def read(self, *args):
            raise IncompleteReadError(actual_bytes=2, expected_bytes=9)

    body = ShortBody()
    fake = ObjectClient()
    monkeypatch.setattr(fake, "get_object", lambda **kwargs: {"Body": body})
    monkeypatch.setattr(storage_service, "client", fake)
    assert (
        await find_verified_content(
            db_session,
            dataset.id,
            hashlib.sha256(data).hexdigest(),
            legacy_md5=item.content_hash,
        )
        is None
    )
    assert body.closed
    asset = await db_session.scalar(
        select(DatasetAsset).where(DatasetAsset.dataset_item_id == item.id)
    )
    assert asset.verification_state == "unverified"


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["PreconditionFailed", "NoSuchVersion", "NoSuchKey"])
async def test_upload_completion_maps_storage_races_to_conflict(
    httpx_client, db_session, super_admin, monkeypatch, code
):
    from botocore.exceptions import ClientError

    user, token = super_admin
    fake = ObjectClient()
    monkeypatch.setattr(storage_service, "client", fake)
    dataset = await DatasetService(db_session).create(
        "raced-upload", "", "image", user.id
    )
    headers = {"Authorization": f"Bearer {token}"}
    response = await httpx_client.post(
        f"/api/v1/datasets/{dataset.id}/items/upload-init",
        headers=headers,
        json={"file_name": "a.bin", "content_type": "application/octet-stream"},
    )
    assert response.status_code == 200, response.text
    item_id = uuid.UUID(response.json()["item_id"])
    item = await db_session.get(DatasetItem, item_id)
    fake.objects[fake.presigned[-1][1]["Key"]] = b"original"

    def changed(**kwargs):
        assert kwargs["IfMatch"]
        raise ClientError({"Error": {"Code": code}}, "GetObject")

    monkeypatch.setattr(fake, "get_object", changed)
    response = await httpx_client.post(
        f"/api/v1/datasets/{dataset.id}/items/upload-complete/{item_id}",
        headers=headers,
    )
    assert response.status_code == 409, response.text
    asset = await db_session.scalar(
        select(DatasetAsset).where(DatasetAsset.dataset_item_id == item_id)
    )
    assert asset.verification_state == "unverified"
    assert item.content_hash is None
