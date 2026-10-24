"""Freeze an original's relation and saved location without performing object I/O.

Callers own authorization, transaction boundaries and durable storage admission.
The snapshots deliberately contain no ORM instances or mutable JSON values.
"""

from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime
import re
from urllib.parse import urlsplit
import uuid

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from app.config import settings
from app.db.models.dataset import DatasetItem
from app.db.models.dataset_asset import (
    DatasetAsset,
    DatasetAssetLocation,
    StorageLocation,
)
from app.db.models.storage_maintenance import StorageMaintenanceGate
from app.db.models.task import Task
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.db.models.task_original import TaskOriginal
from app.services import storage_access, task_originals
from app.services.content_snapshot import ContentError
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.reference_keys import is_reference, reference_key

_SHA = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class LinkFact:
    id: uuid.UUID
    task_id: uuid.UUID
    item_id: uuid.UUID
    role: str
    sensor_name: str | None


@dataclass(frozen=True)
class TaskRelation:
    task_id: uuid.UUID
    project_id: uuid.UUID
    batch_id: uuid.UUID | None
    file_path: str
    file_type: str
    original_id: uuid.UUID | None
    direct_item_id: uuid.UUID | None
    primary_item_id: uuid.UUID | None
    primary_link_id: uuid.UUID | None
    item_id: uuid.UUID | None
    links: tuple[LinkFact, ...]


@dataclass(frozen=True)
class ItemFacts:
    id: uuid.UUID
    dataset_id: uuid.UUID
    file_path: str
    file_type: str
    width: int | None
    height: int | None


@dataclass(frozen=True)
class AssetFacts:
    id: uuid.UUID
    item_id: uuid.UUID
    media_type: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class ReplicaFacts:
    asset_id: uuid.UUID
    location_id: uuid.UUID
    bucket: str
    object_key: str
    role: str
    reference_state: str
    creation_id: uuid.UUID | None
    etag: str | None
    verified_at: datetime | None


@dataclass(frozen=True)
class LocationFacts:
    id: uuid.UUID
    kind: str
    enabled: bool
    config_revision: int
    endpoint: str
    use_ssl: bool
    connection_id: uuid.UUID | None


@dataclass(frozen=True)
class OriginalSource:
    owner_kind: str
    content_id: uuid.UUID
    sha256: str
    size_bytes: int
    media_type: str
    item_id: uuid.UUID | None
    relation: TaskRelation | None
    bucket: str | None
    object_key: str
    endpoint_url: str | None
    location_id: uuid.UUID | None
    location_revision: int | None
    maintenance_revision: int | None
    ownership_facts: object
    item_facts: ItemFacts | None
    replica_facts: ReplicaFacts | None
    location_facts: LocationFacts | None


@asynccontextmanager
async def _reads(db, lock):
    # Session.begin_nested() flushes even inside no_autoflush. A connection
    # savepoint recovers NOWAIT errors without flushing or ending caller work.
    with db.no_autoflush:
        if not lock:
            yield
            return
        connection = await db.connection()
        savepoint = await connection.begin_nested()
        try:
            yield
        except BaseException as exc:
            await savepoint.rollback()
            if isinstance(exc, DBAPIError) and (
                storage_access.unavailable(exc)
                or getattr(exc.orig, "sqlstate", None) == "40001"
                or getattr(exc.orig, "pgcode", None) == "40001"
            ):
                raise ContentError("storage_original_busy") from exc
            raise
        else:
            await savepoint.commit()


def _statement(statement, lock):
    if lock:
        statement = statement.with_for_update(read=True, nowait=True)
    return statement.execution_options(populate_existing=True)


async def _relation(db, task_id, lock):
    task = await db.scalar(_statement(select(Task).where(Task.id == task_id), lock))
    if task is None:
        raise ContentError("content_task_missing", 404)
    links = tuple(
        LinkFact(row.id, row.task_id, row.dataset_item_id, row.role, row.sensor_name)
        for row in await db.scalars(
            _statement(
                select(TaskDatasetItemLink)
                .where(TaskDatasetItemLink.task_id == task.id)
                .order_by(TaskDatasetItemLink.id),
                lock,
            )
        )
    )
    primary = tuple(link for link in links if link.role == "primary_lidar")
    if (
        len(primary) > 1
        or (task.original_id is not None and (task.dataset_item_id or links))
        or (
            task.dataset_item_id is not None
            and primary
            and task.dataset_item_id != primary[0].item_id
        )
    ):
        raise ContentError("original_relation_conflict")
    item_id = task.dataset_item_id or (primary[0].item_id if primary else None)
    if task.original_id is None and item_id is None:
        raise ContentError("no_readable_original")
    return TaskRelation(
        task.id,
        task.project_id,
        task.batch_id,
        task.file_path,
        task.file_type,
        task.original_id,
        task.dataset_item_id,
        primary[0].item_id if primary else None,
        primary[0].id if primary else None,
        item_id,
        links,
    )


async def resolve_task_relation(db, task_id, *, lock=False):
    """Resolve direct/primary/standalone ownership; never infer from a path."""
    async with _reads(db, lock):
        return await _relation(db, task_id, lock)


def _platform_location(location):
    config = {"endpoint": settings.minio_endpoint, "use_ssl": settings.minio_use_ssl}
    if (
        location is None
        or location.id != LEGACY_LOCATION_ID
        or location.enabled is not True
        or location.kind != "s3"
        or type(location.config_revision) is not int
        or location.config_revision != 1
        or location.config != config
        or type(location.config.get("use_ssl")) is not bool
    ):
        raise ContentError("location_unavailable")
    endpoint = f"{'https' if config['use_ssl'] else 'http'}://{config['endpoint']}"
    try:
        parsed = urlsplit(endpoint)
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.port == 0
            or "\\" in endpoint
            or not endpoint.isascii()
            or any(ord(char) <= 32 or ord(char) == 127 for char in endpoint)
        ):
            raise ValueError
    except ValueError:
        raise ContentError("location_unavailable") from None
    facts = LocationFacts(
        location.id,
        location.kind,
        location.enabled,
        location.config_revision,
        config["endpoint"],
        config["use_ssl"],
        location.connection_id,
    )
    return endpoint, facts


async def _gate(db, location_id, lock):
    gate = await db.scalar(
        _statement(
            select(StorageMaintenanceGate).where(
                StorageMaintenanceGate.location_id == location_id
            ),
            lock,
        )
    )
    # Admission creates a durable Gate before any physical operation. Metadata
    # lookup may see no row and follows storage_access.state's initial revision.
    return storage_access.validate(
        {"state": gate.state, "revision": gate.revision}
        if gate is not None
        else {"state": "active", "revision": 1},
        None,
    )


def _safe_locator(bucket, key):
    if (
        not isinstance(bucket, str)
        or not 0 < len(bucket) <= 255
        or not bucket.isascii()
        or any(ord(char) <= 32 or ord(char) == 127 for char in bucket)
        or any(char in bucket for char in "/\\?#@")
        or not isinstance(key, str)
        or not 0 < len(key) <= 1000
        or any(ord(char) < 32 or ord(char) == 127 for char in key)
    ):
        raise ContentError("no_readable_original")


async def _item(db, item_id, lock):
    item = await db.scalar(
        _statement(select(DatasetItem).where(DatasetItem.id == item_id), lock)
    )
    if item is None:
        raise ContentError("item_removed")
    asset = await db.scalar(
        _statement(
            select(DatasetAsset).where(DatasetAsset.dataset_item_id == item.id), lock
        )
    )
    if (
        asset is None
        or asset.verification_state != "verified"
        or asset.checksum_algorithm != "sha256"
        or not isinstance(asset.checksum_value, str)
        or _SHA.fullmatch(asset.checksum_value) is None
        or type(asset.size_bytes) is not int
        or not 0 <= asset.size_bytes <= 2**63 - 1
    ):
        raise ContentError("content_unverified")
    if item.file_type != asset.media_type:
        raise ContentError("content_binding_changed")
    item_facts = ItemFacts(
        item.id,
        item.dataset_id,
        item.file_path,
        item.file_type,
        item.width,
        item.height,
    )
    asset_facts = AssetFacts(
        asset.id, item.id, asset.media_type, asset.checksum_value, asset.size_bytes
    )
    if is_reference(item.file_path):
        if item.file_path != reference_key(item.id, asset.checksum_value):
            raise ContentError("content_binding_changed")
        return OriginalSource(
            "dataset_asset",
            asset.id,
            asset.checksum_value,
            asset.size_bytes,
            asset.media_type,
            item.id,
            None,
            None,
            item.file_path,
            None,
            None,
            None,
            None,
            asset_facts,
            item_facts,
            None,
            None,
        )
    # Location precedes replica locks, including when rejecting ambiguity. Do
    # not filter by today's purpose bucket or require a historical Creation.
    candidates = list(
        await db.scalars(
            _statement(
                select(DatasetAssetLocation)
                .where(
                    DatasetAssetLocation.asset_id == asset.id,
                    DatasetAssetLocation.role == "managed_original",
                    DatasetAssetLocation.reference_state == "present",
                )
                .order_by(
                    DatasetAssetLocation.location_id, DatasetAssetLocation.bucket
                ),
                False,
            )
        )
    )
    if not candidates:
        raise ContentError("no_readable_original")
    location_ids = sorted({row.location_id for row in candidates})
    locations = list(
        await db.scalars(
            _statement(
                select(StorageLocation)
                .where(StorageLocation.id.in_(location_ids))
                .order_by(StorageLocation.id),
                lock,
            )
        )
    )
    if lock:
        candidates = list(
            await db.scalars(
                _statement(
                    select(DatasetAssetLocation)
                    .where(
                        DatasetAssetLocation.asset_id == asset.id,
                        DatasetAssetLocation.role == "managed_original",
                        DatasetAssetLocation.reference_state == "present",
                    )
                    .order_by(
                        DatasetAssetLocation.location_id, DatasetAssetLocation.bucket
                    ),
                    True,
                )
            )
        )
    if len(candidates) != 1:
        raise ContentError("original_location_ambiguous")
    replica = candidates[0]
    if len(locations) != 1 or locations[0].id != replica.location_id:
        raise ContentError("location_unavailable")
    endpoint, location_facts = _platform_location(locations[0])
    if replica.object_key != item.file_path:
        raise ContentError("content_binding_changed")
    _safe_locator(replica.bucket, replica.object_key)
    revision = await _gate(db, replica.location_id, lock)
    replica_facts = ReplicaFacts(
        replica.asset_id,
        replica.location_id,
        replica.bucket,
        replica.object_key,
        replica.role,
        replica.reference_state,
        replica.creation_id,
        replica.etag,
        replica.verified_at,
    )
    return OriginalSource(
        "dataset_asset",
        asset.id,
        asset.checksum_value,
        asset.size_bytes,
        asset.media_type,
        item.id,
        None,
        replica.bucket,
        replica.object_key,
        endpoint,
        replica.location_id,
        locations[0].config_revision,
        revision,
        asset_facts,
        item_facts,
        replica_facts,
        location_facts,
    )


async def resolve_item(db, item_id, *, lock=False):
    """Freeze a verified DatasetItem source without granting dataset authority."""
    async with _reads(db, lock):
        return await _item(db, item_id, lock)


async def resolve_task(db, task_id, *, lock=False):
    """Freeze a TaskOriginal or the exact direct/primary DatasetItem source."""
    async with _reads(db, lock):
        relation = await _relation(db, task_id, lock)
        if relation.original_id is None:
            source = await _item(db, relation.item_id, lock)
            if (
                relation.file_path != source.object_key
                or relation.file_type != source.media_type
            ):
                raise ContentError("content_binding_changed")
            return replace(source, relation=relation)
        owner = await db.scalar(
            _statement(
                select(TaskOriginal).where(TaskOriginal.id == relation.original_id),
                lock,
            )
        )
        receipt = task_originals.snapshot(owner)
        if (
            receipt.task_id != relation.task_id
            or receipt.project_id != relation.project_id
            or receipt.object_key != relation.file_path
        ):
            raise ContentError("content_binding_changed")
        location = await db.scalar(
            _statement(
                select(StorageLocation).where(
                    StorageLocation.id == receipt.location_id
                ),
                lock,
            )
        )
        endpoint, location_facts = _platform_location(location)
        locator = task_originals.resolve(receipt, endpoint_url=endpoint)
        revision = await _gate(db, receipt.location_id, lock)
        return OriginalSource(
            "task_original",
            receipt.id,
            receipt.sha256,
            receipt.size_bytes,
            relation.file_type,
            None,
            relation,
            locator.bucket,
            locator.object_key,
            locator.endpoint_url,
            locator.location_id,
            location.config_revision,
            revision,
            receipt,
            None,
            None,
            location_facts,
        )
