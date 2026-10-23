"""Retirement records share business deletion's atomic boundary, without I/O."""

from types import SimpleNamespace
import uuid
from unittest.mock import MagicMock

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.dataset_asset import (
    DatasetAsset,
    DatasetAssetLocation,
    StorageBinding,
    StorageLocation,
)
from app.db.models.managed_original_retirement import (
    ManagedOriginalRetirement as Retirement,
)
from app.db.models.task import Task
from app.services.dataset import DatasetService
from app.services.dataset_assets import LEGACY_LOCATION_ID, register_item_asset
from app.services.dataset_storage_keys import managed_dataset_key
from app.services import managed_original_retirement as retirement
from app.services.storage import storage_service
from tests.factory import create_project


async def managed_item(db, owner, *, verified=True):
    dataset = Dataset(
        id=uuid.uuid4(),
        display_id=f"D-{uuid.uuid4().hex[:10]}",
        name="Retired managed originals",
        created_by=owner.id,
    )
    item = DatasetItem(
        id=uuid.uuid4(),
        dataset_id=dataset.id,
        file_name="photo.png",
        file_path=managed_dataset_key(dataset.id, "photo.png"),
        file_type="image",
        file_size=123 if verified else None,
    )
    db.add(dataset)
    await db.flush()
    db.add(item)
    await db.flush()
    evidence = (
        {"sha256": "a" * 64, "size_bytes": 123, "etag": "etag"} if verified else {}
    )
    asset = await register_item_asset(db, item, **evidence)
    return dataset, item, asset


@pytest.mark.parametrize("target", ["dataset", "item"])
async def test_hard_delete_keeps_exact_managed_original_without_physical_io(
    db_session, super_admin, monkeypatch, target
):
    db = db_session
    dataset, item, asset = await managed_item(db, super_admin[0])
    historical = dataset.id, item.id, asset.id, item.file_path
    delete_object, delete_prefix = MagicMock(), MagicMock()
    monkeypatch.setattr(storage_service, "delete_object", delete_object)
    monkeypatch.setattr(storage_service, "delete_prefix", delete_prefix)
    service = DatasetService(db)
    if target == "dataset":
        await service.delete(dataset.id)
    else:
        assert await service.delete_item(item.id, dataset_id=dataset.id)
    await db.commit()

    record = await db.scalar(
        select(Retirement).where(Retirement.asset_id == historical[2])
    )
    assert record is not None
    assert (
        record.dataset_id,
        record.dataset_item_id,
        record.asset_id,
        record.object_key,
    ) == historical
    assert record.location_id == LEGACY_LOCATION_ID
    assert record.bucket == storage_service.datasets_bucket
    assert record.role == "managed_original"
    assert record.identity_state == "verified_binding"
    assert record.storage_identity == retirement.managed_storage_identity(
        storage_service.client.meta.endpoint_url, storage_service.datasets_bucket
    )
    assert record.identity_error_code is None
    assert (record.location_config_revision, record.binding_generation) == (1, 1)
    assert (record.sha256, record.size_bytes) == ("a" * 64, 123)
    assert (record.reference_state, record.asset_verification_state) == (
        "present",
        "verified",
    )
    assert record.verified_at is not None and record.retired_at is not None
    assert record.retain_until is None and record.policy_revision == 1
    assert not await db.scalar(
        select(DatasetItem.id).where(DatasetItem.id == historical[1])
    )
    assert not await db.scalar(
        select(DatasetAsset.id).where(DatasetAsset.id == historical[2])
    )
    assert not await db.scalar(
        select(DatasetAssetLocation).where(
            DatasetAssetLocation.asset_id == historical[2]
        )
    )
    delete_object.assert_not_called()
    delete_prefix.assert_not_called()


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ("missing_binding", "platform_binding_unproven"),
        ("binding_generation", "platform_binding_unproven"),
        ("location_revision", "platform_configuration_unproven"),
        ("location_config", "platform_configuration_unproven"),
        ("runtime_endpoint", "runtime_configuration_unproven"),
        ("runtime_bucket", "runtime_configuration_unproven"),
        ("other_location", "platform_location_unproven"),
    ],
)
async def test_unknown_identity_is_retained_without_blocking_business_delete(
    db_session, super_admin, monkeypatch, change, error
):
    db = db_session
    dataset, item, asset = await managed_item(db, super_admin[0])
    asset_id = asset.id
    binding = await db.get(StorageBinding, "datasets")
    location = await db.get(StorageLocation, LEGACY_LOCATION_ID)
    if change == "missing_binding":
        await db.delete(binding)
    elif change == "binding_generation":
        binding.generation = 2
    elif change == "location_revision":
        location.config_revision = 2
    elif change == "location_config":
        location.config = {"endpoint": "different.invalid", "use_ssl": False}
    elif change == "other_location":
        other = StorageLocation(
            id=uuid.uuid4(), name=f"other-{uuid.uuid4()}", kind="s3", config={}
        )
        db.add(other)
        await db.flush()
        locator = await db.get(
            DatasetAssetLocation, (asset.id, LEGACY_LOCATION_ID, binding.bucket)
        )
        locator.location_id = other.id
    else:
        monkeypatch.setattr(
            retirement,
            "storage_service",
            SimpleNamespace(
                client=SimpleNamespace(
                    meta=SimpleNamespace(
                        endpoint_url=(
                            "https://different.invalid"
                            if change == "runtime_endpoint"
                            else storage_service.client.meta.endpoint_url
                        )
                    )
                ),
                datasets_bucket=(
                    "different-bucket"
                    if change == "runtime_bucket"
                    else storage_service.datasets_bucket
                ),
            ),
        )
    await db.flush()
    assert await DatasetService(db).delete_item(item.id, dataset_id=dataset.id)
    record = await db.scalar(select(Retirement).where(Retirement.asset_id == asset_id))
    assert record.identity_state == "unknown"
    assert record.identity_error_code == error
    assert record.storage_identity is None


async def test_capture_is_idempotent_and_excludes_other_roles_and_reference_items(
    db_session, super_admin
):
    db = db_session
    dataset, item, asset = await managed_item(db, super_admin[0])
    for role in ("original_cache", "source_reference", "preview"):
        db.add(
            DatasetAssetLocation(
                asset_id=asset.id,
                location_id=LEGACY_LOCATION_ID,
                bucket=f"other-{role}",
                object_key=f"private/{role}",
                role=role,
            )
        )
    await db.flush()
    for _ in range(2):
        await retirement.capture_managed_original_retirements(db, dataset_id=dataset.id)
    assert await db.scalar(select(func.count()).select_from(Retirement)) == 1
    record = await db.scalar(select(Retirement))
    assert record.object_key == item.file_path and record.role == "managed_original"
    await db.execute(delete(Retirement))
    # Use the actual reference namespace; a mislabeled reference locator must not
    # become a managed retirement assertion merely because its role is defaulted.
    from app.services.reference_keys import PREFIX

    item.file_path = f"{PREFIX}{item.id}/{'a' * 64}"
    await db.flush()
    await retirement.capture_managed_original_retirements(db, dataset_id=dataset.id)
    assert not await db.scalar(select(Retirement.id))


async def test_legacy_unverified_registered_locator_is_preserved(
    db_session, super_admin
):
    db = db_session
    dataset, item, asset = await managed_item(db, super_admin[0], verified=False)
    locator = await db.get(
        DatasetAssetLocation,
        (asset.id, LEGACY_LOCATION_ID, storage_service.datasets_bucket),
    )
    locator.object_key = "historical/shared/photo.png"
    await db.flush()
    assert await DatasetService(db).delete_item(item.id)
    record = await db.scalar(select(Retirement).where(Retirement.asset_id == asset.id))
    assert record.object_key == "historical/shared/photo.png"
    assert record.sha256 is None and record.size_bytes is None
    assert record.asset_verification_state == "unverified"
    assert record.verified_at is None


async def test_shared_physical_locator_retains_each_asset_assertion(
    db_session, super_admin
):
    db = db_session
    first, _, first_asset = await managed_item(db, super_admin[0])
    second, _, second_asset = await managed_item(db, super_admin[0])
    locators = list(await db.scalars(select(DatasetAssetLocation)))
    for locator in locators:
        locator.object_key = "historical/shared/original.png"
    await db.flush()
    await DatasetService(db).delete(first.id)
    await DatasetService(db).delete(second.id)
    records = list(await db.scalars(select(Retirement)))
    assert len(records) == 2
    assert {record.asset_id for record in records} == {first_asset.id, second_asset.id}
    assert len({record.assertion_digest for record in records}) == 2
    assert {record.object_key for record in records} == {
        "historical/shared/original.png"
    }


async def test_missing_registered_locator_does_not_invent_ownership(
    db_session, super_admin
):
    db = db_session
    dataset, item, asset = await managed_item(db, super_admin[0])
    await db.execute(
        delete(DatasetAssetLocation).where(DatasetAssetLocation.asset_id == asset.id)
    )
    assert await DatasetService(db).delete_item(item.id, dataset_id=dataset.id)
    assert not await db.scalar(select(Retirement.id))


@pytest.mark.parametrize("target", ["dataset", "item"])
async def test_failed_business_fk_rolls_back_retirement_assertions(
    db_session, super_admin, target
):
    db = db_session
    dataset, item, asset = await managed_item(db, super_admin[0])
    project = await create_project(db, owner_id=super_admin[0].id, name="FK root")
    db.add(
        Task(
            display_id=f"T-{uuid.uuid4().hex[:10]}",
            project_id=project.id,
            dataset_item_id=item.id,
            file_name=item.file_name,
            file_path=item.file_path,
            file_type="image",
        )
    )
    await db.flush()
    dataset_id, item_id, asset_id = dataset.id, item.id, asset.id
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            if target == "dataset":
                await DatasetService(db).delete(dataset_id)
            else:
                await DatasetService(db).delete_item(item_id)
    assert await db.get(DatasetItem, item_id) is not None
    assert not await db.scalar(
        select(Retirement.id).where(Retirement.asset_id == asset_id)
    )


async def test_caller_rollback_removes_capture_and_business_deletion(
    db_session, super_admin
):
    db = db_session
    dataset, item, asset = await managed_item(db, super_admin[0])
    item_id, asset_id = item.id, asset.id
    nested = await db.begin_nested()
    assert await DatasetService(db).delete_item(item_id)
    assert await db.scalar(select(Retirement.id).where(Retirement.asset_id == asset_id))
    await nested.rollback()
    assert await db.get(DatasetItem, item_id) is not None
    assert not await db.scalar(
        select(Retirement.id).where(Retirement.asset_id == asset_id)
    )
