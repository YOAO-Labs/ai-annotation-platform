"""Own export candidates, verified cache reads, and durable physical retirement."""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextvars import copy_context
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import partial
from types import SimpleNamespace

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.services import export_delivery_data as data
from app.services import (
    export_execution_context,
    export_media_retirement,
    export_media_storage,
    export_storage,
    export_zip_accounting,
    saved_output_io,
    storage_access,
)
from app.services import key_removal_accounting as removal
from app.services import object_removal_accounting as version_removal
from app.services import (
    storage_migration_artifact_reads as artifact_reads,
)
from app.services import (
    storage_migration_read_publications as read_publications,
)
from app.services.export_output_receipt import (
    OutputSnapshot,
    same as same,
    snapshot as snapshot,
)
from app.services.original_read_plan import ReadRoute

log = logging.getLogger(__name__)


def now():
    return datetime.now(timezone.utc)


def reject(code="export_output_changed"):
    raise export_storage.ExportStorageError(code)


def object_key(receipt):
    return export_storage.resolve(receipt).object_key


def read_purpose(identifier):
    return "export_read_" + uuid.UUID(str(identifier)).hex


async def _locked(db, identifier):
    return await data.zip_receipt(db, identifier, mode="update")


async def _settled_thread(call, *args):
    task, failure, entered = None, None, False
    owner = export_execution_context.current_optional()
    if owner is not None:
        await owner.enter_io()
        entered = True
    try:
        task = asyncio.get_running_loop().run_in_executor(
            None, copy_context().run, partial(call, *args)
        )
        return await asyncio.shield(task)
    except BaseException as interrupted:
        failure = interrupted
        if task is None:
            raise storage_access.mark_unsettled(interrupted)
        while not task.done():
            try:
                await asyncio.shield(task)
            except BaseException:
                continue
        try:
            task.result()
        except BaseException as late:
            if owner is not None and late is not interrupted:
                failure = BaseExceptionGroup(
                    "Export hashing and drained physical work failed",
                    [interrupted, late],
                )
                raise failure from None
            if storage_access.failure_unsettled(late):
                storage_access.mark_unsettled(interrupted)
        raise
    finally:
        if (
            entered
            and task is not None
            and task.done()
            and (failure is None or not storage_access.failure_unsettled(failure))
        ):
            await export_execution_context.settle_io(failure)


def _identifier(value):
    try:
        result = uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        reject("export_storage_invalid_receipt")
    if not isinstance(value, (str, uuid.UUID)) or str(value) != str(result):
        reject("export_storage_invalid_receipt")
    return result


def _instant(value):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        reject("export_storage_invalid_receipt")
    return value.astimezone(timezone.utc)


async def fixture_native_lifetime(db, job_id, project_id):
    return (
        await db.execute(
            text(
                "SELECT run_id, created_at, expires_at "
                "FROM public.fixture_native_export_lifetime(:job_id, :project_id)"
            ),
            {"job_id": _identifier(job_id), "project_id": _identifier(project_id)},
        )
    ).one_or_none()


def _creation_valid(
    bundle, *, bundle_id, identifier, project_id, job_id, media, started, expires
):
    current = now()
    if started > current or not started < expires <= started + timedelta(days=7):
        reject("export_storage_invalid_receipt")
    if expires <= current:
        reject("export_storage_expired")
    if bundle is None:
        return
    if (
        bundle.id != bundle_id
        or bundle.project_id != _identifier(project_id)
        or bundle.job_id != _identifier(job_id)
        or bundle.zip_output_id != identifier
        or bundle.created_at != started
        or bundle.expires_at != expires
        or media
        != ("video" if export_media_storage.input_schema(bundle) == 4 else "image")
        or bundle.state != "building"
        or bundle.published_at is not None
        or bundle.retired_at is not None
        or not isinstance(bundle.lease_token, uuid.UUID)
        or bundle.lease_expires_at is None
    ):
        reject()
    if _instant(bundle.lease_expires_at) <= current:
        reject("export_storage_expired")


async def create(
    db,
    *,
    project_id,
    job_id,
    media,
    format,
    file_count,
    path,
    expected_size=None,
    output_id=None,
    media_bundle_id=None,
    created_at=None,
    expires_at=None,
):
    """Persist a distinct immutable candidate before any object-store write."""
    sha256, size_bytes = await _settled_thread(export_storage.file_receipt, path)
    if expected_size is not None and size_bytes != expected_size:
        reject("export_storage_integrity_mismatch")
    bundle = None
    if media_bundle_id is not None:
        media_bundle_id = _identifier(media_bundle_id)
        bundle = await data.media_fence(db, media_bundle_id, mode="update_nowait")
        if bundle is None or bundle.id != media_bundle_id:
            reject()
    native = (
        await fixture_native_lifetime(db, job_id, project_id)
        if bundle is None
        else None
    )
    if native is not None:
        if created_at is not None or expires_at is not None:
            reject("export_storage_invalid_receipt")
        created_at, expires_at = native.created_at, native.expires_at
    identifier = (
        _identifier(output_id)
        if output_id is not None
        else (bundle.zip_output_id if bundle is not None else uuid.uuid4())
    )
    started = (
        _instant(created_at)
        if created_at is not None
        else (_instant(bundle.created_at) if bundle is not None else now())
    )
    expires = (
        _instant(expires_at)
        if expires_at is not None
        else (
            _instant(bundle.expires_at)
            if bundle is not None
            else started + timedelta(days=7)
        )
    )
    facts = dict(
        bundle_id=media_bundle_id,
        identifier=identifier,
        project_id=project_id,
        job_id=job_id,
        media=media,
        started=started,
        expires=expires,
    )
    _creation_valid(bundle, **facts)
    row = SimpleNamespace(
        id=identifier,
        project_id=project_id,
        job_id=job_id,
        media_bundle_id=media_bundle_id,
        media=media,
        format=format,
        file_count=file_count,
        sha256=sha256,
        size_bytes=size_bytes,
        state="pending",
        created_at=started,
        expires_at=expires,
    )
    if bundle is None:
        row.storage_ref = await export_storage.allocate(db, row)
    else:
        saved = deepcopy(bundle.storage_ref)
        root = saved.pop("key_prefix")[
            : -len(export_media_storage.prefix_for(bundle.id))
        ]
        saved["object_key"] = root + export_storage.key_for(
            media, project_id, job_id, identifier
        )
        row.storage_ref = export_storage.resolve_ref(
            saved,
            expected_key=export_storage.key_for(media, project_id, job_id, identifier),
        ).ref
    _creation_valid(bundle, **facts)
    if not await data.reserve_zip(db, row):
        reject()
    _creation_valid(bundle, **facts)
    receipt = snapshot(
        row,
        fixture=native is not None
        or (bundle is not None and bundle.fixture_run_id is not None),
    )
    await db.commit()
    return receipt


def proof_writer(db, receipt):
    """Persist a verified upload from its physical thread before it can settle."""
    loop = asyncio.get_running_loop()
    factory = async_sessionmaker(storage_access.engine(db), expire_on_commit=False)

    async def persist(proof):
        async with factory() as owned:
            row = await _locked(owned, receipt.id)
            if row is None or row.state != "pending" or not same(row, receipt):
                reject()
            if row.upload_proof is not None:
                reject("export_storage_invalid_receipt")
            if not await data.save_zip_proof(owned, receipt.id, proof):
                reject("export_storage_invalid_receipt")
            await owned.commit()

    def save(proof):
        coroutine = persist(proof)
        try:
            future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        except BaseException:
            coroutine.close()
            raise
        future.result()

    return save


async def write(db, receipt, path):
    """Drain the actual upload/readback thread before releasing the ZIP path."""
    owner = export_storage.resolve(receipt)
    target = await export_storage.io_target(db, receipt)
    source = saved_output_io.capture(target, "zip", receipt)
    revision = None

    async def acquired(permit):
        nonlocal revision
        revision = permit.revision
        if source is not None:
            source.attach(permit)
        row = await _locked(db, receipt.id)
        if row is None or row.state != "pending" or not same(row, receipt):
            reject()
        if row.expires_at <= now():
            reject("export_storage_expired")
        await db.commit()

    accounting = export_zip_accounting.recorder(db, receipt)
    failure = None
    try:
        await storage_access.run(
            db,
            uuid.UUID(owner.ref["location_id"]),
            "export_write_" + receipt.id.hex,
            source.write_path if source is not None else export_storage.write,
            *((path,) if source is not None else (receipt, path)),
            **({"target": target} if target is not None and source is None else {}),
            accounting=accounting,
            **(
                {"save_proof": proof_writer(db, receipt)}
                if receipt.fixture and source is None
                else {}
            ),
            on_acquired=acquired,
        )
    except BaseException as exc:
        failure = exc
    failure = await export_zip_accounting.finish(accounting, failure)
    if failure is not None:
        raise failure
    return revision


async def publish(db, receipt, revision):
    """Join the caller's final authorization/result transaction, without commit."""
    owner = export_storage.resolve(receipt)
    row = await _locked(db, receipt.id)
    if row is None or row.state != "pending" or not same(row, receipt):
        reject()
    if row.expires_at <= now():
        reject("export_storage_expired")
    await storage_access.check(
        db,
        uuid.UUID(owner.ref["location_id"]),
        expected_revision=revision,
        lock=True,
    )
    if not await data.publish_zip(db, receipt.id):
        if row.expires_at <= now():
            reject("export_storage_expired")
        reject("export_storage_invalid_receipt")
    return SimpleNamespace(**(dict(row._mapping) | {"state": "ready"}))


def couple(artifact, row):
    """A foreign key alone does not prove the cache metadata describes its bytes."""
    if (
        artifact.output_id != row.id
        or artifact.project_id != row.project_id
        or artifact.object_key != object_key(row)
        or artifact.size_bytes != row.size_bytes
        or artifact.file_count != row.file_count
        or artifact.format != row.format
        or artifact.expires_at != row.expires_at
    ):
        reject("export_cache_output_mismatch")


@dataclass(frozen=True)
class CacheHit:
    id: uuid.UUID
    cache_key: str
    batch_id: uuid.UUID | None
    output: OutputSnapshot
    gate_revision: int
    read_route: ReadRoute | None = None

    @property
    def project_id(self):
        return self.output.project_id

    @property
    def object_key(self):
        return object_key(self.output)

    @property
    def expires_at(self):
        return self.output.expires_at

    @property
    def file_count(self):
        return self.output.file_count

    @property
    def size_bytes(self):
        return self.output.size_bytes


async def verify_cache(db, artifact):
    """Use only a saved ready owner and retain uncertain/historical bytes."""
    if artifact.output_id is None:
        return None
    artifact_id, cache_key, batch_id = (
        artifact.id,
        artifact.cache_key,
        artifact.batch_id,
    )
    row = await data.zip_receipt(db, artifact.output_id)
    if row is None:
        reject("export_cache_output_mismatch")
    couple(artifact, row)
    receipt = snapshot(row)
    if row.state != "ready":
        reject("export_cache_output_mismatch")
    if row.expires_at <= now():
        return None
    if receipt.media_bundle_id is None:
        if await fixture_native_lifetime(db, receipt.job_id, receipt.project_id):
            return None
    else:
        bundle = await data.media_fence(db, receipt.media_bundle_id)
        if bundle is None:
            reject("export_cache_output_mismatch")
        if bundle.fixture_run_id is not None:
            return None
    owner = export_storage.resolve(receipt)
    route = await _read_route(db, receipt)
    target = None if route is not None else await export_storage.io_target(db, receipt)
    source = saved_output_io.capture(target, "zip", receipt)
    revision = None

    async def acquired(permit):
        nonlocal revision
        revision = permit.revision
        if source is not None:
            source.attach(permit)
        current = await _locked(db, receipt.id)
        if current is None or current.state != "ready" or not same(current, receipt):
            reject()
        if current.expires_at <= now():
            reject("export_storage_expired")
        await db.commit()

    try:
        if route is not None:
            present = await artifact_reads.run(
                db,
                route,
                read_purpose(receipt.id),
                lambda client: export_storage.verify_read(
                    client, receipt, missing_ok=True
                ),
                on_acquired=acquired,
            )
        else:
            present = await storage_access.run(
                db,
                uuid.UUID(owner.ref["location_id"]),
                read_purpose(receipt.id),
                source.verify if source is not None else export_storage.verify,
                *(() if source is not None else (receipt,)),
                missing_ok=True,
                **({"target": target} if target is not None and source is None else {}),
                on_acquired=acquired,
            )
    except export_storage.ExportStorageError as exc:
        if exc.code not in {
            "export_storage_missing",
            "export_storage_integrity_mismatch",
            "export_storage_expired",
        }:
            raise
        if getattr(exc, "storage_operation_unsettled", False):
            raise
        await db.rollback()
        present = False
    if not present:
        await data.invalidate_cache(db, artifact_id, receipt.id)
        await db.commit()
        return None
    return CacheHit(artifact_id, cache_key, batch_id, receipt, revision, route)


@dataclass(frozen=True)
class PreparedDownload:
    output: OutputSnapshot
    download_name: str
    url: str = field(repr=False)
    gate_revision: int
    read_route: ReadRoute | None = None


async def _read_route(db, receipt):
    owner = export_storage.resolve(receipt)
    return await artifact_reads.resolve(
        db, owner.ref, owner.object_key, receipt.sha256, receipt.size_bytes
    )


async def _check_read(db, receipt, revision, route, *, lock=False):
    owner = export_storage.resolve(receipt)
    location_id = uuid.UUID(owner.ref["location_id"])
    if route is not None:
        if (
            not isinstance(route, ReadRoute)
            or route.source_location_id != location_id
            or route.source_endpoint_url != owner.ref["endpoint_url"]
            or route.source_bucket != owner.bucket
            or route.source_object_key != owner.object_key
            or route.sha256 != receipt.sha256
            or route.size_bytes != receipt.size_bytes
            or route.maintenance_revision != revision
        ):
            reject("export_storage_invalid_receipt")
        await read_publications.recheck(db, route, lock=lock)
        return
    await storage_access.check(db, location_id, expected_revision=revision, lock=lock)


async def prepare_download(db, receipt, *, download_name, expected_revision=None):
    """Construct, sign and close an external client before final business locks."""
    if receipt.storage_ref["schema"] == 2 and receipt.upload_proof is None:
        row = await data.zip_receipt(db, receipt.id)
        if row is None or not same(row, receipt):
            reject()
        receipt = snapshot(row, fixture=receipt.fixture)
    route = await _read_route(db, receipt)
    if route is None and receipt.storage_ref["schema"] == 1:
        return None
    revision = None
    source = None

    async def acquired(permit):
        nonlocal revision
        revision = permit.revision
        if source is not None:
            source.attach(permit)

    if route is not None:
        if (
            expected_revision is not None
            and expected_revision != route.maintenance_revision
        ):
            reject("export_storage_identity_changed")
        url = await artifact_reads.run(
            db,
            route,
            read_purpose(receipt.id),
            lambda client: export_storage.sign_read(
                client, receipt, download_name=download_name
            ),
            on_acquired=acquired,
        )
    else:
        target = await export_storage.io_target(db, receipt)
        source = saved_output_io.capture(target, "zip", receipt)
        url = await storage_access.run(
            db,
            target.location_id,
            read_purpose(receipt.id),
            source.sign if source is not None else export_storage.sign,
            *(() if source is not None else (receipt,)),
            download_name=download_name,
            **({"target": target} if source is None else {}),
            expected_revision=expected_revision,
            on_acquired=acquired,
        )
    return PreparedDownload(receipt, download_name, url, revision, route)


async def prepared_download(db, receipt, *, download_name, prepared):
    """Final publication uses only a URL prepared for these exact saved facts."""
    if receipt.storage_ref["schema"] == 1 and prepared is None:
        if await _read_route(db, receipt) is not None:
            reject("export_storage_invalid_receipt")
        await storage_access.check(
            db, uuid.UUID(receipt.storage_ref["location_id"]), lock=True
        )
        return await export_execution_context.run_sync(
            export_storage.sign, receipt, download_name=download_name
        )
    if (
        not isinstance(prepared, PreparedDownload)
        or prepared.download_name != download_name
        or not same(receipt, prepared.output)
        or (receipt.storage_ref["schema"] == 1 and prepared.read_route is None)
    ):
        reject("export_storage_invalid_receipt")
    await _check_read(
        db, receipt, prepared.gate_revision, prepared.read_route, lock=True
    )
    if receipt.expires_at <= now():
        reject("export_storage_expired")
    return prepared.url


async def sign_cached(
    db, hit, *, project_id, download_name, before_sign=None, prepared=None
):
    """Lock the same verified candidate and fence maintenance until publication."""
    if hit.project_id != project_id:
        reject("export_cache_output_mismatch")
    row = await _locked(db, hit.output.id)
    if row is None or row.state != "ready" or not same(row, hit.output):
        reject()
    artifact = await data.lock_cache(db, hit.id)
    if (
        artifact is None
        or artifact.cache_key != hit.cache_key
        or artifact.batch_id != hit.batch_id
        or row.project_id != project_id
    ):
        reject("export_cache_output_mismatch")
    couple(artifact, row)
    if row.expires_at <= now():
        reject("export_storage_expired")
    await _check_read(db, hit.output, hit.gate_revision, hit.read_route, lock=True)
    if hit.read_route is None and hit.output.storage_ref["schema"] == 1:
        if before_sign is not None:
            await before_sign()
        return await export_execution_context.run_sync(
            export_storage.sign, hit.output, download_name=download_name
        )
    if (
        not isinstance(prepared, PreparedDownload)
        or prepared.read_route != hit.read_route
    ):
        reject("export_storage_invalid_receipt")
    url = await prepared_download(
        db, hit.output, download_name=download_name, prepared=prepared
    )
    if before_sign is not None:
        await before_sign()
    if hit.output.expires_at <= now():
        reject("export_storage_expired")
    return url


async def _cleanup_locked(db, identifier):
    bundle_id = await data.zip_retirement_bundle(db, identifier)
    bundle = None
    if bundle_id is not None:
        bundle = await export_media_retirement.lock_owner(db, bundle_id)
        if bundle is None:
            return None, None
    row = await data.zip_receipt(db, identifier, mode="retirement")
    return row, bundle


async def _cleanup_one(db, identifier, counters):
    row, bundle = await _cleanup_locked(db, identifier)
    if row is None:
        await db.commit()
        return False
    native = (
        await fixture_native_lifetime(db, row.job_id, row.project_id)
        if bundle is None
        else None
    )
    if (
        (bundle is not None and bundle.fixture_run_id is not None) or native is not None
    ) and not await export_media_retirement.fixture_owner_session(db):
        await db.commit()
        return False
    native_eligible = native is not None and bool(
        await db.scalar(
            text("SELECT public.fixture_native_export_cleanup_eligible(:output_id)"),
            {"output_id": identifier},
        )
    )
    if native is not None and not native_eligible:
        await db.commit()
        return False
    linked_ended = bundle is not None and await export_media_retirement.zip_eligible(
        db, bundle, row
    )
    if (
        (bundle is not None and not linked_ended)
        or (
            row.state == "pending"
            and not native_eligible
            and (not linked_ended or bundle.published_at is not None)
        )
        or (
            row.expires_at > now()
            and not (
                linked_ended
                and bundle.published_at is None
                and row.state in {"pending", "retired"}
            )
        )
    ):
        await db.commit()
        return False
    private = native is not None or (
        bundle is not None and bundle.fixture_run_id is not None
    )
    proof = deepcopy(row.upload_proof)
    if private and proof is None:
        await db.commit()
        return False
    receipt = snapshot(row)
    removed = await data.start_zip_retirement(db, identifier, now())
    if removed is None:
        await db.commit()
        return False
    await db.commit()
    counters["removed"] += removed
    owner = export_storage.resolve(receipt)
    if bundle is not None:
        await export_media_retirement.delete_zip(
            db, bundle, receipt, **({"proof": proof} if proof is not None else {})
        )
    else:
        recorder = (
            version_removal
            if proof is not None and proof["version_id"] is not None
            else removal
        )
        accounting = recorder.recorder(
            db, receipt, producer="export_zip_removals", purpose="temporary"
        )
        owned_permit, failure = None, None
        target = None
        source = None

        async def acquired(permit):
            nonlocal owned_permit
            owned_permit = permit
            if source is not None:
                source.attach(permit)

        try:
            if receipt.storage_ref["schema"] == 2:
                target = await export_storage.io_target(db, receipt)
                source = saved_output_io.capture(target, "zip", receipt)
            await storage_access.run(
                db,
                uuid.UUID(owner.ref["location_id"]),
                "export_delete_" + receipt.id.hex,
                source.delete if source is not None else export_storage.delete,
                *(() if source is not None else (receipt,)),
                accounting=accounting,
                **({"proof": proof} if proof is not None and source is None else {}),
                on_acquired=acquired,
                **(
                    {"version": proof["version_id"]}
                    if source is not None and proof is not None
                    else {}
                ),
                **({"target": target} if target is not None and source is None else {}),
            )
        except BaseException as exc:
            failure = exc
        failure = await removal.finish(
            accounting,
            failure,
            settled=owned_permit is not None
            and getattr(owned_permit, "_settled", False) is True,
        )
        if failure is not None:
            raise failure
    current, current_bundle = await _cleanup_locked(db, identifier)
    linked_ended = (
        current_bundle is not None
        and current is not None
        and (await export_media_retirement.zip_eligible(db, current_bundle, current))
    )
    if (
        current is not None
        and current.state == "retired"
        and same(current, receipt)
        and (
            current.expires_at <= now()
            or (linked_ended and current_bundle.published_at is None)
        )
        and (current_bundle is None or linked_ended)
        and await data.zip_retirement_unreferenced(db, identifier)
    ):
        if private and not await db.scalar(
            text("SELECT public.confirm_fixture_export_zip_absent(:output_id)"),
            {"output_id": current.id},
        ):
            await db.commit()
            return False
        if current_bundle is not None and current_bundle.fixture_run_id is not None:
            await db.execute(
                text(
                    "INSERT INTO public.fixture_export_zip_retirements "
                    "(bundle_id, output_id, fixture_run_id, project_id, job_id, sha256, size_bytes) "
                    "VALUES (:bundle_id, :output_id, :run_id, :project_id, :job_id, :sha256, :size_bytes)"
                ),
                {
                    "bundle_id": current_bundle.id,
                    "output_id": current.id,
                    "run_id": current_bundle.fixture_run_id,
                    "project_id": current.project_id,
                    "job_id": current.job_id,
                    "sha256": current.sha256,
                    "size_bytes": current.size_bytes,
                },
            )
        if native is not None and not await db.scalar(
            text("SELECT public.retire_fixture_native_export_output(:output_id)"),
            {"output_id": current.id},
        ):
            await db.rollback()
            return False
        if not await data.remove_retired_zip(db, identifier, now()):
            await db.rollback()
            return False
        await db.commit()
        return True
    await db.commit()
    return False


async def cleanup(db, *, limit=100):
    """Retire expired known outputs; legacy expiry drops database indices only."""
    current = now()
    limit = max(1, min(limit, 100))
    removed = await data.purge_legacy_cache(db, current, limit)
    result = {
        "removed": removed,
        "outputs_deleted": 0,
        "retained": 0,
        "failed": 0,
        "now": current.isoformat(),
    }
    await db.commit()
    identifiers = await data.zip_retirement_queue(db, current, limit)
    await db.commit()
    for identifier in identifiers:
        try:
            deleted = await _cleanup_one(db, identifier, result)
            result["outputs_deleted" if deleted else "retained"] += 1
        except Exception:
            await db.rollback()
            result["failed"] += 1
            log.warning("Export output retirement retained: %s", identifier)
    return result
