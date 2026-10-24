"""Own export candidates, verified cache reads, and durable physical retirement."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from contextvars import copy_context
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import partial
import logging
import uuid

from sqlalchemy import delete, or_, select

from app.db.models.export_artifact import ExportArtifact
from app.db.models.export_media_bundle import ExportMediaBundle
from app.db.models.export_output import ExportOutput
from app.db.models.storage_maintenance import StorageOperation
from app.services import export_storage, storage_access

log = logging.getLogger(__name__)
_FACTS = (
    "id",
    "project_id",
    "job_id",
    "media_bundle_id",
    "media",
    "format",
    "file_count",
    "sha256",
    "size_bytes",
    "storage_ref",
    "created_at",
    "expires_at",
)


def now():
    return datetime.now(timezone.utc)


def reject(code="export_output_changed"):
    raise export_storage.ExportStorageError(code)


@dataclass(frozen=True)
class OutputSnapshot:
    id: uuid.UUID
    project_id: uuid.UUID
    job_id: uuid.UUID
    media: str
    format: str
    file_count: int
    sha256: str
    size_bytes: int
    created_at: datetime
    expires_at: datetime
    _storage_ref: dict = field(repr=False)
    media_bundle_id: uuid.UUID | None = None

    @property
    def storage_ref(self):
        return deepcopy(self._storage_ref)


def _fact(row, name):
    if name == "media_bundle_id" and not isinstance(
        row, (ExportOutput, OutputSnapshot)
    ):
        # Historical lightweight consumers model unassociated candidates only.
        return getattr(row, name, None)
    return getattr(row, name)


def snapshot(row):
    """Thread arguments contain immutable values, never an attached ORM record."""
    export_storage.resolve(row)
    facts = {name: deepcopy(_fact(row, name)) for name in _FACTS}
    facts["_storage_ref"] = facts.pop("storage_ref")
    return OutputSnapshot(**facts)


def same(row, receipt):
    current = snapshot(row)
    return all(_fact(current, name) == _fact(receipt, name) for name in _FACTS)


def object_key(receipt):
    return export_storage.resolve(receipt).object_key


def read_purpose(identifier):
    return "export_read_" + uuid.UUID(str(identifier)).hex


async def _locked(db, identifier):
    return await db.scalar(
        select(ExportOutput)
        .where(ExportOutput.id == identifier)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def _settled_thread(call, *args):
    task = None
    try:
        task = asyncio.get_running_loop().run_in_executor(
            None, copy_context().run, partial(call, *args)
        )
        return await asyncio.shield(task)
    except BaseException as interrupted:
        if task is None:
            raise storage_access.mark_unsettled(interrupted)
        # The temporary ZIP remains owned until hashing physically finishes.
        # Soft limits and a second interruption must also drain the real thread.
        while not task.done():
            try:
                await asyncio.shield(task)
            except BaseException:
                continue
        try:
            task.result()
        except BaseException as late:
            if getattr(late, "storage_operation_unsettled", False):
                storage_access.mark_unsettled(interrupted)
        raise


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
        or media != "image"
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
        bundle = await db.scalar(
            select(ExportMediaBundle)
            .where(ExportMediaBundle.id == media_bundle_id)
            .with_for_update(nowait=True)
            .execution_options(populate_existing=True)
        )
        if bundle is None or bundle.id != media_bundle_id:
            reject()
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
    row = ExportOutput(
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
        # Allocation happened with the HTTP Job. The reserved ZIP must use the
        # same saved owner as its media, even if today's purpose defaults move.
        saved = deepcopy(bundle.storage_ref)
        saved.pop("key_prefix")
        saved["object_key"] = export_storage.key_for(
            media, project_id, job_id, identifier
        )
        row.storage_ref = export_storage.resolve_ref(
            saved, expected_key=saved["object_key"]
        ).ref
    _creation_valid(bundle, **facts)
    db.add(row)
    await db.flush()
    _creation_valid(bundle, **facts)
    receipt = snapshot(row)
    await db.commit()
    return receipt


async def write(db, receipt, path):
    """Drain the actual upload/readback thread before releasing the ZIP path."""
    owner = export_storage.resolve(receipt)
    revision = None

    async def acquired(permit):
        nonlocal revision
        revision = permit.revision
        row = await _locked(db, receipt.id)
        if row is None or row.state != "pending" or not same(row, receipt):
            reject()
        if row.expires_at <= now():
            reject("export_storage_expired")
        await db.commit()

    await storage_access.run(
        db,
        uuid.UUID(owner.ref["location_id"]),
        "export_write_" + receipt.id.hex,
        export_storage.write,
        receipt,
        path,
        on_acquired=acquired,
    )
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
    row.state = "ready"
    await db.flush()
    return row


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


async def _invalidate(db, artifact_id, output_id):
    # A competing record() may have repointed the same cache row. Never remove
    # that new index merely because the old candidate failed verification.
    await db.execute(
        delete(ExportArtifact).where(
            ExportArtifact.id == artifact_id,
            ExportArtifact.output_id == output_id,
        )
    )
    await db.commit()


async def verify_cache(db, artifact):
    """Use only a saved ready owner and retain uncertain/historical bytes."""
    if artifact.output_id is None:
        return None
    artifact_id, cache_key, batch_id = (
        artifact.id,
        artifact.cache_key,
        artifact.batch_id,
    )
    row = await db.get(ExportOutput, artifact.output_id, populate_existing=True)
    if row is None:
        reject("export_cache_output_mismatch")
    couple(artifact, row)
    receipt = snapshot(row)
    if row.state != "ready":
        reject("export_cache_output_mismatch")
    if row.expires_at <= now():
        return None
    owner = export_storage.resolve(receipt)
    revision = None

    async def acquired(permit):
        nonlocal revision
        revision = permit.revision
        current = await _locked(db, receipt.id)
        if current is None or current.state != "ready" or not same(current, receipt):
            reject()
        if current.expires_at <= now():
            reject("export_storage_expired")
        # The durable read receipt is visible before releasing this row lock.
        # Retirement either sees it or makes a later reader reject before I/O.
        await db.commit()

    try:
        present = await storage_access.run(
            db,
            uuid.UUID(owner.ref["location_id"]),
            read_purpose(receipt.id),
            export_storage.verify,
            receipt,
            missing_ok=True,
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
        await _invalidate(db, artifact_id, receipt.id)
        return None
    return CacheHit(artifact_id, cache_key, batch_id, receipt, revision)


async def sign_cached(db, hit, *, project_id, download_name, before_sign=None):
    """Lock the same verified candidate and fence maintenance until publication."""
    if hit.project_id != project_id:
        reject("export_cache_output_mismatch")
    row = await _locked(db, hit.output.id)
    if row is None or row.state != "ready" or not same(row, hit.output):
        reject()
    artifact = await db.scalar(
        select(ExportArtifact)
        .where(ExportArtifact.id == hit.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
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
    owner = export_storage.resolve(row)
    await storage_access.check(
        db,
        uuid.UUID(owner.ref["location_id"]),
        expected_revision=hit.gate_revision,
        lock=True,
    )
    if before_sign is not None:
        await before_sign()
    return export_storage.sign(hit.output, download_name=download_name)


async def _has_read(db, receipt):
    return (
        await db.scalar(
            select(StorageOperation.id)
            .where(
                StorageOperation.location_id
                == uuid.UUID(receipt.storage_ref["location_id"]),
                StorageOperation.purpose == read_purpose(receipt.id),
            )
            .limit(1)
        )
        is not None
    )


async def _has_index(db, identifier):
    return (
        await db.scalar(
            select(ExportArtifact.id)
            .where(ExportArtifact.output_id == identifier)
            .limit(1)
        )
        is not None
    )


async def _cleanup_locked(db, identifier):
    # Linked candidates share Bundle -> Output lock order with publication.
    from app.services import export_media_retirement

    bundle_id = await db.scalar(
        select(ExportOutput.media_bundle_id).where(ExportOutput.id == identifier)
    )
    bundle = None
    if bundle_id is not None:
        bundle = await export_media_retirement.lock_owner(db, bundle_id)
        if bundle is None:
            return None, None
    row = await db.scalar(
        select(ExportOutput)
        .where(ExportOutput.id == identifier)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    return row, bundle


async def _cleanup_one(db, identifier, counters):
    from app.services import export_media_retirement

    row, bundle = await _cleanup_locked(db, identifier)
    if row is None:
        await db.commit()
        return False
    linked_ended = bundle is not None and await export_media_retirement.zip_eligible(
        db, bundle, row
    )
    if (
        (bundle is not None and not linked_ended)
        or (
            row.state == "pending"
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
    receipt = snapshot(row)
    row.last_attempted_at = now()
    if await _has_read(db, receipt):
        await db.commit()
        return False
    removed = 0
    if row.state == "pending":
        if await _has_index(db, identifier):
            await db.commit()
            return False
        row.state, row.retired_at = "retired", now()
    elif row.state == "ready":
        result = await db.execute(
            delete(ExportArtifact)
            .where(ExportArtifact.output_id == identifier)
            .returning(ExportArtifact.id)
        )
        removed = len(result.all())
        row.state, row.retired_at = "retired", now()
    elif row.state != "retired" or await _has_index(db, identifier):
        await db.commit()
        return False
    row.last_attempted_at = now()
    await db.commit()
    counters["removed"] += removed
    owner = export_storage.resolve(receipt)
    if bundle is not None:
        await export_media_retirement.delete_zip(db, bundle, receipt)
    else:
        await storage_access.run(
            db,
            uuid.UUID(owner.ref["location_id"]),
            "export_delete_" + receipt.id.hex,
            export_storage.delete,
            receipt,
        )
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
        and not await _has_index(db, identifier)
        and not await _has_read(db, receipt)
    ):
        await db.delete(current)
        await db.commit()
        return True
    await db.commit()
    return False


async def cleanup(db, *, limit=100):
    """Retire expired known outputs; legacy expiry drops database indices only."""
    current = now()
    from app.services import export_media_retirement

    limit = max(1, min(limit, 100))
    legacy_ids = (
        select(ExportArtifact.id)
        .where(
            ExportArtifact.output_id.is_(None),
            ExportArtifact.expires_at <= current,
        )
        .order_by(ExportArtifact.expires_at, ExportArtifact.id)
        .limit(limit)
    )
    old = await db.execute(
        delete(ExportArtifact)
        .where(
            ExportArtifact.id.in_(legacy_ids),
            ExportArtifact.output_id.is_(None),
        )
        .returning(ExportArtifact.id)
    )
    result = {
        "removed": len(old.all()),
        "outputs_deleted": 0,
        "retained": 0,
        "failed": 0,
        "now": current.isoformat(),
    }
    await db.commit()
    identifiers = list(
        await db.scalars(
            select(ExportOutput.id)
            .where(
                or_(
                    (ExportOutput.state == "ready")
                    & (ExportOutput.expires_at <= current),
                    ExportOutput.state == "retired",
                    (ExportOutput.state == "pending")
                    & export_media_retirement.pending_zip_predicate(current),
                ),
                ~export_media_retirement.zip_operations_predicate(),
            )
            .order_by(
                ExportOutput.last_attempted_at.asc().nulls_first(),
                ExportOutput.created_at,
                ExportOutput.id,
            )
            .limit(limit)
        )
    )
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
