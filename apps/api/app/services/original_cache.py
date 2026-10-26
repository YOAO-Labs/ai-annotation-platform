"""Own rebuildable full-image copies, not business read authorization.

Callers must authorize and revalidate the exact reference before and after I/O.
Policy configuration and the unified resolver own public access decisions.
No source bytes, managed originals or training copies are ever deleted here.
"""

from contextlib import contextmanager
from dataclasses import dataclass, replace
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
    StorageLocation,
)
from app.db.models.dataset_source import DatasetSource
from app.db.models.original_cache import (
    OriginalCacheEntry as Entry,
    OriginalCacheReadLease as ReadLease,
)
from app.db.models.storage_maintenance import StorageOperation
from app.services import content_snapshot as snapshots
from app.services import media_cache_storage as saved_storage
from app.services import media_cache_accounting
from app.services import key_removal_accounting as removal
from app.services import storage_access
from app.services import storage_migration_artifact_reads as migrated_reads
from app.services import storage_migration_read_publications as read_publications
from app.services.dataset_assets import LEGACY_LOCATION_ID
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


def logical_prefix(record):
    return f"original-cache/v1/{record.asset_id}/{record.sha256}/{record.id}/"


def object_key(record):
    if getattr(record, "storage_ref", None) is not None:
        return saved_storage.resolve_ref(
            record.storage_ref, expected_prefix=logical_prefix(record)
        ).key("payload")
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


def _saved_owner(record):
    owner = saved_storage.resolve_ref(
        record.storage_ref, expected_prefix=logical_prefix(record)
    )
    if (
        owner.location_id,
        owner.bucket,
        owner.ref["binding_generation"],
        owner.ref["storage_identity"],
    ) != (
        record.location_id,
        record.bucket,
        record.binding_generation,
        record.storage_identity,
    ):
        raise saved_storage.MediaCacheStorageError()
    return owner


async def _legacy_target(db, record, *, require_enabled=True):
    # NULL is the historical A address, independent of today's binding.
    if record.location_id != LEGACY_LOCATION_ID or record.binding_generation != 1:
        raise saved_storage.MediaCacheStorageError()
    location = await db.get(StorageLocation, LEGACY_LOCATION_ID, populate_existing=True)
    target = replace(
        await saved_storage.target_for(db, location, require_enabled=require_enabled),
        bucket=record.bucket,
    )
    from app.services.managed_original_retirement import managed_storage_identity

    if (
        managed_storage_identity(target.endpoint_url, target.bucket)
        != record.storage_identity
    ):
        raise saved_storage.MediaCacheStorageError()
    return target


async def prepare_target(db, record, *, require_enabled=True):
    """Capture current credentials without changing saved source/quota facts."""
    if record.storage_ref is not None:
        _saved_owner(record)
        target = await saved_storage.target_for_ref(
            db, record.storage_ref, require_enabled=require_enabled
        )
    else:
        target = await _legacy_target(db, record, require_enabled=require_enabled)
    record._target = target
    record._target_maintenance_revision = await storage_access.check(
        db, record.location_id
    )
    return target


async def prepare_read(db, record):
    """Resolve only the saved cache owner, separately from its canonical scope."""
    if record.storage_ref is not None:
        ref = _saved_owner(record).ref
    else:
        target = await _legacy_target(db, record)
        ref = {
            "location_id": str(record.location_id),
            "endpoint_url": target.endpoint_url,
            "bucket": record.bucket,
        }
    route = await migrated_reads.resolve(
        db, ref, object_key(record), record.sha256, record.size_bytes
    )
    if route is None:
        await prepare_target(db, record)
    return route


@contextmanager
def physical_storage(record, *, owned=False):
    """Open only inside the caller's existing physical admission."""
    target = record._target
    if record.location_id == LEGACY_LOCATION_ID:
        try:
            storage = make_storage()
        except BaseException as exc:
            raise storage_access.mark_unsettled(exc)
        failure = None
        try:
            if storage.client.meta.endpoint_url.rstrip("/") != target.endpoint_url:
                raise saved_storage.MediaCacheStorageError()
            from types import SimpleNamespace

            yield SimpleNamespace(
                client=storage.client, media_cache_bucket=record.bucket
            )
        except BaseException as exc:
            failure = exc
            raise
        finally:
            try:
                storage_access.close_registered(storage.client)
            except BaseException:
                if failure is None:
                    raise
                storage_access.mark_unsettled(failure)
    else:
        from types import SimpleNamespace

        context = target.client(read_timeout=15, **({"owned": True} if owned else {}))
        try:
            client = context.__enter__()
        except BaseException as exc:
            raise storage_access.mark_unsettled(exc)
        failure = None
        try:
            yield SimpleNamespace(client=client, media_cache_bucket=record.bucket)
        except BaseException as exc:
            failure = exc
            raise
        finally:
            try:
                context.__exit__(None, None, None)
            except BaseException as exc:
                if failure is None:
                    raise storage_access.mark_unsettled(exc)
                storage_access.mark_unsettled(failure)


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
            location_id=LEGACY_LOCATION_ID,
            bucket="pending",
            binding_generation=1,
            storage_identity="0" * 64,
            state="uploading",
            uncertain_write=True,
            last_accessed_at=created,
            expires_at=created + timedelta(seconds=configured.ttl),
            writer_until=writer_until,
            retain_until=writer_until + timedelta(days=RECOVERY_DAYS),
            next_check_at=writer_until,
        )
        record.storage_ref = await saved_storage.allocate(db, logical_prefix(record))
        owner = saved_storage.resolve_ref(record.storage_ref)
        record.location_id = owner.location_id
        record.bucket = owner.bucket
        record.binding_generation = owner.ref["binding_generation"]
        record.storage_identity = owner.ref["storage_identity"]
        record._maintenance_revision = configured.maintenance_revision
        await prepare_target(db, record)
        db.add(record)
        await db.commit()  # Independent durable ownership before the first PUT.
        return record, "reserved"


@contextmanager
def stored_object(record, settled=None, *, observed=None):
    with _stored_object(
        record, physical_storage(record), settled, observed=observed
    ) as data:
        yield data


@contextmanager
def _routed_storage(record, admission):
    from types import SimpleNamespace

    context = migrated_reads.client(admission.route, admission.target)
    try:
        reader = context.__enter__()
    except BaseException as exc:
        raise storage_access.mark_unsettled(exc)
    failure = None
    try:
        yield SimpleNamespace(client=reader, media_cache_bucket=record.bucket)
    except BaseException as exc:
        failure = exc
        raise
    finally:
        try:
            context.__exit__(None, None, None)
        except BaseException as exc:
            if failure is None:
                raise storage_access.mark_unsettled(exc)
            storage_access.mark_unsettled(failure)


@contextmanager
def routed_object(record, admission, settled):
    """Keep real target closure inside the existing complete-image capture."""
    with _stored_object(record, _routed_storage(record, admission), settled) as data:
        yield data


@contextmanager
def _stored_object(record, context, settled=None, *, observed=None):
    body, failure = None, None
    try:
        with context as storage:
            try:
                response = storage.client.get_object(
                    Bucket=record.bucket, Key=object_key(record)
                )
                if observed is not None:
                    observed.append(response)
                body = response["Body"]
                if response.get("ContentLength") != record.size_bytes:
                    raise snapshots.ContentError("cache_content_mismatch")
                yield body, {"size": record.size_bytes}
            finally:
                if body is not None:
                    storage_access.close_registered(body)
    except BaseException as exc:
        failure = exc
        raise
    finally:
        if settled is not None and not getattr(
            failure, "storage_operation_unsettled", False
        ):
            settled.set()


def verify_stored(record):
    # The input Snapshot already proves image decoding and dimensions. Equality
    # of complete stored bytes needs no second snapshot/admission or decoder.
    digest, size = hashlib.sha256(), 0
    deadline = time.monotonic() + snapshots.COPY_SECONDS
    observed = []
    with stored_object(record, observed=observed) as (body, _):
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
    response = observed[0]
    return (
        response.get("VersionId")
        if response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 200
        and not response.get("DeleteMarker")
        and response.get("ContentRange") is None
        else None
    )


async def bound_destination(db, record):
    try:
        await prepare_target(db, record)
        return True
    except saved_storage.MediaCacheStorageError:
        return False


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
            select(
                select(ReadLease.id)
                .where(ReadLease.entry_id == identity, ReadLease.expires_at > now())
                .exists()
                | select(StorageOperation.id)
                .where(StorageOperation.purpose == read_purpose(identity))
                .exists()
            )
        )
    )


def read_purpose(identity):
    return "original_cache_read_" + uuid.UUID(str(identity)).hex


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
        target_valid = await bound_destination(db, current) if current else False
        if (
            not current
            or current.state != "uploading"
            or current.writer_until <= now()
            or current.expires_at <= now()
            or not matches(current, configured)
            or not target_valid
            or current.storage_ref != record.storage_ref
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
        await storage_access.check(
            db,
            record.location_id,
            expected_revision=record._target_maintenance_revision,
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
    record, confirmed_put = None, False
    recorder, accounting_settled = None, False
    try:
        record, status = await reserve(
            factory, source_id, asset_id, None, expected_revision=expected_revision
        )
        if record is None:
            return status
        if (record.sha256, record.size_bytes) != (snapshot.sha256, snapshot.size):
            await retire(factory, record.id, uncertain=False)
            return "content_mismatch"
        if record.writer_until <= now():
            await retire(factory, record.id, uncertain=False)
            return "expired"
        async with factory() as db:
            await prepare_target(db, record)
            recorder = media_cache_accounting.Recorder(
                factory,
                "original_cache",
                record.id,
                record._target.endpoint_url,
                record.bucket,
            )
            async with await storage_access.acquire(
                db,
                record.location_id,
                "original_cache_write",
                expected_revision=record._target_maintenance_revision,
            ):
                context = physical_storage(record)
                storage = await settled_io(context.__enter__)
                failure = None
                try:
                    snapshot.file.seek(0)
                    _, attempt = await settled_io(
                        media_cache_accounting.put,
                        recorder,
                        storage.client,
                        object_key(record),
                        {"sha256": record.sha256, "size_bytes": record.size_bytes},
                        Bucket=record.bucket,
                        Key=object_key(record),
                        Body=snapshot.file,
                        ContentLength=snapshot.size,
                        ContentType=snapshot.media_type,
                        IfNoneMatch="*",
                    )
                    confirmed_put = True
                    observed_version = await settled_io(verify_stored, record)
                    await settled_io(attempt.verified, storage.client, observed_version)
                except BaseException as exc:
                    failure = exc
                    raise
                finally:
                    try:
                        await settled_io(context.__exit__, None, None, None)
                        accounting_settled = not getattr(
                            failure, "storage_operation_unsettled", False
                        )
                    except BaseException:
                        if failure is None:
                            raise
                        storage_access.mark_unsettled(failure)
        if await publish(factory, record, None):
            return "stored"
        await retire(factory, record.id, uncertain=False)
        return "configuration_changed"
    except BaseException as exc:
        if record:
            try:
                await retire(
                    factory,
                    record.id,
                    uncertain=not confirmed_put
                    or getattr(exc, "storage_operation_unsettled", False),
                )
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
        try:
            if recorder is not None:
                await recorder.finish(settled=accounting_settled)
        finally:
            snapshot.file.seek(0)


def _read_facts(record):
    return (
        *(
            getattr(record, name)
            for name in (
                "id",
                "scope_location_id",
                "source_id",
                "asset_id",
                "sha256",
                "size_bytes",
                "source_revision",
                "policy_revision",
                "location_revision",
                "location_id",
                "bucket",
                "binding_generation",
                "storage_identity",
                "expires_at",
            )
        ),
        json.dumps(record.storage_ref, sort_keys=True),
    )


async def _current_read(db, record, facts, lease_id, configured, route):
    current = await db.get(Entry, record.id)
    lease = await db.get(ReadLease, lease_id)
    if (
        not current
        or current.state != "ready"
        or current.expires_at <= now()
        or _read_facts(current) != facts
        or not lease
        or lease.expires_at <= now()
        or await scope(db, record.source_id, record.asset_id) != configured
    ):
        return False
    locator = await db.get(
        DatasetAssetLocation, (record.asset_id, record.location_id, record.bucket)
    )
    if (
        not locator
        or locator.role != "original_cache"
        or locator.object_key != object_key(record)
        or locator.reference_state != "present"
    ):
        return False
    if route is not None:
        await read_publications.recheck(db, route, lock=True)
    elif not await bound_destination(db, current):
        return False
    return True


async def read(factory, source_id, asset_id, *, on_acquired=None):
    """Return only a newly verified complete snapshot; callers still authorize it."""
    lease_id, identity, snapshot = uuid.uuid4(), None, None
    settled = threading.Event()
    damaged = False
    admission_failed = False
    physical_started = False
    route, admission = None, None

    def open_stored():
        nonlocal physical_started
        physical_started = True
        return (
            stored_object(record, settled)
            if route is None
            else routed_object(record, admission, settled)
        )

    async def target_admitted(_permit):
        nonlocal admission_failed
        try:
            async with factory() as db:
                if not await _current_read(
                    db, record, facts, lease_id, configured, route
                ):
                    raise snapshots.ContentError("cache_read_changed")
                await db.commit()
        except BaseException:
            admission_failed = True
            settled.set()
            raise

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
            try:
                route = await prepare_read(db, record)
            except saved_storage.MediaCacheStorageError:
                return None
            facts = _read_facts(record)
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

                async def capture():
                    # C remains the canonical authority/quota scope. Only the
                    # saved cache transport is redirected from A to actual B.
                    return await snapshots.capture_from_location(
                        permit_db,
                        configured.location_id,
                        open_stored,
                        record.sha256,
                        expected_revision=configured.maintenance_revision,
                        purpose="original_cache_read",
                        on_acquired=admitted,
                    )

                if route is not None:
                    async with migrated_reads.admit(
                        permit_db,
                        route,
                        read_purpose(identity),
                        on_acquired=target_admitted,
                    ) as admission:
                        # Admission rechecks open a metadata transaction; no
                        # SQL owner or publication locks survive into SDK I/O.
                        await permit_db.commit()
                        snapshot = await capture()
                else:
                    async with await storage_access.acquire(
                        permit_db,
                        record.location_id,
                        read_purpose(identity),
                        expected_revision=record._target_maintenance_revision,
                    ):
                        snapshot = await capture()
        except snapshots.ContentError as exc:
            if not physical_started:
                settled.set()
            if (
                admission_failed
                or exc.status in {429, 504}
                or exc.code.startswith("storage_")
                or exc.code.startswith("read_publication_")
            ):
                # Admission/deadline pressure does not prove a corrupt object.
                raise
            damaged = True
            return None
        except Exception:
            if not physical_started:
                settled.set()
            if admission_failed:
                raise
            damaged = True
            return None
        async with factory() as db:
            if not await _current_read(db, record, facts, lease_id, configured, route):
                snapshot.close()
                snapshot = None
        return snapshot
    except BaseException:
        if not physical_started:
            settled.set()
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
    async with factory() as db:
        record = await db.scalar(
            select(Entry).where(Entry.id == identity).with_for_update(skip_locked=True)
        )
        if not record or record.next_check_at > now() or await readers(db, identity):
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
        try:
            await prepare_target(db, record, require_enabled=False)
            matched = True
        except saved_storage.MediaCacheStorageError:
            matched = False
        record.error_code = None if matched else "storage_identity_changed"
        await db.commit()
        if not matched:
            return "deferred"
    accounting, owned_permit, failure = None, None, None
    try:
        accounting = removal.Recorder(
            factory,
            "original_cache_removals",
            record.id,
            record._target.endpoint_url,
            record.bucket,
            purpose="media_cache",
        )
        async with factory() as permit_db:
            async with await storage_access.acquire(
                permit_db,
                record.location_id,
                "original_cache_cleanup",
                expected_revision=record._target_maintenance_revision,
            ) as owned_permit:

                def remove():
                    with physical_storage(record, owned=True) as storage:
                        key = object_key(record)
                        observation = removal.Observation(
                            accounting, storage.client, key
                        )
                        response = observation.submit(
                            lambda: storage.client.delete_object(
                                Bucket=record.bucket, Key=key
                            )
                        )
                        observation.complete(response)

                await settled_io(remove)
    except BaseException as exc:
        failure = exc
    if accounting is not None:
        failure = await removal.finish(
            accounting,
            failure,
            settled=getattr(owned_permit, "_settled", False) is True,
        )
    if failure is not None:
        if not isinstance(failure, Exception):
            raise failure
        async with factory() as db:
            current = await db.get(Entry, identity)
            if current:
                current.error_code = (
                    "storage_identity_changed"
                    if isinstance(failure, saved_storage.MediaCacheStorageError)
                    else "storage_cleanup_failed"
                )
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
