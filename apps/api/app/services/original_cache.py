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
from types import SimpleNamespace
import uuid

from botocore.config import Config
from sqlalchemy import text

from app.db.models.original_cache import OriginalCacheEntry as Entry
from app.services import content_snapshot as snapshots
from app.services import media_cache_storage as saved_storage
from app.services import media_cache_accounting
from app.services import key_removal_accounting as removal
from app.services import storage_access, storage_registry_data
from app.services import storage_migration_artifact_reads as migrated_reads
from app.services import storage_migration_read_publications as read_publications
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.prediction_inputs import settled_io
from app.services.storage import StorageService

log = logging.getLogger(__name__)
READ_SECONDS = 15
WRITE_SECONDS = 120
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
            read_timeout=READ_SECONDS,
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
    facts = (
        await db.execute(
            text(
                "SELECT * FROM yoao_assets_v1.original_cache_scope(:source, :asset, :lock)"
            ),
            {"source": source_id, "asset": asset_id, "lock": lock},
        )
    ).one_or_none()
    if facts is None or facts.size_bytes > snapshots.MAX_BYTES:
        return None
    quota, ttl = facts.cache_quota, facts.cache_ttl
    if (
        type(quota) is not int
        or not 0 < quota <= 2**50
        or type(ttl) is not int
        or not 60 <= ttl <= 30 * 86400
    ):
        return None
    return Scope(
        facts.location_id,
        facts.sha256,
        facts.size_bytes,
        facts.source_revision,
        facts.policy_revision,
        facts.location_revision,
        quota,
        ttl,
        await storage_access.check(db, facts.location_id, lock=lock)
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
            text("SELECT * FROM yoao_assets_v1.original_cache_usage(:location)"),
            {"location": location_id},
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
    retired, leased, remaining = (
        await db.execute(
            text(
                "SELECT * FROM yoao_assets_v1.retire_original_cache_page(:location, :before, :checked, :limit)"
            ),
            {
                "location": location.id,
                "before": recorded_before,
                "checked": now(),
                "limit": limit,
            },
        )
    ).one()
    return {
        "retired_count": retired,
        "skipped_leased_count": leased,
        "remaining_ready_count": remaining,
        "usage": await usage_snapshot(db, location),
    }


async def charged_bytes(db, location_id):
    return int(
        await db.scalar(
            text("SELECT yoao_assets_v1.original_cache_charge(:location)"),
            {"location": location_id},
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
    if record.location_id != LEGACY_LOCATION_ID or record.binding_generation != 1:
        raise saved_storage.MediaCacheStorageError()
    location = await storage_registry_data.get_location(db, LEGACY_LOCATION_ID)
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

        context = target.client(
            read_timeout=READ_SECONDS, **({"owned": True} if owned else {})
        )
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
        if await scope(db, source_id, asset_id) != configured:
            return None, "configuration_changed"
        existing = await db.scalar(
            text("SELECT yoao_assets_v1.original_cache_current_state(:asset)"),
            {"asset": asset_id},
        )
        if existing:
            return None, "exists" if existing == "ready" else "filling"
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
        record.created_at = await db.scalar(
            text(
                "SELECT yoao_assets_v1.reserve_original_cache_generation(CAST(:request AS jsonb))"
            ),
            {
                "request": json.dumps(
                    {
                        field: getattr(record, field)
                        for field in (
                            "id",
                            "scope_location_id",
                            "source_id",
                            "asset_id",
                            "sha256",
                            "size_bytes",
                            "source_revision",
                            "policy_revision",
                            "location_revision",
                            "storage_ref",
                            "last_accessed_at",
                            "expires_at",
                            "writer_until",
                            "retain_until",
                        )
                    },
                    default=str,
                )
            },
        )
        await db.commit()
        return record, "reserved"


@contextmanager
def registered_object(record, lease_id, permit, settled):
    from app.services import saved_media_io

    failure = None
    try:
        payload = saved_media_io.subject(record.id, permit, lease=str(lease_id))
        with saved_media_io.download(
            "original-cache-read", payload, timeout=READ_SECONDS
        ) as response:
            if response.metadata.get("ContentLength") != record.size_bytes:
                raise snapshots.ContentError("cache_content_mismatch")
            yield response, {"size": record.size_bytes}
    except BaseException as exc:
        failure = exc
        raise
    finally:
        if not storage_access.failure_unsettled(failure):
            settled.set()


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


async def _cache_entry(db, identity, *, lock="read"):
    row = (
        (
            await db.execute(
                text(
                    "SELECT * FROM yoao_assets_v1.original_cache_entry_facts(:entry, :lock)"
                ),
                {"entry": identity, "lock": lock},
            )
        )
        .mappings()
        .one_or_none()
    )
    return SimpleNamespace(**row) if row is not None else None


async def readers(db, identity):
    return await db.scalar(
        text("SELECT yoao_assets_v1.original_cache_has_readers(:entry, :checked)"),
        {"entry": identity, "checked": now()},
    )


def read_purpose(identity):
    return "original_cache_read_" + uuid.UUID(str(identity)).hex


async def retire(factory, identity, *, uncertain=None):
    async with factory() as db:
        checked = now()
        if not await db.scalar(
            text(
                "SELECT yoao_assets_v1.retire_original_cache_generation(:entry, :checked, :checked, :uncertain)"
            ),
            {"entry": identity, "checked": checked, "uncertain": uncertain},
        ):
            return False
        await db.commit()
        return True


async def publish(factory, record, storage):
    async with factory() as db:
        await db.execute(text("SET LOCAL lock_timeout = '1s'"))
        configured = await scope(db, record.source_id, record.asset_id, lock=True)
        current = await _cache_entry(db, record.id, lock="update")
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
        if not await db.scalar(
            text(
                "SELECT yoao_assets_v1.publish_original_cache_generation(:entry, :checked)"
            ),
            {"entry": current.id, "checked": now()},
        ):
            return False
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
    from app.services import saved_media_io

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
            registered = saved_media_io.registered(record._target)
            async with await storage_access.acquire(
                db,
                record.location_id,
                f"original_cache_write_{record.id.hex}"
                if registered
                else "original_cache_write",
                expected_revision=record._target_maintenance_revision,
            ) as permit:
                if registered:
                    snapshot.file.seek(0)
                    await settled_io(
                        saved_media_io.write_checked,
                        "original-cache-write",
                        saved_media_io.subject(
                            record.id, permit, content_type=snapshot.media_type
                        ),
                        snapshot.file,
                        object_key(record),
                        {"sha256": record.sha256, "size_bytes": record.size_bytes},
                        accounting=recorder,
                        timeout=WRITE_SECONDS,
                    )
                    confirmed_put = accounting_settled = True
                else:
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
                        await settled_io(
                            attempt.verified, storage.client, observed_version
                        )
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
    current = await _cache_entry(db, record.id)
    if (
        not current
        or current.state != "ready"
        or current.expires_at <= now()
        or _read_facts(current) != facts
        or not await db.scalar(
            text(
                "SELECT yoao_assets_v1.original_cache_lease_current(:entry, :lease, :checked)"
            ),
            {"entry": record.id, "lease": lease_id, "checked": now()},
        )
        or await scope(db, record.source_id, record.asset_id) != configured
    ):
        return False
    if not await db.scalar(
        text("SELECT yoao_assets_v1.original_cache_locator_current(:entry)"),
        {"entry": record.id},
    ):
        return False
    if route is not None:
        await read_publications.recheck(db, route, lock=True)
    elif not await bound_destination(db, current):
        return False
    return True


async def read(factory, source_id, asset_id, *, on_acquired=None):
    """Return only a newly verified complete snapshot; callers still authorize it."""
    from app.services import saved_media_io

    lease_id, identity, snapshot = uuid.uuid4(), None, None
    settled = threading.Event()
    damaged = False
    admission_failed = False
    physical_started = False
    route, admission = None, None
    target_permit = None

    def open_stored():
        nonlocal physical_started
        physical_started = True
        if route is None and saved_media_io.registered(record._target):
            return registered_object(record, lease_id, target_permit, settled)
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
                settled.set()
                raise

    try:
        async with factory() as db:
            configured = await scope(db, source_id, asset_id)
            if configured is None:
                return None
            row = (
                (
                    await db.execute(
                        text(
                            "SELECT * FROM yoao_assets_v1.lock_original_cache_read(:source, :asset)"
                        ),
                        {"source": source_id, "asset": asset_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
            record = SimpleNamespace(**row) if row is not None else None
            if (
                not record
                or record.expires_at <= now()
                or not matches(record, configured)
            ):
                return None
            if not await db.scalar(
                text("SELECT yoao_assets_v1.original_cache_locator_current(:entry)"),
                {"entry": record.id},
            ):
                return None
            try:
                route = await prepare_read(db, record)
            except saved_storage.MediaCacheStorageError:
                return None
            facts = _read_facts(record)
            identity = record.id
            if not await db.scalar(
                text(
                    "SELECT yoao_assets_v1.begin_original_cache_read(:entry, :lease, :checked)"
                ),
                {"entry": identity, "lease": lease_id, "checked": now()},
            ):
                return None
            await db.commit()
        try:
            async with factory() as permit_db:

                async def capture():
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
                        await permit_db.commit()
                        snapshot = await capture()
                else:
                    async with await storage_access.acquire(
                        permit_db,
                        record.location_id,
                        read_purpose(identity),
                        expected_revision=record._target_maintenance_revision,
                    ) as target_permit:
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
        if identity and settled.is_set():
            try:
                async with factory() as db:
                    await db.execute(
                        text(
                            "SELECT yoao_assets_v1.end_original_cache_read(:entry, :lease)"
                        ),
                        {"entry": identity, "lease": lease_id},
                    )
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
        identity = await db.scalar(
            text(
                "SELECT yoao_assets_v1.evict_original_cache_generation(:location, :checked)"
            ),
            {"location": location_id, "checked": now()},
        )
        if identity is None:
            return None
        await db.commit()
        return identity


async def purge(factory, identity):
    from app.services import saved_media_io

    async with factory() as db:
        record = await _cache_entry(db, identity, lock="skip")
        if not record or record.next_check_at > now() or await readers(db, identity):
            return "skipped"
        await db.execute(
            text("SELECT yoao_assets_v1.expire_original_cache_reads(:entry, :checked)"),
            {"entry": identity, "checked": now()},
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
                await db.execute(
                    text(
                        "SELECT yoao_assets_v1.reschedule_original_cache_check(:entry, :checked)"
                    ),
                    {"entry": identity, "checked": now()},
                )
                await db.commit()
                return "skipped"
        checked = now()
        if not await db.scalar(
            text(
                "SELECT yoao_assets_v1.retire_original_cache_generation(:entry, :checked, :next, NULL)"
            ),
            {
                "entry": identity,
                "checked": checked,
                "next": checked + timedelta(seconds=CHECK_SECONDS),
            },
        ):
            return "skipped"
        try:
            await prepare_target(db, record, require_enabled=False)
            matched = True
        except saved_storage.MediaCacheStorageError:
            matched = False
        await _record_cleanup_error(
            db, identity, None if matched else "storage_identity_changed"
        )
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
                f"original_cache_delete_{record.id.hex}"
                if saved_media_io.registered(record._target)
                else "original_cache_cleanup",
                expected_revision=record._target_maintenance_revision,
            ) as owned_permit:

                def remove():
                    if saved_media_io.registered(record._target):
                        accounting.begin(object_key(record), {"version_id": None})
                        saved_media_io.command(
                            "original-cache-delete",
                            saved_media_io.subject(record.id, owned_permit),
                            access="cleanup",
                            timeout=READ_SECONDS,
                        )
                        return
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
            await _record_cleanup_error(
                db,
                identity,
                "storage_identity_changed"
                if isinstance(failure, saved_storage.MediaCacheStorageError)
                else "storage_cleanup_failed",
            )
            await db.commit()
        log.warning("Original cache deletion deferred entry=%s", identity)
        return "deferred"
    async with factory() as db:
        removed = await db.scalar(
            text(
                "SELECT yoao_assets_v1.finish_original_cache_cleanup(:entry, :checked)"
            ),
            {"entry": identity, "checked": now()},
        )
        await db.commit()
    return "removed" if removed else "retained"


async def _record_cleanup_error(db, identity, error):
    await db.execute(
        text(
            "SELECT yoao_assets_v1.record_original_cache_cleanup_error(:entry, :error)"
        ),
        {"entry": identity, "error": error},
    )


async def maintain(factory, *, limit=50):
    async with factory() as db:
        ids = list(
            await db.scalars(
                text(
                    "SELECT * FROM yoao_assets_v1.due_original_cache_checks(:checked, :limit)"
                ),
                {"checked": now(), "limit": limit},
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
