"""Durable allocation, conditional creation and publication without final deletion."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import re
from urllib.parse import urlsplit
import uuid

import boto3
from botocore.config import Config
from sqlalchemy import insert, select, update

from app.config import settings
from app.db.models.dataset_asset import (
    DatasetAsset,
    DatasetAssetLocation,
    StorageBinding,
    StorageLocation,
)
from app.db.models.managed_original_creation import ManagedOriginalCreation as Creation
from app.db.models.storage_maintenance import StorageOperation
from app.services import storage_access
from app.services.content_snapshot import ContentError
from app.services.dataset_assets import (
    LEGACY_LOCATION_ID,
    PURPOSE_BUCKET_ATTRIBUTES,
    ensure_legacy_bindings,
    validate_legacy_bindings,
)
from app.services.dataset_storage_keys import (
    managed_dataset_key,
    normalize_dataset_relative_path,
)
from app.services.managed_original_retirement import managed_storage_identity
from app.services.storage import storage_service

ORIGINS = {"direct", "zip", "connector", "directory_copy"}
TERMINAL = {"verified", "failed", "conflict", "unknown"}
_SAFE_CODE = re.compile(r"[a-z][a-z0-9_]{0,79}")
STAGING_PREFIX = "__aap_upload_staging__"


@dataclass(frozen=True)
class CreatedOriginal:
    creation_id: uuid.UUID
    sha256: str
    size_bytes: int
    md5: str
    etag: str
    version_id: str | None
    head_bytes: bytes


def now():
    return datetime.now(timezone.utc)


def reject(code, status=409):
    raise ContentError(code, status)


def _json(value):
    return json.loads(json.dumps(value, default=str))


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def create_client(*, timeout_seconds=60):
    """Each physical operation owns one nonretrying, same-origin client."""
    endpoint = (
        f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}"
    )
    target = urlsplit(endpoint)

    def guard(request, **kwargs):
        destination = urlsplit(request.url)
        if (destination.scheme, destination.netloc) != (target.scheme, target.netloc):
            reject("managed_original_redirect_denied")

    try:
        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=settings.minio_access_key,
            aws_secret_access_key=settings.minio_secret_key,
            config=Config(
                connect_timeout=2,
                read_timeout=max(10, min(timeout_seconds, 3600)),
                retries={"total_max_attempts": 1, "mode": "standard"},
                s3={"addressing_style": "path"},
                proxies={},
                signature_version="s3v4",
            ),
        )
        client.meta.events.register("before-send.s3", guard)
        return client
    except BaseException:
        raise storage_access.mark_unsettled(
            ContentError("managed_original_client_unknown", 503)
        ) from None


async def _configuration(db, *, lock=False):
    if lock:
        await db.execute(
            select(StorageLocation.id)
            .where(StorageLocation.id == LEGACY_LOCATION_ID)
            .with_for_update(read=True)
        )
        await db.execute(
            select(StorageBinding.purpose)
            .order_by(StorageBinding.purpose)
            .with_for_update(read=True)
        )
    if await validate_legacy_bindings(db):
        reject("managed_original_configuration_changed")
    location = await db.get(StorageLocation, LEGACY_LOCATION_ID, populate_existing=True)
    if location is None or not location.enabled:
        reject("managed_original_configuration_changed")
    bindings = list(
        await db.scalars(
            select(StorageBinding)
            .order_by(StorageBinding.purpose)
            .execution_options(populate_existing=True)
        )
    )
    return _json(
        {
            "endpoint": f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}",
            "buckets": {
                purpose: getattr(storage_service, attr)
                for purpose, attr in PURPOSE_BUCKET_ATTRIBUTES.items()
            },
            "location": {
                column.key: getattr(location, column.key)
                for column in StorageLocation.__table__.columns
            },
            "bindings": [
                {
                    column.key: getattr(row, column.key)
                    for column in StorageBinding.__table__.columns
                }
                for row in bindings
            ],
        }
    )


async def _read(db, creation_id):
    async with storage_access.engine(db).connect() as connection:
        row = (
            (
                await connection.execute(
                    select(Creation.__table__).where(Creation.id == creation_id)
                )
            )
            .mappings()
            .one_or_none()
        )
        return dict(row) if row else None


async def _change(db, creation_id, expected, **values):
    """No independent ledger transaction spans executor or network I/O."""
    async with storage_access.engine(db).begin() as connection:
        row = (
            (
                await connection.execute(
                    update(Creation)
                    .where(
                        Creation.id == creation_id,
                        Creation.status.in_(
                            [expected] if isinstance(expected, str) else expected
                        ),
                    )
                    .values(**values)
                    .returning(*Creation.__table__.columns)
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            reject("managed_original_creation_state_conflict")
        return dict(row)


async def _validate(db, row, *, lock=False):
    configuration = await _configuration(db, lock=lock)
    if (
        _digest(configuration) != row["input_fingerprint"]
        or configuration != row["configuration"]
    ):
        reject("managed_original_configuration_changed")
    await storage_access.check(
        db, row["location_id"], expected_revision=row["maintenance_revision"], lock=lock
    )


async def allocate(
    db,
    *,
    dataset_id,
    logical_path,
    origin,
    max_bytes,
    requested_by=None,
    authority=None,
    staging=False,
    content_type="application/octet-stream",
):
    if (
        origin not in ORIGINS
        or type(max_bytes) is not int
        or not 0 <= max_bytes <= 2**63 - 1
        or staging != (origin == "direct")
    ):
        reject("managed_original_invalid_allocation", 422)
    if (
        not isinstance(content_type, str)
        or not content_type
        or len(content_type) > 255
        or any(c in content_type for c in "\r\n\x00")
    ):
        reject("managed_original_invalid_allocation", 422)
    try:
        logical_path = normalize_dataset_relative_path(logical_path)
    except (AttributeError, TypeError, ValueError):
        reject("managed_original_invalid_allocation", 422)
    binding = await ensure_legacy_bindings(db)
    configuration = await _configuration(db)
    revision = await storage_access.check(db, LEGACY_LOCATION_ID)
    creation_id = uuid.uuid4()
    object_key = managed_dataset_key(dataset_id, logical_path)
    if (
        len(object_key) > 1000
        or len(object_key.encode()) > 1024
        or len(binding.bucket.encode()) > 255
    ):
        reject("managed_original_invalid_allocation", 422)
    values = {
        "id": creation_id,
        "dataset_id": dataset_id,
        "location_id": LEGACY_LOCATION_ID,
        "storage_identity": managed_storage_identity(
            configuration["endpoint"], binding.bucket
        ),
        "bucket": binding.bucket,
        "object_key": object_key,
        "logical_path": logical_path,
        "origin": origin,
        "max_bytes": max_bytes,
        "timeout_seconds": max(60, min(3600, math.ceil(max_bytes / (8 * 1024**2)))),
        "content_type": content_type,
        "requested_by": requested_by,
        "authority": _json(authority or {}),
        "configuration": configuration,
        "input_fingerprint": _digest(configuration),
        "maintenance_revision": revision,
        "staging_bucket": storage_service.import_bucket if staging else None,
        "staging_key": f"{STAGING_PREFIX}/{creation_id}/payload" if staging else None,
        "status": "allocated",
    }
    async with storage_access.engine(db).begin() as connection:
        row = (
            (
                await connection.execute(
                    insert(Creation)
                    .values(**values)
                    .returning(*Creation.__table__.columns)
                )
            )
            .mappings()
            .one()
        )
        return dict(row)


async def for_key(db, *, dataset_id, object_key):
    async with storage_access.engine(db).connect() as connection:
        row = (
            (
                await connection.execute(
                    select(Creation.__table__).where(
                        Creation.dataset_id == dataset_id,
                        Creation.object_key == object_key,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        row = dict(row) if row else None
    if row:
        await _validate(db, row)
    return row


def _result(row, head_bytes=b""):
    return CreatedOriginal(
        row["id"],
        row["sha256"],
        row["size_bytes"],
        row["md5"],
        row["etag"],
        row["version_id"],
        head_bytes,
    )


async def _loaded(db, allocation):
    row = await _read(db, allocation["id"])
    if row is None or any(
        row[field] != allocation[field]
        for field in (
            "dataset_id",
            "location_id",
            "storage_identity",
            "bucket",
            "object_key",
            "logical_path",
            "origin",
            "max_bytes",
            "timeout_seconds",
            "content_type",
            "requested_by",
            "authority",
            "configuration",
            "input_fingerprint",
            "maintenance_revision",
            "staging_bucket",
            "staging_key",
        )
    ):
        reject("managed_original_creation_not_found", 404)
    await _validate(db, row)
    return row


def _proof(proof):
    return {"sha256": proof.sha256, "md5": proof.md5, "size_bytes": proof.size_bytes}


def _error(exc):
    code = getattr(exc, "code", None)
    return (
        code
        if isinstance(code, str) and _SAFE_CODE.fullmatch(code)
        else "managed_original_creation_failed"
    )


async def _physical(db, row, stream):
    from app.services import managed_original_writer as writer

    client, prepared, completion_started = None, None, False
    try:
        client = await asyncio.to_thread(
            create_client, timeout_seconds=row["timeout_seconds"]
        )
        if row["origin"] == "direct":
            source = await asyncio.to_thread(
                writer.inspect_staging,
                client,
                row["staging_bucket"],
                row["staging_key"],
                max_bytes=row["max_bytes"],
                timeout_seconds=row["timeout_seconds"],
            )
            await _change(
                db,
                row["id"],
                "preparing",
                source={
                    **_proof(source),
                    "etag": source.etag,
                    "version_id": source.version_id,
                    "observation": _json(source.observation),
                },
            )
            prepared = await asyncio.to_thread(
                writer.prepare_copy,
                client,
                row["staging_bucket"],
                row["staging_key"],
                row["bucket"],
                row["object_key"],
                source,
                content_type=row["content_type"],
                timeout_seconds=row["timeout_seconds"],
            )
        else:
            prepared = await asyncio.to_thread(
                writer.prepare_stream,
                client,
                row["bucket"],
                row["object_key"],
                stream,
                content_type=row["content_type"],
                max_bytes=row["max_bytes"],
                timeout_seconds=row["timeout_seconds"],
            )
        if prepared.bucket != row["bucket"] or prepared.object_key != row["object_key"]:
            reject("managed_original_preparation_mismatch")
        await _validate(db, row)
        await _change(
            db,
            row["id"],
            "preparing",
            status="prepared",
            frozen_at=now(),
            **_proof(prepared),
            prepared={
                **_proof(prepared),
                "upload_id": prepared.upload_id,
                "parts": _json(prepared.parts),
                "bucket": prepared.bucket,
                "object_key": prepared.object_key,
            },
        )
        await _change(
            db, row["id"], "prepared", status="writing", write_started_at=now()
        )
        completion_started = True
        receipt = await asyncio.to_thread(
            writer.complete,
            client,
            row["bucket"],
            row["object_key"],
            prepared,
            content_type=row["content_type"],
        )
        try:
            await _change(
                db,
                row["id"],
                "writing",
                status="written",
                acknowledged_at=now(),
                receipt={
                    "etag": receipt.etag,
                    "version_id": receipt.version_id,
                    "request_id": receipt.request_id,
                    "response": _json(receipt.response),
                },
                etag=receipt.etag,
                version_id=receipt.version_id,
                request_id=receipt.request_id,
            )
        except BaseException:
            raise storage_access.mark_unsettled(
                ContentError("managed_original_receipt_persistence_unknown", 503)
            ) from None
        verified = await asyncio.to_thread(
            writer.verify,
            client,
            row["bucket"],
            row["object_key"],
            receipt,
            sha256=prepared.sha256,
            size_bytes=prepared.size_bytes,
            max_bytes=row["max_bytes"],
            timeout_seconds=row["timeout_seconds"],
        )
        if _proof(verified) != _proof(prepared):
            reject("managed_original_readback_mismatch")
        try:
            await _change(
                db,
                row["id"],
                "written",
                verification={
                    **_proof(verified),
                    "observation": _json(verified.observation),
                },
                readback_at=now(),
            )
        except BaseException:
            raise storage_access.mark_unsettled(
                ContentError("managed_original_readback_persistence_unknown", 503)
            ) from None
        return verified.head_bytes
    except BaseException:
        if prepared is not None and not completion_started:
            # A failed freeze/config fence may clean only its exact unfinished
            # MPU. This never deletes or retries the final object.
            try:
                await asyncio.to_thread(
                    writer.abort_prepared,
                    client,
                    row["bucket"],
                    row["object_key"],
                    prepared,
                )
            except BaseException:
                raise storage_access.mark_unsettled(
                    ContentError("managed_original_abort_unknown", 503)
                ) from None
        raise
    finally:
        if client is not None:
            try:
                await asyncio.to_thread(storage_access.close_registered, client)
            except BaseException:
                raise storage_access.mark_unsettled(
                    ContentError("managed_original_close_unknown", 503)
                ) from None


async def _execute(db, allocation, stream):
    row = await _loaded(db, allocation)
    if row["status"] == "verified":
        return _result(row)
    if row["status"] != "allocated":
        reject(
            "managed_original_creation_active"
            if row["status"] not in TERMINAL
            else "managed_original_creation_terminal"
        )
    row = await _change(
        db, row["id"], "allocated", status="preparing", claimed_at=now()
    )
    try:
        async with await storage_access.acquire(
            db,
            row["location_id"],
            "managed_original_creation",
            expected_revision=row["maintenance_revision"],
        ) as permit:
            await _change(db, row["id"], "preparing", operation_id=permit.id)
            head_bytes = await _physical(db, row, stream)
        await _validate(db, row)
        row = await _change(
            db,
            row["id"],
            "written",
            status="verified",
            settled_at=now(),
            completed_at=now(),
        )
        return _result(row, head_bytes)
    except BaseException as exc:
        try:
            current = await _read(db, row["id"])
            operation = None
            if current and current["operation_id"]:
                async with storage_access.engine(db).connect() as connection:
                    operation = await connection.scalar(
                        select(StorageOperation.id).where(
                            StorageOperation.id == current["operation_id"]
                        )
                    )
            if current and current["status"] not in TERMINAL:
                outcome = (
                    "unknown"
                    if operation or getattr(exc, "storage_operation_unsettled", False)
                    else (
                        "conflict"
                        if getattr(exc, "outcome", None) == "conflict"
                        else "failed"
                    )
                )
                await _change(
                    db,
                    row["id"],
                    current["status"],
                    status=outcome,
                    error_code=_error(exc),
                    completed_at=now(),
                )
            status = (
                503
                if operation or getattr(exc, "storage_operation_unsettled", False)
                else (
                    413
                    if _error(exc) == "creation_byte_budget_exceeded"
                    else getattr(exc, "status", 409)
                )
            )
        except BaseException:
            raise storage_access.mark_unsettled(
                ContentError("managed_original_persistence_unknown", 503)
            ) from None
        error = ContentError(_error(exc), status)
        if operation or getattr(exc, "storage_operation_unsettled", False):
            storage_access.mark_unsettled(error)
        raise error from None


async def _shielded(db, allocation, stream):
    task = asyncio.create_task(_execute(db, allocation, stream))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Do not let request cancellation abandon its source or skip write-ack
        # persistence, readback, client close, or permit settlement.
        try:
            await storage_access.finish(task)
        except BaseException:
            pass
        raise


async def create_stream(db, allocation, stream, *, content_type):
    if allocation["origin"] == "direct" or allocation["content_type"] != content_type:
        reject("managed_original_creation_input_changed")
    return await _shielded(db, allocation, stream)


async def complete_direct(db, allocation):
    if allocation["origin"] != "direct":
        reject("managed_original_creation_input_changed")
    return await _shielded(db, allocation, None)


async def attach(db, item, creation_id):
    row = await _read(db, creation_id)
    if row is None or row["status"] != "verified":
        reject("managed_original_creation_unverified")
    await _validate(db, row, lock=True)
    asset = await db.scalar(
        select(DatasetAsset)
        .where(DatasetAsset.dataset_item_id == item.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    replica = (
        await db.get(
            DatasetAssetLocation,
            (asset.id, row["location_id"], row["bucket"]),
            populate_existing=True,
        )
        if asset
        else None
    )
    if (
        item.dataset_id != row["dataset_id"]
        or item.file_path != row["object_key"]
        or asset is None
        or asset.verification_state != "verified"
        or asset.checksum_algorithm != "sha256"
        or asset.checksum_value != row["sha256"]
        or asset.size_bytes != row["size_bytes"]
        or replica is None
        or replica.object_key != row["object_key"]
        or replica.role != "managed_original"
        or replica.creation_id not in (None, creation_id)
    ):
        reject("managed_original_creation_attachment_mismatch")
    replica.creation_id = creation_id
    await db.flush()
