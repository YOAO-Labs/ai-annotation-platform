"""Independent video outputs, durable client ownership and guarded collection."""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import sys
import uuid

from sqlalchemy import String, and_, cast, exists, func, or_, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm.attributes import flag_modified

from app.config import settings
from app.db.models.dataset import DatasetItem, VideoChunk, VideoFrameCache
from app.db.models.storage_maintenance import StorageOperation
from app.db.models.video_derivative import VideoDerivativeOutput as Output
from app.services import storage_access
from app.services import video_derivative_originals as originals
from app.services import video_derivative_storage as physical
from app.services.content_snapshot import ContentError


def utcnow():
    return datetime.now(timezone.utc)


def reject(code="video_derivative_output_changed"):
    raise ContentError(code)


def purpose(identifier):
    return "video_derivative_" + uuid.UUID(str(identifier)).hex


@dataclass(frozen=True)
class Receipt:
    id: uuid.UUID
    dataset_item_id: uuid.UUID
    kind: str
    spec: dict = field(repr=False)
    original_ref: dict = field(repr=False)
    auth_ref: dict = field(repr=False)
    metadata_output_id: uuid.UUID | None
    storage_ref: dict = field(repr=False)
    state: str
    lease_token: uuid.UUID | None
    lease_expires_at: datetime | None
    inventory: dict | None = field(repr=False)
    result: dict | None = field(repr=False)
    error_code: str | None
    created_at: datetime
    updated_at: datetime


def snapshot(row):
    physical.resolve(row)
    return Receipt(
        **{name: deepcopy(getattr(row, name)) for name in Receipt.__dataclass_fields__}
    )


async def allocate(
    db, *, dataset_item_id, kind, spec, original_ref, auth_ref, metadata_output_id=None
):
    """Allocate metadata only; the caller retains its short business transaction."""
    if (
        not isinstance(dataset_item_id, uuid.UUID)
        or kind not in {"metadata", "frame", "chunk"}
        or (metadata_output_id is None) != (kind == "metadata")
        or (
            metadata_output_id is not None
            and not isinstance(metadata_output_id, uuid.UUID)
        )
        or any(not isinstance(value, dict) for value in (spec, original_ref, auth_ref))
    ):
        reject("video_derivative_input_invalid")
    identifier = uuid.uuid4()
    ref = await physical.allocate(db, identifier)
    await storage_access.check(db, uuid.UUID(ref["location_id"]), lock=True)
    output = Output(
        id=identifier,
        dataset_item_id=dataset_item_id,
        kind=kind,
        spec=deepcopy(spec),
        original_ref=deepcopy(original_ref),
        auth_ref=deepcopy(auth_ref),
        metadata_output_id=metadata_output_id,
        storage_ref=ref,
        state="building",
    )
    db.add(output)
    await db.flush()
    return output


async def claim(db, output, ttl_seconds=900):
    """Claim a caller-locked attempt exactly once; expired attempts never renew."""
    if type(ttl_seconds) is not int or not 0 < ttl_seconds <= originals.JOB_SECONDS:
        reject("video_derivative_lease_invalid")
    physical.resolve(output)
    if (
        output.state != "building"
        or output.lease_token is not None
        or output.lease_expires_at is not None
    ):
        return None
    token = uuid.uuid4()
    output.lease_token = token
    output.lease_expires_at = utcnow() + timedelta(seconds=ttl_seconds)
    await db.flush()
    from app.services.video_asset_retry_jobs import sync_output

    await sync_output(db, output)
    return token


@asynccontextmanager
async def access(db, receipt, guard):
    """Own client construction through actual close under an independent permit."""
    owner = physical.resolve(receipt)
    factory = async_sessionmaker(storage_access.engine(db), expire_on_commit=False)
    session = factory()
    try:
        location_id = uuid.UUID(owner.ref["location_id"])
        revision = await storage_access.check(session, location_id)
        await session.commit()
        async with await storage_access.acquire(
            session, location_id, purpose(receipt.id), expected_revision=revision
        ) as permit:
            await guard(permit.revision)
            context, entered = physical.open_storage(), []

            def open_owned():
                value = context.__enter__()
                entered.append(True)
                return value

            try:
                client = await originals.physical(
                    open_owned, timeout=originals.JOB_SECONDS
                )
                yield client
            finally:
                # A cancelled constructor may finish after its await was interrupted.
                # physical() drains it before returning; the holder owns that late result.
                if entered:
                    await originals.physical(
                        context.__exit__, *sys.exc_info(), timeout=originals.JOB_SECONDS
                    )
            await guard(permit.revision)
    finally:
        await originals.drain(asyncio.create_task(session.close()))


def _no_operations(identifier):
    return ~exists().where(
        StorageOperation.purpose
        == "video_derivative_" + func.replace(cast(identifier, String), "-", "")
    )


def _unreferenced(identifier):
    return and_(
        ~exists().where(
            or_(
                DatasetItem.video_output_id == identifier,
                DatasetItem.video_building_output_id == identifier,
            )
        ),
        ~exists().where(VideoFrameCache.output_id == identifier),
        ~exists().where(VideoChunk.output_id == identifier),
    )


async def _in_use(db, identifier, *, except_id=None):
    statement = select(exists().where(StorageOperation.purpose == purpose(identifier)))
    if except_id is not None:
        statement = select(
            exists().where(
                StorageOperation.purpose == purpose(identifier),
                StorageOperation.id != except_id,
            )
        )
    return bool(await db.scalar(statement))


async def _has_reference(db, identifier):
    return not bool(await db.scalar(select(_unreferenced(identifier))))


async def _lock_context(db, identifier):
    item_id = await db.scalar(
        select(Output.dataset_item_id).where(Output.id == identifier)
    )
    if item_id is None:
        return None, [], [], None
    item = await db.scalar(
        select(DatasetItem)
        .where(DatasetItem.id == item_id)
        .with_for_update(nowait=True)
        .execution_options(populate_existing=True)
    )
    frames = list(
        await db.scalars(
            select(VideoFrameCache)
            .where(
                VideoFrameCache.dataset_item_id == item_id,
                VideoFrameCache.output_id == identifier,
            )
            .order_by(VideoFrameCache.id)
            .with_for_update(nowait=True)
            .execution_options(populate_existing=True)
        )
    )
    chunks = list(
        await db.scalars(
            select(VideoChunk)
            .where(
                VideoChunk.dataset_item_id == item_id,
                VideoChunk.output_id == identifier,
            )
            .order_by(VideoChunk.id)
            .with_for_update(nowait=True)
            .execution_options(populate_existing=True)
        )
    )
    output = await db.scalar(
        select(Output)
        .where(Output.id == identifier)
        .with_for_update(nowait=True)
        .execution_options(populate_existing=True)
    )
    return item, frames, chunks, output


def _clear_cache(row):
    row.output_id = None
    row.status = "pending"
    row.storage_key = None
    row.byte_size = None
    row.error = None
    if isinstance(row, VideoChunk):
        row.generation_mode = None
        row.diagnostics = {}


async def _expire_cache(db, model, kind, cutoff, limit):
    candidates = list(
        await db.execute(
            select(model.dataset_item_id, model.id, Output.id)
            .join(Output, model.output_id == Output.id)
            .where(
                model.status == "ready",
                model.last_accessed_at < cutoff,
                Output.state == "ready",
                Output.kind == kind,
                Output.dataset_item_id == model.dataset_item_id,
                _no_operations(Output.id),
            )
            .order_by(model.last_accessed_at, model.id)
            .limit(limit)
        )
    )
    await db.commit()
    for item_id, row_id, identifier in candidates:
        try:
            await db.scalar(
                select(DatasetItem.id)
                .where(DatasetItem.id == item_id)
                .with_for_update(nowait=True)
            )
            row = await db.scalar(
                select(model)
                .where(model.id == row_id, model.dataset_item_id == item_id)
                .with_for_update(nowait=True)
                .execution_options(populate_existing=True)
            )
            output = await db.scalar(
                select(Output)
                .where(Output.id == identifier)
                .with_for_update(nowait=True)
                .execution_options(populate_existing=True)
            )
            if (
                row is None
                or output is None
                or output.state != "ready"
                or output.kind != kind
                or output.dataset_item_id != item_id
                or row.output_id != identifier
                or row.status != "ready"
                or row.last_accessed_at is None
                or row.last_accessed_at >= cutoff
                or await _in_use(db, identifier)
            ):
                await db.rollback()
                continue
            physical.resolve(output)
            _clear_cache(row)
            await db.commit()
        except (ContentError, physical.VideoDerivativeStorageError):
            await db.rollback()
        except DBAPIError as exc:
            await db.rollback()
            if not storage_access.unavailable(exc):
                raise


def _expired(now):
    return or_(
        Output.lease_expires_at <= now,
        and_(
            Output.lease_token.is_(None),
            Output.lease_expires_at.is_(None),
            Output.updated_at < now - timedelta(seconds=originals.JOB_SECONDS),
        ),
    )


async def _expire_building(db, now, limit):
    identifiers = list(
        await db.scalars(
            select(Output.id)
            .where(Output.state == "building", _expired(now), _no_operations(Output.id))
            .order_by(Output.updated_at, Output.id)
            .limit(limit)
        )
    )
    await db.commit()
    for identifier in identifiers:
        try:
            item, frames, chunks, output = await _lock_context(db, identifier)
            if (
                output is None
                or output.state != "building"
                or await _in_use(db, identifier)
            ):
                await db.rollback()
                continue
            expired = (
                output.lease_expires_at <= now
                if output.lease_expires_at is not None
                else output.lease_token is None
                and output.updated_at < now - timedelta(seconds=originals.JOB_SECONDS)
            )
            if not expired:
                await db.rollback()
                continue
            physical.resolve(output)
            if (
                item is not None
                and output.kind == "metadata"
                and item.video_building_output_id == identifier
            ):
                item.video_building_output_id = None
            for row in (
                frames
                if output.kind == "frame"
                else chunks
                if output.kind == "chunk"
                else []
            ):
                if row.status == "pending":
                    _clear_cache(row)
            age = output.updated_at
            output.state = "retiring"
            output.error_code = output.error_code or "lease_expired"
            output.updated_at = age
            flag_modified(output, "updated_at")
            from app.services.video_asset_retry_jobs import sync_output

            await sync_output(db, output)
            await db.commit()
        except (ContentError, physical.VideoDerivativeStorageError):
            await db.rollback()
        except DBAPIError as exc:
            await db.rollback()
            if not storage_access.unavailable(exc):
                raise


async def reconcile(db, *, limit=100, grace_seconds=3600):
    """Use a GC-owned session to detach stale known refs, then retire exact owners."""
    if (
        type(limit) is not int
        or not 1 <= limit <= 1000
        or type(grace_seconds) is not int
        or grace_seconds < 3600
    ):
        reject("video_derivative_gc_invalid")
    now = utcnow()
    await _expire_cache(
        db,
        VideoFrameCache,
        "frame",
        now - timedelta(days=settings.video_frame_cache_ttl_days),
        limit,
    )
    await _expire_cache(
        db,
        VideoChunk,
        "chunk",
        now - timedelta(days=settings.video_chunk_cache_ttl_days),
        limit,
    )
    await _expire_building(db, now, limit)
    cutoff = now - timedelta(seconds=grace_seconds)
    identifiers = list(
        await db.scalars(
            select(Output.id)
            .where(
                Output.state != "deleted",
                Output.updated_at < cutoff,
                or_(Output.state != "building", _expired(now)),
                _unreferenced(Output.id),
                _no_operations(Output.id),
            )
            .order_by(Output.updated_at, Output.id)
            .limit(limit)
        )
    )
    await db.commit()
    removed = 0
    for identifier in identifiers:
        try:
            _, _, _, output = await _lock_context(db, identifier)
            if (
                output is None
                or output.state == "deleted"
                or output.updated_at >= cutoff
                or await _has_reference(db, identifier)
                or await _in_use(db, identifier)
            ):
                await db.rollback()
                continue
            if (
                output.state == "building"
                and output.lease_expires_at is not None
                and output.lease_expires_at > now
            ):
                await db.rollback()
                continue
            physical.resolve(output)
            published = output.state == "ready"
            age = output.updated_at
            output.state = "retiring"
            output.updated_at = now if published else age
            flag_modified(output, "updated_at")
            await db.flush()
            receipt = snapshot(output)
            await db.commit()
            if published:
                continue

            async def guarded(revision):
                _, _, _, current = await _lock_context(db, identifier)
                if (
                    current is None
                    or snapshot(current) != receipt
                    or current.state != "retiring"
                    or await _has_reference(db, identifier)
                ):
                    reject()
                # access() acquires one permit for this attempt. Any other record is
                # an earlier uncertain operation and prevents physical collection.
                operations = list(
                    await db.scalars(
                        select(StorageOperation).where(
                            StorageOperation.purpose == purpose(identifier)
                        )
                    )
                )
                if len(operations) != 1 or operations[0].revision != revision:
                    reject("storage_operation_unsettled")
                await storage_access.check(
                    db,
                    uuid.UUID(receipt.storage_ref["location_id"]),
                    expected_revision=revision,
                    lock=True,
                )
                await db.commit()

            async with access(db, receipt, guarded) as client:
                count = await originals.physical(
                    physical.delete_output,
                    client,
                    receipt,
                    timeout=originals.JOB_SECONDS,
                )
            _, _, _, current = await _lock_context(db, identifier)
            if (
                current is None
                or snapshot(current) != receipt
                or current.state != "retiring"
                or await _has_reference(db, identifier)
                or await _in_use(db, identifier)
            ):
                reject()
            current.state = "deleted"
            await db.commit()
            removed += count
        except (ContentError, physical.VideoDerivativeStorageError):
            await db.rollback()
        except DBAPIError as exc:
            await db.rollback()
            if not storage_access.unavailable(exc):
                raise
    return removed
