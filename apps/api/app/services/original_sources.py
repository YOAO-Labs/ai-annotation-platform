"""Freeze an original's relation and saved location without performing object I/O.

Callers own authorization, transaction boundaries and durable storage admission.
The snapshots deliberately contain no ORM instances or mutable JSON values.
"""

from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from functools import partial
import re
from urllib.parse import urlsplit
import uuid

from sqlalchemy import Text, case, cast, func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

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
_MAX_ETAG_BYTES = 1024
_MAX_STORAGE_REF_BYTES = 8192


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


async def _relation(
    db,
    task_id,
    lock,
    *,
    allow_missing=False,
    compact_relation=False,
    link_id=None,
    max_links=None,
):
    # Scalar rows avoid rehydrating unrelated JSON or expiring the caller's
    # already-budgeted ORM state through populate_existing/load_only.
    task = (
        await db.execute(
            _statement(
                select(
                    Task.id,
                    Task.project_id,
                    Task.batch_id,
                    Task.file_path,
                    Task.file_type,
                    Task.original_id,
                    Task.dataset_item_id,
                ).where(Task.id == task_id),
                lock,
            )
        )
    ).one_or_none()
    if task is None:
        raise ContentError("content_task_missing", 404)
    statement = select(TaskDatasetItemLink).where(
        TaskDatasetItemLink.task_id == task.id
    )
    if compact_relation:
        selected = TaskDatasetItemLink.role == "primary_lidar"
        if link_id is not None:
            selected = selected | (TaskDatasetItemLink.id == link_id)
        statement = statement.where(selected)
    if max_links is not None:
        statement = statement.limit(max_links + 1)
    rows = list(
        await db.scalars(_statement(statement.order_by(TaskDatasetItemLink.id), lock))
    )
    if max_links is not None and len(rows) > max_links:
        raise ContentError("original_relation_budget_exceeded", 422)
    links = tuple(
        LinkFact(row.id, row.task_id, row.dataset_item_id, row.role, row.sensor_name)
        for row in rows
    )
    has_links = bool(links)
    if compact_relation and task.original_id is not None and not has_links:
        # A non-primary link still conflicts with an independent TaskOriginal.
        # EXISTS avoids hydrating unrelated camera facts in an object guard.
        has_links = bool(
            await db.scalar(
                select(
                    select(TaskDatasetItemLink.id)
                    .where(TaskDatasetItemLink.task_id == task.id)
                    .exists()
                )
            )
        )
    primary = tuple(link for link in links if link.role == "primary_lidar")
    if (
        len(primary) > 1
        or (task.original_id is not None and (task.dataset_item_id or has_links))
        or (
            task.dataset_item_id is not None
            and primary
            and task.dataset_item_id != primary[0].item_id
        )
    ):
        raise ContentError("original_relation_conflict")
    item_id = task.dataset_item_id or (primary[0].item_id if primary else None)
    if task.original_id is None and item_id is None and not allow_missing:
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


async def resolve_task_relation(db, task_id, *, lock=False, max_links=None):
    """Resolve direct/primary/standalone ownership; never infer from a path."""
    async with _reads(db, lock):
        return await _relation(db, task_id, lock, max_links=max_links)


def _platform_location(location, *, bounded=False):
    config = {"endpoint": settings.minio_endpoint, "use_ssl": settings.minio_use_ssl}
    if (
        location is None
        or location.id != LEGACY_LOCATION_ID
        or location.enabled is not True
        or location.kind != "s3"
        or type(location.config_revision) is not int
        or location.config_revision != 1
        or (
            location.config_matches is not True
            if bounded
            else location.config != config
            or type(location.config.get("use_ssl")) is not bool
        )
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


async def saved_location(
    db, location_id, storage_ref, *, lock=False, bounded=False, location=None
):
    """Resolve saved physical facts; live credentials never enter source facts."""
    if location is None:
        locations = await _locations(db, [location_id], lock, bounded=bounded)
        location = locations[0] if len(locations) == 1 else None
    if storage_ref is None:
        return _platform_location(location, bounded=bounded)
    from app.services import dataset_storage

    try:
        saved = dataset_storage.resolve_ref(storage_ref)
        if (
            saved.location_id != location_id
            or location is None
            or location.id != location_id
            or location.kind != "s3"
            or location.enabled is not True
            or type(location.config_revision) is not int
            or location.config_revision < 1
        ):
            raise ValueError
        # This captures metadata only. DNS, decryption and client construction
        # stay inside the caller's existing registered physical operation.
        await dataset_storage.target_for_ref(db, storage_ref)
        parsed = urlsplit(saved.endpoint_url)
        facts = LocationFacts(
            location.id,
            location.kind,
            location.enabled,
            saved.config_revision,
            parsed.netloc,
            parsed.scheme == "https",
            location.connection_id,
        )
    except ValueError:
        raise ContentError("location_unavailable") from None
    return saved.endpoint_url, facts


async def read_target(db, source):
    """Prepare a separate current-credential target outside business transactions."""
    from app.db.models.dataset_auxiliary_original import DatasetAuxiliaryOriginal
    from app.services import dataset_storage

    async with AsyncSession(
        bind=storage_access.engine(db), expire_on_commit=False, autoflush=False
    ) as owned:
        if source.location_id == LEGACY_LOCATION_ID:
            ref = dataset_storage.legacy_ref(source.bucket, source.object_key)
        else:
            if source.owner_kind == "dataset_asset":
                query = select(DatasetAssetLocation.storage_ref).where(
                    DatasetAssetLocation.asset_id == source.content_id,
                    DatasetAssetLocation.location_id == source.location_id,
                    DatasetAssetLocation.bucket == source.bucket,
                    DatasetAssetLocation.object_key == source.object_key,
                    DatasetAssetLocation.role == "managed_original",
                    DatasetAssetLocation.reference_state == "present",
                )
            elif source.owner_kind == "dataset_auxiliary":
                query = select(DatasetAuxiliaryOriginal.storage_ref).where(
                    DatasetAuxiliaryOriginal.id == source.content_id,
                    DatasetAuxiliaryOriginal.location_id == source.location_id,
                    DatasetAuxiliaryOriginal.bucket == source.bucket,
                    DatasetAuxiliaryOriginal.object_key == source.object_key,
                )
            else:
                raise ContentError("location_unavailable")
            ref = await owned.scalar(query)
            if ref is None:
                raise ContentError("location_unavailable")
        try:
            saved = dataset_storage.resolve_ref(ref)
            if (
                saved.location_id != source.location_id
                or saved.config_revision != source.location_revision
                or saved.endpoint_url != source.endpoint_url
                or saved.bucket != source.bucket
                or saved.object_key != source.object_key
            ):
                raise ValueError
            return await dataset_storage.target_for_ref(owned, ref)
        except ValueError:
            raise ContentError("location_unavailable") from None


async def read_options(db, source, *, read_timeout=30):
    """Keep legacy transport and old callback signatures unchanged."""
    if source.location_id == LEGACY_LOCATION_ID:
        return {}
    target = await read_target(db, source)
    return {"client_context": partial(target.client, read_timeout=read_timeout)}


async def _gate(db, location_id, lock, *, bounded=False):
    statement = (
        select(StorageMaintenanceGate.state, StorageMaintenanceGate.revision)
        if bounded
        else select(StorageMaintenanceGate)
    ).where(StorageMaintenanceGate.location_id == location_id)
    gate = (
        (await db.execute(_statement(statement, lock))).one_or_none()
        if bounded
        else await db.scalar(_statement(statement, lock))
    )
    # Admission creates a durable Gate before any physical operation. Metadata
    # lookup may see no row and follows storage_access.state's initial revision.
    return storage_access.validate(
        {"state": gate.state, "revision": gate.revision}
        if gate is not None
        else {"state": "active", "revision": 1},
        None,
    )


async def _locations(db, location_ids, lock, *, bounded=False):
    if bounded:
        # Exact JSONB equality rejects unknown keys and numeric "booleans"
        # without transferring a possibly unbounded configuration value.
        statement = select(
            StorageLocation.id,
            StorageLocation.kind,
            StorageLocation.enabled,
            StorageLocation.config_revision,
            StorageLocation.connection_id,
            (
                StorageLocation.config
                == {
                    "endpoint": settings.minio_endpoint,
                    "use_ssl": settings.minio_use_ssl,
                }
            ).label("config_matches"),
        ).limit(2)
    else:
        statement = select(StorageLocation)
    statement = statement.where(StorageLocation.id.in_(location_ids)).order_by(
        StorageLocation.id
    )
    if bounded:
        return list((await db.execute(_statement(statement, lock))).all())
    return list(await db.scalars(_statement(statement, lock)))


async def _replicas(db, asset_id, lock, *, bounded=False):
    if bounded:
        etag_bytes = func.octet_length(DatasetAssetLocation.etag)
        ref_bytes = func.octet_length(cast(DatasetAssetLocation.storage_ref, Text))
        statement = select(
            DatasetAssetLocation.asset_id,
            DatasetAssetLocation.location_id,
            DatasetAssetLocation.bucket,
            DatasetAssetLocation.object_key,
            DatasetAssetLocation.role,
            DatasetAssetLocation.reference_state,
            DatasetAssetLocation.creation_id,
            case(
                (ref_bytes <= _MAX_STORAGE_REF_BYTES, DatasetAssetLocation.storage_ref),
                else_=None,
            ).label("storage_ref"),
            (ref_bytes > _MAX_STORAGE_REF_BYTES).label("storage_ref_oversized"),
            DatasetAssetLocation.verified_at,
            case(
                (etag_bytes <= _MAX_ETAG_BYTES, DatasetAssetLocation.etag), else_=None
            ).label("etag"),
            (etag_bytes > _MAX_ETAG_BYTES).label("etag_oversized"),
        ).limit(2)
    else:
        statement = select(DatasetAssetLocation)
    statement = statement.where(
        DatasetAssetLocation.asset_id == asset_id,
        DatasetAssetLocation.role == "managed_original",
        DatasetAssetLocation.reference_state == "present",
    ).order_by(DatasetAssetLocation.location_id, DatasetAssetLocation.bucket)
    if bounded:
        return list((await db.execute(_statement(statement, lock))).all())
    return list(await db.scalars(_statement(statement, lock)))


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


async def _item(db, item_id, lock, *, bounded=False):
    item = (
        await db.execute(
            _statement(
                select(
                    DatasetItem.id,
                    DatasetItem.dataset_id,
                    DatasetItem.file_path,
                    DatasetItem.file_type,
                    DatasetItem.width,
                    DatasetItem.height,
                ).where(DatasetItem.id == item_id),
                lock,
            )
        )
    ).one_or_none()
    if item is None:
        raise ContentError("item_removed")
    statement = (
        select(
            DatasetAsset.id,
            DatasetAsset.media_type,
            DatasetAsset.checksum_algorithm,
            DatasetAsset.checksum_value,
            DatasetAsset.size_bytes,
            DatasetAsset.verification_state,
        )
        if bounded
        else select(DatasetAsset)
    ).where(DatasetAsset.dataset_item_id == item.id)
    asset = (
        (await db.execute(_statement(statement, lock))).one_or_none()
        if bounded
        else await db.scalar(_statement(statement, lock))
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
    candidates = await _replicas(db, asset.id, False, bounded=bounded)
    if not candidates:
        raise ContentError("no_readable_original")
    location_ids = sorted({row.location_id for row in candidates})
    locations = await _locations(db, location_ids, lock, bounded=bounded)
    if lock:
        candidates = await _replicas(db, asset.id, True, bounded=bounded)
    if len(candidates) != 1:
        raise ContentError("original_location_ambiguous")
    replica = candidates[0]
    if len(locations) != 1 or locations[0].id != replica.location_id:
        raise ContentError("location_unavailable")
    if bounded and (
        replica.etag_oversized is True or replica.storage_ref_oversized is True
    ):
        raise ContentError("original_source_budget_exceeded", 422)
    endpoint, location_facts = await saved_location(
        db,
        replica.location_id,
        replica.storage_ref,
        lock=lock,
        bounded=bounded,
        location=locations[0],
    )
    if replica.storage_ref is not None:
        from app.services.dataset_storage import resolve_ref

        saved = resolve_ref(replica.storage_ref)
        if saved.bucket != replica.bucket or saved.object_key != replica.object_key:
            raise ContentError("content_binding_changed")
    if replica.object_key != item.file_path:
        raise ContentError("content_binding_changed")
    _safe_locator(replica.bucket, replica.object_key)
    revision = await _gate(db, replica.location_id, lock, bounded=bounded)
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
        location_facts.config_revision,
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


async def _task_source(db, relation, lock, *, source=None, bounded=False):
    if relation.original_id is None:
        if relation.item_id is None:
            raise ContentError("no_readable_original")
        if source is None:
            source = await _item(
                db, relation.item_id, lock, **({"bounded": True} if bounded else {})
            )
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
    if bounded:
        locations = await _locations(db, [receipt.location_id], lock, bounded=True)
        location = locations[0] if locations else None
    else:
        location = await db.scalar(
            _statement(
                select(StorageLocation).where(
                    StorageLocation.id == receipt.location_id
                ),
                lock,
            )
        )
    endpoint, location_facts = _platform_location(location, bounded=bounded)
    locator = task_originals.resolve(receipt, endpoint_url=endpoint)
    revision = await _gate(db, receipt.location_id, lock, bounded=bounded)
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


async def resolve_task(db, task_id, *, lock=False, compact_relation=False):
    """Freeze a TaskOriginal or the exact direct/primary DatasetItem source.

    Compact guards retain only primary links; the default freezes every link.
    """
    async with _reads(db, lock):
        relation = await _relation(db, task_id, lock, compact_relation=compact_relation)
        return await _task_source(db, relation, lock)


def _camera_link(selected):
    if selected is None:
        raise ContentError("content_link_missing", 404)
    if not selected.role.startswith("camera_") or not selected.role[7:]:
        raise ContentError("original_link_invalid")
    return selected


def _camera_source(source, relation):
    if source.media_type != "image":
        raise ContentError("camera_original_not_image")
    return replace(source, relation=relation)


async def resolve_task_link(
    db, task_id, link_id, *, lock=False, compact_relation=False
):
    """Resolve an exact camera link without borrowing the primary file's path.

    A missing primary does not revoke an otherwise lawful linked camera. All
    Task/link facts still freeze together, including independent-owner and
    conflicting-primary checks. The primary resolver keeps its strict default.
    """
    async with _reads(db, lock):
        relation = await _relation(
            db,
            task_id,
            lock,
            allow_missing=True,
            compact_relation=compact_relation,
            link_id=link_id,
        )
        selected = _camera_link(
            next((link for link in relation.links if link.id == link_id), None)
        )
        source = await _item(db, selected.item_id, lock)
        return _camera_source(source, relation)


class CachedResolver:
    """Reuse frozen facts within one caller transaction and collect lifecycle.

    The caller must discard this resolver before committing or rolling back the
    transaction. Cache hits do not refresh authority or replace final rechecks.
    max_links optionally bounds all hydrated relation links across this collect.
    """

    def __init__(self, db, *, lock=False, max_links=None):
        if max_links is not None and (type(max_links) is not int or max_links < 0):
            raise ValueError("max_links must be a non-negative integer or None")
        self.db = db
        self.lock = lock
        self.max_links = max_links
        self._relations = {}
        self._links = {}
        self._items = {}
        self._tasks = {}

    def _remember_relation(self, relation):
        if relation.task_id not in self._relations:
            if (
                self.max_links is not None
                and len(self._links) + len(relation.links) > self.max_links
            ):
                raise ContentError("original_relation_budget_exceeded", 422)
            self._relations[relation.task_id] = relation
            self._links.update(
                ((relation.task_id, link.id), link) for link in relation.links
            )

    def _link_budget(self):
        if self.max_links is None:
            return {}
        return {"max_links": self.max_links - len(self._links)}

    async def task(self, task_id):
        async with _reads(self.db, self.lock):
            source = self._tasks.get(task_id)
            if source is None:
                relation = self._relations.get(task_id)
                if relation is None:
                    relation = await _relation(
                        self.db, task_id, self.lock, **self._link_budget()
                    )
                source = await _task_source(
                    self.db,
                    relation,
                    self.lock,
                    source=self._items.get(relation.item_id),
                    bounded=self.max_links is not None,
                )
        # Never retain observations whose savepoint failed or rolled back.
        self._remember_relation(source.relation)
        self._tasks[task_id] = source
        if source.item_id is not None:
            self._items[source.item_id] = replace(source, relation=None)
        return source

    async def link(self, task_id, link_id):
        async with _reads(self.db, self.lock):
            relation = self._relations.get(task_id)
            if relation is None:
                relation = await _relation(
                    self.db,
                    task_id,
                    self.lock,
                    allow_missing=True,
                    **self._link_budget(),
                )
                selected = next(
                    (link for link in relation.links if link.id == link_id), None
                )
            else:
                selected = self._links.get((task_id, link_id))
            selected = _camera_link(selected)
            source = self._items.get(selected.item_id)
            if source is None:
                source = await _item(
                    self.db,
                    selected.item_id,
                    self.lock,
                    **({"bounded": True} if self.max_links is not None else {}),
                )
            result = _camera_source(source, relation)
        self._remember_relation(relation)
        self._items[selected.item_id] = source
        return result
