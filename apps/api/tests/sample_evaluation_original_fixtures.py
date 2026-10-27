"""Exact bucket/key provider and registered inputs for source comparisons."""

from datetime import datetime, timezone
import hashlib
import io
from types import SimpleNamespace
import uuid

from botocore.exceptions import ClientError

from app.config import settings
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.dataset_asset import DatasetAsset, DatasetAssetLocation
from app.services import managed_original_reader
from app.services.dataset_assets import LEGACY_LOCATION_ID


class Objects:
    def __init__(self):
        self.objects, self.calls, self.bodies = {}, [], []
        self.meta = SimpleNamespace(
            endpoint_url=(
                f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}"
            )
        )

    def put(self, bucket, key, raw):
        self.objects[bucket, key] = raw

    def error(self, code, status):
        return ClientError(
            {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
            "Read",
        )

    def metadata(self, bucket, key):
        raw = self.objects.get((bucket, key))
        if raw is None:
            raise self.error("NoSuchKey", 404)
        return {
            "ResponseMetadata": {"HTTPStatusCode": 200},
            "ContentLength": len(raw),
            "ETag": '"' + hashlib.md5(raw).hexdigest() + '"',
            "LastModified": datetime(2026, 10, 24, tzinfo=timezone.utc),
            "ContentType": "image/png",
        }

    def head_object(self, *, Bucket, Key, **_):
        self.calls.append(("head", Bucket, Key))
        return self.metadata(Bucket, Key)

    def get_object(self, *, Bucket, Key, IfMatch=None, **_):
        self.calls.append(("get", Bucket, Key))
        head = self.metadata(Bucket, Key)
        if IfMatch is not None and IfMatch != head["ETag"]:
            raise self.error("PreconditionFailed", 412)
        body = io.BytesIO(self.objects[Bucket, Key])
        self.bodies.append(body)
        return {**head, "Body": body}

    def close(self):
        self.calls.append(("close", None, None))

    def install(self, monkeypatch):
        monkeypatch.setattr(managed_original_reader, "create_client", lambda **_: self)
        return self


async def registered(
    db, task, owner, raw, *, bucket="saved-evaluation-originals", key=None
):
    dataset = Dataset(
        id=uuid.uuid4(),
        display_id="D-SOURCE-" + uuid.uuid4().hex[:10],
        name="Current originals",
        created_by=owner.id,
    )
    db.add(dataset)
    await db.flush()
    item = DatasetItem(
        id=uuid.uuid4(),
        dataset_id=dataset.id,
        file_path=key or task.file_path,
        file_name="current.png",
        file_type="image",
        width=16,
        height=12,
    )
    db.add(item)
    await db.flush()
    asset = DatasetAsset(
        id=uuid.uuid4(),
        dataset_item_id=item.id,
        media_type="image",
        verification_state="verified",
        checksum_algorithm="sha256",
        checksum_value=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw),
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
    )
    db.add(replica)
    task.dataset_item_id, task.file_path = item.id, item.file_path
    await db.flush()
    return item, asset, replica
