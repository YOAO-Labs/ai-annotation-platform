"""Own rebuildable full-image copies, not business read authorization.

Callers must authorize and revalidate the exact reference before and after I/O.
Policy configuration and the unified resolver own public access decisions.
No source bytes, managed originals or training copies are ever deleted here.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import threading
import time
import uuid

from botocore.config import Config
from sqlalchemy import delete, func, select, text

from app.db.models.dataset import DatasetItem
from app.db.models.dataset_asset import (
    DatasetAsset,
    DatasetAssetLocation,
    StorageBinding,
    StorageLocation,
)
from app.db.models.dataset_source import DatasetSource
from app.db.models.original_cache import (
    OriginalCacheEntry as Entry,
    OriginalCacheReadLease as ReadLease,
)
from app.services import content_snapshot as snapshots
from app.services import storage_access
from app.services.dataset_assets import validate_legacy_bindings
from app.services.prediction_inputs import settled_io
from app.services.storage import StorageService

log = logging.getLogger(__name__)
WRITE_SECONDS = 120
READ_SECONDS = 60
CHECK_SECONDS = 60
RECOVERY_DAYS = 7
DEFAULT_TTL_SECONDS = 86400


def now():
    return datetime.now(timezone.utc)


def object_key(record):
    return f"original-cache/v1/{record.asset_id}/{record.sha256}/{record.id}"


def make_storage():
    return StorageService(
        client_config=Config(
            connect_timeout=3,
            read_timeout=15,
            retries={"total_max_attempts": 1, "mode": "standard"},
        )
    )


def storage_identity(storage):
    configuration = [
        storage.client.meta.endpoint_url.rstrip("/"),
        storage.media_cache_bucket,
    ]
    return hashlib.sha256(json.dumps(configuration).encode()).hexdigest()


@dataclass(frozen=True)
class Scope:
    location_id: uuid.UUID
    sha256: str
    size: int
    source_revision: int
    policy_revision: int
    location_revision: int
    quota: int
    ttl: int
    maintenance_revision: int = 1


async def scope(db, source_id, asset_id, *, lock=False, check_maintenance=True):
    """Internal policy ceiling; this deliberately does not replace actor checks."""
    source = await db.get(DatasetSource, source_id, populate_existing=True)
    asset = await db.get(DatasetAsset, asset_id, populate_existing=True)
    if not source or not asset:
        return None
    item = await db.get(DatasetItem, asset.dataset_item_id, populate_existing=True)
    location = await db.get(
        StorageLocation,
        source.location_id,
        populate_existing=True,
        with_for_update={"nowait": True} if lock else False,
    )
    if lock:
        source = await db.get(
            DatasetSource,
            source_id,
            populate_existing=True,
            with_for_update={"nowait": True},
        )
        if source is None:
            return None
    if (
        not item
        or item.dataset_id != source.dataset_id
        or not source.enabled
        or source.unregistered_at
        or source.ownership_mode != "reference"
        or source.cache_policy != "on_demand"
        or not location
        or not location.enabled
        or source.location_id != location.id
        or location.kind not in {"filesystem", "s3", "sftp"}
        or location.config.get("cache_policy") != "on_demand"
        or asset.verification_state != "verified"
        or asset.checksum_algorithm != "sha256"
        or asset.media_type != "image"
        or not asset.size_bytes
        or asset.size_bytes > snapshots.MAX_BYTES
    ):
        return None
    quota = location.config.get("original_cache_quota_bytes", 0)
    ttl = location.config.get("original_cache_ttl_seconds", DEFAULT_TTL_SECONDS)
    if (
        type(quota) is not int
        or not 0 < quota <= 2**50
        or type(ttl) is not int
        or not 60 <= ttl <= 30 * 86400
    ):
        return None
    return Scope(
        location.id,
        asset.checksum_value,
        asset.size_bytes,
        source.config_revision,
        source.policy_revision,
        location.config_revision,
        quota,
        ttl,
        await storage_access.check(db, location.id, lock=lock)
        if check_maintenance
        else 1,
    )


async def budget_lock(db, location_id):
    key = int.from_bytes(
        hashlib.sha256(b"original-cache:" + location_id.bytes).digest()[:8],
        "big",
        signed=True,
    )
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


async def usage(db, location_id):
    rows = (
        await db.execute(
            select(
                Entry.state,
                func.sum(Entry.size_bytes),
                func.count(),
                func.count().filter(Entry.error_code.is_not(None)),
                func.count().filter(Entry.uncertain_write.is_(True)),
            )
            .where(Entry.scope_location_id == location_id)
            .group_by(Entry.state)
        )
    ).all()
    return {
        state: {
            "bytes": int(size),
            "count": count,
            "failed_count": failed,
            "uncertain_count": uncertain,
        }
        for state, size, count, failed, uncertain in rows
    }


async def usage_snapshot(db, location):
    checked = now()
    states = await usage(db, location.id)
    return {
        "expected_revision": location.config_revision,
        "cache_policy": location.config.get("cache_policy", "preview"),
        "quota_bytes": location.config.get("original_cache_quota_bytes", 0),
        "ttl_seconds": location.config.get(
            "original_cache_ttl_seconds", DEFAULT_TTL_SECONDS
        ),
        "charged_bytes": sum(state["bytes"] for state in states.values()),
        **{
            state: states.get(state, {"count": 0, "bytes": 0})
            for state in ("uploading", "ready", "deleting")
        },
        "failed_count": sum(state["failed_count"] for state in states.values()),
        "uncertain_count": sum(state["uncertain_count"] for state in states.values()),
        "measured_at": checked,
    }


async def request_cleanup(db, location, *, recorded_before, limit):
    """Seal a bounded set in the caller's authorization transaction; never delete I/O."""
    candidates = list(
        await db.scalars(
            select(Entry)
            .where(
                Entry.scope_location_id == location.id,
                Entry.state == "ready",
                Entry.verified_at <= recorded_before,
            )
            .order_by(Entry.last_accessed_at, Entry.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    retired, leased = 0, 0
    for record in candidates:
        if await readers(db, record.id):
            leased += 1
            continue
        record.state, record.next_check_at = "deleting", now()
        await detach(db, record)
        retired += 1
    await db.flush()
    remaining = await db.scalar(
        select(func.count())
        .select_from(Entry)
        .where(Entry.scope_location_id == location.id, Entry.state == "ready")
    )
    return {
        "retired_count": retired,
        "skipped_leased_count": leased,
        "remaining_ready_count": remaining,
        "usage": await usage_snapshot(db, location),
    }


async def charged_bytes(db, location_id):
    return int(
        await db.scalar(
            select(func.coalesce(func.sum(Entry.size_bytes), 0)).where(
                Entry.scope_location_id == location_id
            )
        )
    )


async def destination(db, storage):
    if await validate_legacy_bindings(db):
        return None
    binding = await db.get(StorageBinding, "media-cache", populate_existing=True)
    if binding and binding.bucket == storage.media_cache_bucket:
        return binding
    return None


def matches(record, configured):
    return configured is not None and (
        record.scope_location_id,
        record.sha256,
        record.size_bytes,
        record.source_revision,
        record.policy_revision,
        record.location_revision,
    ) == (
        configured.location_id,
        configured.sha256,
        configured.size,
        configured.source_revision,
        configured.policy_revision,
        configured.location_revision,
    )


async def reserve(factory, source_id, asset_id, storage, *, expected_revision=None):
    async with factory() as db:
        configured = await scope(db, source_id, asset_id)
        if configured is None:
            return None, "disabled"
        if expected_revision is not None:
            await storage_access.check(
                db,
                configured.location_id,
                expected_revision=expected_revision,
            )
        await budget_lock(db, configured.location_id)
        # Re-read after waiting for another reservation's transaction.
        if await scope(db, source_id, asset_id) != configured:
            return None, "configuration_changed"
        binding = await destination(db, storage)
        if binding is None:
            return None, "storage_binding_changed"
        existing = await db.scalar(
            select(Entry).where(
                Entry.asset_id == asset_id, Entry.state.in_(["uploading", "ready"])
            )
        )
        if existing:
            return None, "exists" if existing.state == "ready" else "filling"
        if configured.size > configured.quota:
            return None, "quota_too_small"
        if (
            await charged_bytes(db, configured.location_id) + configured.size
            > configured.quota
        ):
            return None, "quota_full"
        created = now()
        writer_until = created + timedelta(seconds=WRITE_SECONDS)
        record = Entry(
            id=uuid.uuid4(),
            scope_location_id=configured.location_id,
            source_id=source_id,
            asset_id=asset_id,
            sha256=configured.sha256,
            size_bytes=configured.size,
            source_revision=configured.source_revision,
            policy_revision=configured.policy_revision,
            location_revision=configured.location_revision,
            location_id=binding.location_id,
            bucket=binding.bucket,
            binding_generation=binding.generation,
            storage_identity=storage_identity(storage),
            state="uploading",
            uncertain_write=True,
            last_accessed_at=created,
            expires_at=created + timedelta(seconds=configured.ttl),
            writer_until=writer_until,
            retain_until=writer_until + timedelta(days=RECOVERY_DAYS),
            next_check_at=writer_until,
        )
        record._maintenance_revision = configured.maintenance_revision
        db.add(record)
        await db.commit()  # Independent durable ownership before the first PUT.
        return record, "reserved"


@contextmanager
def stored_object(record, settled=None):
    storage = make_storage()
    body = None
    try:
        if storage_identity(storage) != record.storage_identity:
            raise snapshots.ContentError("cache_storage_identity_changed")
        response = storage.client.get_object(
            Bucket=record.bucket, Key=object_key(record)
        )
        body = response["Body"]
        if response.get("ContentLength") != record.size_bytes:
            raise snapshots.ContentError("cache_content_mismatch")
        yield body, {"size": record.size_bytes}
    finally:
        closed = False
        try:
            if body is not None:
                storage_access.close_registered(body)
            closed = True
        finally:
            storage_access.close_registered(storage.client)
            if closed and settled is not None:
                settled.set()


def verify_stored(record):
    # The input Snapshot already proves image decoding and dimensions. Equality
    # of complete stored bytes needs no second snapshot/admission or decoder.
    digest, size = hashlib.sha256(), 0
    deadline = time.monotonic() + snapshots.COPY_SECONDS
    with stored_object(record) as (body, _):
        while True:
            if time.monotonic() > deadline:
                raise snapshots.ContentError("cache_verify_timeout", 504)
            chunk = body.read(snapshots.CHUNK_BYTES)
            if not chunk:
                break
            size += len(chunk)
            if size > record.size_bytes:
                raise snapshots.ContentError("cache_content_mismatch")
            digest.update(chunk)
    if size != record.size_bytes or digest.hexdigest() != record.sha256:
        raise snapshots.ContentError("cache_content_mismatch")


async def bound_destination(db, record):
    storage = make_storage()
    try:
        binding = await destination(db, storage)
        return bool(
            binding
            and (binding.location_id, binding.bucket, binding.generation)
            == (record.location_id, record.bucket, record.binding_generation)
            and storage_identity(storage) == record.storage_identity
        )
    finally:
        storage.client.close()


async def detach(db, record):
    # A retired generation cannot remove a newer locator or another role.
    await db.execute(
        delete(DatasetAssetLocation).where(
            DatasetAssetLocation.asset_id == record.asset_id,
            DatasetAssetLocation.location_id == record.location_id,
            DatasetAssetLocation.bucket == record.bucket,
            DatasetAssetLocation.object_key == object_key(record),
            DatasetAssetLocation.role == "original_cache",
        )
    )


async def readers(db, identity):
    return bool(
        await db.scalar(
            select(ReadLease.id)
            .where(ReadLease.entry_id == identity, ReadLease.expires_at > now())
            .limit(1)
        )
    )


async def retire(factory, identity, *, uncertain=None):
    async with factory() as db:
        record = await db.scalar(
            select(Entry).where(Entry.id == identity).with_for_update()
        )
        if record is None:
            return False
        if await readers(db, identity):
            return False
        record.state = "deleting"
        if uncertain is not None:
            record.uncertain_write = uncertain
        record.next_check_at = now()
        await detach(db, record)
        await db.commit()
        return True


async def publish(factory, record, storage):
    async with factory() as db:
        # Retention is optional; a caller may itself own a business row lock.
        # Never wait indefinitely for that same caller to finish its transaction.
        await db.execute(text("SET LOCAL lock_timeout = '1s'"))
        configured = await scope(db, record.source_id, record.asset_id, lock=True)
        current = await db.scalar(
            select(Entry).where(Entry.id == record.id).with_for_update()
        )
        binding = await destination(db, storage)
        if (
            not current
            or current.state != "uploading"
            or current.writer_until <= now()
            or current.expires_at <= now()
            or not matches(current, configured)
            or not binding
            or (binding.location_id, binding.bucket, binding.generation)
            != (record.location_id, record.bucket, record.binding_generation)
            or storage_identity(storage) != record.storage_identity
        ):
            return False
        if await charged_bytes(db, configured.location_id) > configured.quota:
            return False
        locator = await db.get(
            DatasetAssetLocation, (record.asset_id, record.location_id, record.bucket)
        )
        # Never replace a managed original or an unowned existing locator.
        if locator is not None:
            return False
        checked = now()
        db.add(
            DatasetAssetLocation(
                asset_id=record.asset_id,
                location_id=record.location_id,
                bucket=record.bucket,
                object_key=object_key(record),
                role="original_cache",
                reference_state="present",
                verified_at=checked,
            )
        )
        current.state = "ready"
        current.uncertain_write = False
        current.verified_at = checked
        current.next_check_at = min(
            current.expires_at, checked + timedelta(seconds=CHECK_SECONDS)
        )
        await storage_access.check(
            db,
            record.scope_location_id,
            expected_revision=getattr(record, "_maintenance_revision", 1),
            lock=True,
        )
        await db.commit()
        return True


async def store(factory, source_id, asset_id, snapshot, *, expected_revision=None):
    """Best-effort retention; the caller retains ownership of its verified snapshot."""
    if (
        not isinstance(snapshot, snapshots.Snapshot)
        or snapshot._closed
        or snapshot.inspection_error
    ):
        raise snapshots.ContentError("cache_snapshot_invalid")
    storage, record, confirmed_put = make_storage(), None, False
    try:
        record, status = await reserve(
            factory, source_id, asset_id, storage, expected_revision=expected_revision
        )
        if record is None:
            return status
        if (record.sha256, record.size_bytes) != (snapshot.sha256, snapshot.size):
            await retire(factory, record.id, uncertain=False)
            return "content_mismatch"
        if record.writer_until <= now():
            await retire(factory, record.id, uncertain=False)
            return "expired"
        snapshot.file.seek(0)
        await settled_io(
            storage.client.put_object,
            Bucket=record.bucket,
            Key=object_key(record),
            Body=snapshot.file,
            ContentLength=snapshot.size,
            ContentType=snapshot.media_type,
        )
        confirmed_put = True
        # Verify actual stored bytes, not a successful PUT or its opaque ETag.
        await settled_io(verify_stored, record)
        if await publish(factory, record, storage):
            return "stored"
        await retire(factory, record.id, uncertain=False)
        return "configuration_changed"
    except BaseException as exc:
        if record:
            try:
                await retire(factory, record.id, uncertain=not confirmed_put)
            except Exception:
                log.warning("Original cache recovery deferred entry=%s", record.id)
        if not isinstance(exc, Exception) or (
            isinstance(exc, snapshots.ContentError) and exc.code.startswith("storage_")
        ):
            raise
        log.warning(
            "Original cache fill deferred entry=%s", record.id if record else None
        )
        return "fill_failed"
    finally:
        snapshot.file.seek(0)
        storage.client.close()


async def read(factory, source_id, asset_id, *, on_acquired=None):
    """Return only a newly verified complete snapshot; callers still authorize it."""
    lease_id, identity, snapshot = uuid.uuid4(), None, None
    settled = threading.Event()
    damaged = False
    admission_failed = False

    async def admitted():
        nonlocal admission_failed
        if on_acquired is not None:
            try:
                await on_acquired()
            except BaseException:
                admission_failed = True
                # No physical stream was started; its temporary read lease can
                # be removed without waiting for an SDK handle to settle.
                settled.set()
                raise

    try:
        async with factory() as db:
            configured = await scope(db, source_id, asset_id)
            if configured is None:
                return None
            record = await db.scalar(
                select(Entry)
                .where(
                    Entry.asset_id == asset_id,
                    Entry.source_id == source_id,
                    Entry.state == "ready",
                )
                .with_for_update()
            )
            if (
                not record
                or record.expires_at <= now()
                or not matches(record, configured)
            ):
                return None
            locator = await db.get(
                DatasetAssetLocation, (asset_id, record.location_id, record.bucket)
            )
            if (
                not locator
                or locator.role != "original_cache"
                or locator.object_key != object_key(record)
                or locator.reference_state != "present"
            ):
                return None
            if not await bound_destination(db, record):
                return None
            identity = record.id
            checked = now()
            db.add(
                ReadLease(
                    id=lease_id,
                    entry_id=identity,
                    expires_at=checked + timedelta(seconds=READ_SECONDS),
                )
            )
            record.last_accessed_at = checked
            await db.commit()
        try:
            async with factory() as permit_db:
                snapshot = await snapshots.capture_from_location(
                    permit_db,
                    configured.location_id,
                    lambda: stored_object(record, settled),
                    record.sha256,
                    expected_revision=configured.maintenance_revision,
                    purpose="original_cache_read",
                    on_acquired=admitted,
                )
        except snapshots.ContentError as exc:
            if (
                admission_failed
                or exc.status in {429, 504}
                or exc.code.startswith("storage_")
            ):
                # Admission/deadline pressure does not prove a corrupt object.
                raise
            damaged = True
            return None
        except Exception:
            if admission_failed:
                raise
            damaged = True
            return None
        async with factory() as db:
            current = await db.get(Entry, identity)
            lease = await db.get(ReadLease, lease_id)
            if (
                not current
                or current.state != "ready"
                or current.expires_at <= now()
                or not lease
                or lease.expires_at <= now()
                or await scope(db, source_id, asset_id) != configured
                or not await bound_destination(db, current)
            ):
                snapshot.close()
                snapshot = None
        return snapshot
    except BaseException:
        if snapshot:
            snapshot.close()
        raise
    finally:
        # Bounded capture now waits for the SDK thread and handle to settle.
        if identity and settled.is_set():
            try:
                async with factory() as db:
                    await db.execute(delete(ReadLease).where(ReadLease.id == lease_id))
                    await db.commit()
                if damaged:
                    await retire(factory, identity)
            except BaseException:
                if snapshot:
                    snapshot.close()
                raise


async def evict_lru(factory, location_id):
    """Seal one unleased ready generation. Deletion never runs under quota locks."""
    async with factory() as db:
        await budget_lock(db, location_id)
        leased = select(ReadLease.entry_id).where(ReadLease.expires_at > now())
        record = await db.scalar(
            select(Entry)
            .where(
                Entry.scope_location_id == location_id,
                Entry.state == "ready",
                Entry.id.not_in(leased),
            )
            .order_by(Entry.last_accessed_at, Entry.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if not record:
            return None
        if await readers(db, record.id):
            return None
        record.state, record.next_check_at = "deleting", now()
        await detach(db, record)
        await db.commit()
        return record.id


async def purge(factory, identity):
    storage = make_storage()
    try:
        async with factory() as db:
            record = await db.scalar(
                select(Entry)
                .where(Entry.id == identity)
                .with_for_update(skip_locked=True)
            )
            if (
                not record
                or record.next_check_at > now()
                or await readers(db, identity)
            ):
                return "skipped"
            await db.execute(
                delete(ReadLease).where(
                    ReadLease.entry_id == identity, ReadLease.expires_at <= now()
                )
            )
            if record.state == "uploading" and record.writer_until > now():
                return "skipped"
            if record.state == "ready":
                configured = await scope(
                    db, record.source_id, record.asset_id, check_maintenance=False
                )
                total = await charged_bytes(db, record.scope_location_id)
                if (
                    record.expires_at > now()
                    and matches(record, configured)
                    and total <= configured.quota
                ):
                    record.next_check_at = min(
                        record.expires_at, now() + timedelta(seconds=CHECK_SECONDS)
                    )
                    await db.commit()
                    return "skipped"
            record.state = "deleting"
            await detach(db, record)
            record.next_check_at = now() + timedelta(seconds=CHECK_SECONDS)
            matched = (
                record.storage_identity == storage_identity(storage)
                and record.bucket == storage.media_cache_bucket
            )
            record.error_code = None if matched else "storage_identity_changed"
            await db.commit()
            if not matched:
                return "deferred"
        try:
            await settled_io(
                storage.client.delete_object,
                Bucket=record.bucket,
                Key=object_key(record),
            )
        except Exception:
            async with factory() as db:
                current = await db.get(Entry, identity)
                if current:
                    current.error_code = "storage_cleanup_failed"
                    await db.commit()
            log.warning("Original cache deletion deferred entry=%s", identity)
            return "deferred"
        async with factory() as db:
            current = await db.scalar(
                select(Entry).where(Entry.id == identity).with_for_update()
            )
            if current:
                if current.uncertain_write and current.retain_until > now():
                    current.error_code = None
                    await db.commit()
                    return "retained"
                await db.delete(current)
                await db.commit()
        return "removed"
    finally:
        storage.client.close()


async def maintain(factory, *, limit=50):
    async with factory() as db:
        ids = list(
            await db.scalars(
                select(Entry.id)
                .where(Entry.next_check_at <= now())
                .order_by(Entry.next_check_at, Entry.last_accessed_at, Entry.id)
                .limit(limit)
            )
        )
    counts = {"removed": 0, "retained": 0, "deferred": 0, "skipped": 0}
    for identity in ids:
        try:
            counts[await purge(factory, identity)] += 1
        except Exception:
            counts["deferred"] += 1
            log.warning("Original cache maintenance deferred entry=%s", identity)
    return counts
