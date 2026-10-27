"""Registered managed image inputs and borrowed stages for training tests."""

import hashlib
import io
import uuid

from PIL import Image
from sqlalchemy import select

from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.dataset_asset import DatasetAsset, DatasetAssetLocation
from app.services import storage_access
from app.services.dataset_assets import LEGACY_LOCATION_ID, ensure_legacy_bindings
from app.services.managed_original_reader import BinaryStage


def image_bytes(size=(32, 24), color=(20, 40, 100)):
    buffer = io.BytesIO()
    with Image.new("RGB", size, color) as image:
        image.save(buffer, format="PNG")
    return buffer.getvalue()


def original_receipt(raw, *, content_id=None):
    return {
        "schema": 1,
        "owner_kind": "dataset_asset",
        "content_id": str(content_id or uuid.uuid4()),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
        "location_id": str(LEGACY_LOCATION_ID),
    }


def staged_image(raw):
    with Image.open(io.BytesIO(raw)) as image:
        image.load()
    return BinaryStage(io.BytesIO(raw), hashlib.sha256(raw).hexdigest(), len(raw), {})


async def register_original(db, task, owner, *, raw=None, provider=None, bucket=None):
    raw = image_bytes() if raw is None else raw
    with Image.open(io.BytesIO(raw)) as image:
        image.load()
        width, height = image.size
    await ensure_legacy_bindings(db)
    await storage_access.ensure(db, LEGACY_LOCATION_ID)
    item = (
        await db.get(DatasetItem, task.dataset_item_id)
        if task.dataset_item_id
        else None
    )
    if item is None:
        dataset = Dataset(
            display_id="D-TRAIN-" + uuid.uuid4().hex[:10],
            name="Training original",
            created_by=owner.id,
        )
        db.add(dataset)
        await db.flush()
        item = DatasetItem(
            dataset_id=dataset.id,
            file_name=task.file_name,
            file_path=task.file_path,
            file_type="image",
        )
        db.add(item)
        await db.flush()
        task.dataset_item_id = item.id
    item.width, item.height = width, height
    item.file_size, item.content_hash = len(raw), hashlib.sha256(raw).hexdigest()
    asset = await db.scalar(
        select(DatasetAsset).where(DatasetAsset.dataset_item_id == item.id)
    )
    if asset is None:
        asset = DatasetAsset(
            id=uuid.uuid4(), dataset_item_id=item.id, media_type="image"
        )
        db.add(asset)
    asset.verification_state = "verified"
    asset.checksum_algorithm, asset.checksum_value = "sha256", item.content_hash
    asset.size_bytes = len(raw)
    await db.flush()
    replica = DatasetAssetLocation(
        asset_id=asset.id,
        location_id=LEGACY_LOCATION_ID,
        bucket=bucket or "saved-training-originals",
        object_key=item.file_path,
        role="managed_original",
        reference_state="present",
    )
    db.add(replica)
    await db.flush()
    if provider is not None:
        provider.put(replica.bucket, replica.object_key, raw)
    return item, asset, replica
