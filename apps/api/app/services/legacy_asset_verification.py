"""Explicit observation and reviewed publication of legacy managed content."""

from dataclasses import dataclass
import hashlib
import json
import re
import time
from types import SimpleNamespace
import uuid

from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.dataset_asset import (
    DatasetAsset,
    DatasetAssetLocation,
    StorageBinding,
    StorageLocation,
)
from app.db.models.dataset_source import DatasetReference
from app.services import managed_original_reader as reader, storage_access
from app.services.audit import AuditService
from app.services.content_snapshot import ContentError
from app.services.dataset_assets import (
    LEGACY_LOCATION_ID,
    MAX_DIRECT_UPLOAD_BYTES,
    register_item_asset,
    validate_legacy_bindings,
)
from app.services.reference_keys import is_reference
from app.services.storage import storage_service

_SHA = re.compile(r"[a-f0-9]{64}")
TIMEOUT_SECONDS = 60


def reject(code):
    raise ContentError("legacy_verification_" + code)


@dataclass(frozen=True)
class Preview:
    item_id: uuid.UUID
    dataset_id: uuid.UUID
    asset_id: uuid.UUID
    fingerprint: str
    sha256: str
    size_bytes: int

    def __post_init__(self):
        if (
            any(
                not isinstance(v, uuid.UUID)
                for v in (self.item_id, self.dataset_id, self.asset_id)
            )
            or not isinstance(self.sha256, str)
            or not _SHA.fullmatch(self.sha256)
            or not isinstance(self.fingerprint, str)
            or not _SHA.fullmatch(self.fingerprint)
            or type(self.size_bytes) is not int
            or not 0 <= self.size_bytes <= MAX_DIRECT_UPLOAD_BYTES
        ):
            reject("invalid_preview")

    def public(self):
        return {
            **{
                name: str(getattr(self, name))
                for name in ("item_id", "dataset_id", "asset_id")
            },
            "fingerprint": self.fingerprint,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }

    @classmethod
    def parse(cls, data):
        if set(data) != {
            "item_id",
            "dataset_id",
            "asset_id",
            "fingerprint",
            "sha256",
            "size_bytes",
        }:
            reject("invalid_preview")
        return cls(
            **{
                **data,
                **{
                    name: uuid.UUID(data[name])
                    for name in ("item_id", "dataset_id", "asset_id")
                },
            }
        )


@dataclass(frozen=True)
class Pin:
    item_id: uuid.UUID
    dataset_id: uuid.UUID
    asset_id: uuid.UUID
    fingerprint: str
    endpoint: str
    bucket: str
    key: str
    size_hint: int | None
    maintenance_revision: int
    verified_sha256: str | None
    verified_size: int | None


def _query(statement, lock, *, share=False):
    return (
        statement.with_for_update(read=share, nowait=True) if lock else statement
    ).execution_options(populate_existing=True)


async def pin(db, item_id, *, lock=False):
    if lock:
        await db.execute(text("SET LOCAL lock_timeout = '1s'"))
    location = await db.scalar(
        _query(
            select(StorageLocation).where(StorageLocation.id == LEGACY_LOCATION_ID),
            lock,
            share=True,
        )
    )
    binding = await db.scalar(
        _query(
            select(StorageBinding).where(StorageBinding.purpose == "datasets"),
            lock,
            share=True,
        )
    )
    if (
        location is None
        or binding is None
        or not location.enabled
        or location.connection_id is not None
        or await validate_legacy_bindings(db)
    ):
        reject("configuration_changed")
    item_dataset = await db.scalar(
        select(DatasetItem.dataset_id).where(DatasetItem.id == item_id)
    )
    dataset = await db.scalar(
        _query(select(Dataset).where(Dataset.id == item_dataset), lock, share=True)
    )
    item = await db.scalar(
        _query(select(DatasetItem).where(DatasetItem.id == item_id), lock)
    )
    if item is None or dataset is None or item.dataset_id != dataset.id:
        reject("item_removed")
    if is_reference(item.file_path):
        reject("not_legacy_managed")
    if await db.scalar(
        select(DatasetReference.dataset_item_id)
        .where(DatasetReference.dataset_item_id == item.id)
        .limit(1)
    ):
        reject("not_legacy_managed")
    asset = await db.scalar(
        _query(
            select(DatasetAsset).where(DatasetAsset.dataset_item_id == item.id), lock
        )
    )
    if asset is None:
        reject("mapping_required")
    replicas = list(
        await db.scalars(
            _query(
                select(DatasetAssetLocation).where(
                    DatasetAssetLocation.asset_id == asset.id
                ),
                lock,
            )
        )
    )
    if len(replicas) != 1:
        reject("location_ambiguous")
    replica = replicas[0]
    if (
        asset.media_type != item.file_type
        or replica.location_id != binding.location_id
        or replica.location_id != LEGACY_LOCATION_ID
        or replica.bucket != binding.bucket
        or replica.object_key != item.file_path
        or replica.role != "managed_original"
        or replica.creation_id is not None
    ):
        reject("not_legacy_managed")
    if asset.verification_state == "verified":
        if (
            asset.checksum_algorithm != "sha256"
            or not _SHA.fullmatch(asset.checksum_value or "")
            or replica.reference_state != "present"
        ):
            reject("identity_invalid")
    elif (
        asset.verification_state != "unverified"
        or asset.checksum_algorithm is not None
        or asset.checksum_value is not None
        or asset.size_bytes != item.file_size
        or replica.reference_state != "unverified"
    ):
        reject("identity_changed")
    revision = await storage_access.check(db, LEGACY_LOCATION_ID, lock=lock)
    facts = {
        "item": [
            str(item.id),
            str(item.dataset_id),
            item.file_path,
            item.file_type,
            item.file_size,
            item.content_hash,
            item.width,
            item.height,
        ],
        "dataset": [
            str(dataset.id),
            dataset.lifecycle_revision,
            str(dataset.archived_at),
        ],
        "asset": [str(asset.id), asset.media_type, asset.legacy_hash_hint],
        "replica": [
            str(replica.location_id),
            replica.bucket,
            replica.object_key,
            replica.role,
        ],
        "location": [
            location.kind,
            location.config_revision,
            location.config,
            location.enabled,
        ],
        "binding": [binding.generation, binding.bucket],
        "maintenance": revision,
    }
    fingerprint = hashlib.sha256(
        json.dumps(facts, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return Pin(
        item.id,
        item.dataset_id,
        asset.id,
        fingerprint,
        storage_service.client.meta.endpoint_url.rstrip("/"),
        replica.bucket,
        replica.object_key,
        item.file_size,
        revision,
        asset.checksum_value,
        asset.size_bytes,
    )


def observe(source, expected=None):
    """Hash an exact conditional stream; no source write or temporary copy."""
    deadline = time.monotonic() + TIMEOUT_SECONDS

    def check():
        if time.monotonic() >= deadline:
            raise ContentError("content_read_timeout", 504)

    try:
        try:
            client = reader.create_client(timeout_seconds=15)
        except BaseException as exc:
            raise storage_access.mark_unsettled(reader._failure(exc)) from None
        with storage_access.closing_registered(client) as client:
            if client.meta.endpoint_url.rstrip("/") != source.endpoint:
                reject("configuration_changed")
            params = {"Bucket": source.bucket, "Key": source.key}
            check()
            response = client.head_object(**params)
            size = response.get("ContentLength") if isinstance(response, dict) else None
            if type(size) is not int or size < 0:
                reject("source_changed")
            if size > MAX_DIRECT_UPLOAD_BYTES:
                raise ContentError("content_too_large", 413)
            metadata = SimpleNamespace(
                bucket=source.bucket, object_key=source.key, size_bytes=size
            )
            before = reader._observation(response, metadata)
            if source.size_hint is not None and source.size_hint != before["size"]:
                reject("size_changed")
            conditional = dict(params)
            if before["version_id"] not in {None, "null"}:
                conditional["VersionId"] = before["version_id"]
            reader._conditions(client, conditional, before["etag"], check)
            response = client.get_object(**conditional, IfMatch=before["etag"])
            with storage_access.closing_registered(reader._body(response)) as body:
                if reader._observation(response, metadata) != before:
                    reject("source_changed")
                digest, size = hashlib.sha256(), 0
                while True:
                    check()
                    requested = min(reader.CHUNK_BYTES, before["size"] - size + 1)
                    block = body.read(requested)
                    check()
                    if type(block) is not bytes or len(block) > requested:
                        reject("source_changed")
                    if not block:
                        break
                    size += len(block)
                    if size > before["size"]:
                        reject("source_changed")
                    digest.update(block)
                if size != before["size"]:
                    reject("source_changed")
            if reader._observation(client.head_object(**params), metadata) != before:
                reject("source_changed")
            check()
            sha256 = digest.hexdigest()
            if expected is not None and (sha256, size) != (
                expected.sha256,
                expected.size_bytes,
            ):
                reject("content_changed")
            return sha256, size, before["etag"].strip('"')
    except BaseException as exc:
        if isinstance(exc, Exception):
            raise reader._failure(exc) from None
        raise


async def verify(factory, item_id, *, expected=None):
    """Preview by default; a reviewed preview is required to publish identity."""
    async with factory() as db:
        source = await pin(db, item_id)
    if expected is not None and (
        expected.item_id,
        expected.asset_id,
        expected.dataset_id,
        expected.fingerprint,
    ) != (source.item_id, source.asset_id, source.dataset_id, source.fingerprint):
        reject("preview_stale")
    if source.verified_sha256 is not None:
        if expected and (expected.sha256, expected.size_bytes) != (
            source.verified_sha256,
            source.verified_size,
        ):
            reject("identity_changed")
        return {"status": "already_verified", "item_id": str(item_id)}
    async with factory() as db:

        async def admitted(_permit):
            if await pin(db, item_id) != source:
                reject("preview_stale")
            await db.rollback()

        sha256, size, etag = await storage_access.run(
            db,
            LEGACY_LOCATION_ID,
            "legacy_asset_verification",
            observe,
            source,
            expected,
            expected_revision=source.maintenance_revision,
            timeout=TIMEOUT_SECONDS + 20,
            on_acquired=admitted,
        )
    preview = Preview(
        item_id, source.dataset_id, source.asset_id, source.fingerprint, sha256, size
    )
    async with factory() as db:
        try:
            current = await pin(db, item_id, lock=expected is not None)
            if current.fingerprint != source.fingerprint:
                reject("preview_stale")
            if current.verified_sha256 is not None:
                if (current.verified_sha256, current.verified_size) != (sha256, size):
                    reject("identity_changed")
                return {"status": "already_verified", "item_id": str(item_id)}
            if expected is not None:
                item = await db.get(DatasetItem, item_id)
                binding = await db.get(StorageBinding, "datasets")
                await register_item_asset(
                    db, item, sha256=sha256, size_bytes=size, etag=etag, binding=binding
                )
                await AuditService.log(
                    db,
                    actor=None,
                    action="dataset_content.verify",
                    target_type="dataset_asset",
                    target_id=source.asset_id,
                    detail={"purpose": "legacy_current_baseline", **preview.public()},
                )
                await db.commit()
        except DBAPIError as exc:
            if storage_access.unavailable(exc):
                reject("busy")
            raise
    return {
        "status": "verified" if expected is not None else "preview",
        **preview.public(),
    }
