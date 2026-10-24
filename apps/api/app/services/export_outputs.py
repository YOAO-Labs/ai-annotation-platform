"""Own export candidates, verified cache reads, and durable physical retirement."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import logging
import uuid

from sqlalchemy import delete, or_, select

from app.db.models.export_artifact import ExportArtifact
from app.db.models.export_output import ExportOutput
from app.db.models.storage_maintenance import StorageOperation
from app.services import export_storage, storage_access

log = logging.getLogger(__name__)
_FACTS = (
    "id",
    "project_id",
    "job_id",
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

    @property
    def storage_ref(self):
        return deepcopy(self._storage_ref)


def snapshot(row):
    """Thread arguments contain immutable values, never an attached ORM record."""
    export_storage.resolve(row)
    facts = {name: deepcopy(getattr(row, name)) for name in _FACTS}
    facts["_storage_ref"] = facts.pop("storage_ref")
    return OutputSnapshot(**facts)


def same(row, receipt):
    current = snapshot(row)
    return all(getattr(current, name) == getattr(receipt, name) for name in _FACTS)


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
    task = asyncio.create_task(asyncio.to_thread(call, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # The temporary ZIP remains owned until hashing physically finishes.
        try:
            await storage_access.finish(task)
        except BaseException:
            pass
        raise


async def create(
    db, *, project_id, job_id, media, format, file_count, path, expected_size=None
):
    """Persist a distinct immutable candidate before any object-store write."""
    sha256, size_bytes = await _settled_thread(export_storage.file_receipt, path)
    if expected_size is not None and size_bytes != expected_size:
        reject("export_storage_integrity_mismatch")
    started = now()
    row = ExportOutput(
        id=uuid.uuid4(),
        project_id=project_id,
        job_id=job_id,
        media=media,
        format=format,
        file_count=file_count,
        sha256=sha256,
        size_bytes=size_bytes,
        state="pending",
        created_at=started,
        expires_at=started + timedelta(days=7),
    )
    row.storage_ref = await export_storage.allocate(db, row)
    db.add(row)
    await db.flush()
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


async def sign_cached(db, hit, *, project_id, download_name):
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


async def _cleanup_one(db, identifier, counters):
    row = await db.scalar(
        select(ExportOutput)
        .where(ExportOutput.id == identifier)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    if row is None or row.state == "pending" or row.expires_at > now():
        await db.commit()
        return False
    receipt = snapshot(row)
    row.last_attempted_at = now()
    if await _has_read(db, receipt):
        await db.commit()
        return False
    removed = 0
    if row.state == "ready":
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
    await storage_access.run(
        db,
        uuid.UUID(owner.ref["location_id"]),
        "export_delete_" + receipt.id.hex,
        export_storage.delete,
        receipt,
    )
    current = await _locked(db, identifier)
    if (
        current is not None
        and current.state == "retired"
        and same(current, receipt)
        and current.expires_at <= now()
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
                )
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
