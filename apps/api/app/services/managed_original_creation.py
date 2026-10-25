"""Durable allocation, conditional creation and publication without final deletion."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import re
from urllib.parse import urlsplit
import uuid

import boto3
from botocore.config import Config
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.db.models.dataset_asset import (
    DatasetAsset,
    DatasetAssetLocation,
    StorageBinding,
    StorageLocation,
)
from app.db.models.managed_original_creation import ManagedOriginalCreation as Creation
from app.db.models.storage_maintenance import StorageOperation
from app.services import storage_access, storage_usage_accounting as accounting
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
TASK_ORIGINAL_PREFIX = "__aap_task_originals__"
AUXILIARY_ORIGINAL_PREFIX = "__aap_auxiliary_originals__"
ORIGINAL_STORAGE_PURPOSES = frozenset({"datasets", "annotations"})


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
            .where(StorageBinding.purpose.in_(ORIGINAL_STORAGE_PURPOSES))
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
            .where(StorageBinding.purpose.in_(ORIGINAL_STORAGE_PURPOSES))
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
                if purpose in ORIGINAL_STORAGE_PURPOSES
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


async def _change(db, creation_id, expected, *, record_mutation=False, **values):
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
        if record_mutation:
            handle = await accounting.begin_in_transaction(
                connection,
                endpoint_url=row["configuration"]["endpoint"],
                bucket=row["bucket"],
                object_key=row["object_key"],
                producer="managed_original_creation",
                owner_id=row["id"],
                purpose="managed_original",
                kind="create",
                nonce=str(row["id"]),
                new_request=True,
                evidence={"owner_kind": row["owner_kind"], "origin": row["origin"]},
            )
            return dict(row), handle
        return dict(row)


def _original_configuration(configuration):
    """Project old and current snapshots onto this writer's actual dependencies."""
    return {
        **configuration,
        "buckets": {
            purpose: bucket
            for purpose, bucket in configuration.get("buckets", {}).items()
            if purpose in ORIGINAL_STORAGE_PURPOSES
        },
        "bindings": [
            binding
            for binding in configuration.get("bindings", [])
            if binding.get("purpose") in ORIGINAL_STORAGE_PURPOSES
        ],
    }


async def _validate(db, row, *, lock=False):
    saved = row["configuration"]
    # Earlier immutable ledgers include unrelated purposes. Verify their facts
    # before comparing only the dependencies this original writer consumes.
    if _digest(saved) != row["input_fingerprint"]:
        reject("managed_original_configuration_changed")
    configuration = await _configuration(db, lock=lock)
    if _original_configuration(configuration) != _original_configuration(saved):
        reject("managed_original_configuration_changed")
    if row["origin"] == "direct":
        staging_owner(row)
    await storage_access.check(
        db, row["location_id"], expected_revision=row["maintenance_revision"], lock=lock
    )


def staging_owner(row):
    """Interpret old null locators only from their saved creation configuration."""
    from app.services import import_storage

    if row["origin"] != "direct":
        reject("managed_original_creation_input_changed")
    prefix = f"{STAGING_PREFIX}/{row['id']}/"
    reference = row.get("staging_ref")
    if reference is None:
        saved = row["configuration"]
        if (
            _digest(saved) != row["input_fingerprint"]
            or saved.get("buckets", {}).get("import") != row["staging_bucket"]
        ):
            reject("managed_original_configuration_changed")
        reference = _import_call(
            import_storage.legacy_ref,
            prefix,
            endpoint_url=saved.get("endpoint"),
            bucket=row["staging_bucket"],
        )
    owner = _import_call(import_storage.resolve_ref, reference, expected_prefix=prefix)
    if (
        owner.bucket != row["staging_bucket"]
        or owner.key("payload") != row["staging_key"]
    ):
        reject("managed_original_creation_input_changed")
    return owner


def _import_error(exc):
    error = ContentError(exc.code, exc.status_code)
    if getattr(exc, "storage_operation_unsettled", False):
        storage_access.mark_unsettled(error)
    return error


def _import_call(call, *args, **kwargs):
    from app.services import import_storage

    try:
        return call(*args, **kwargs)
    except import_storage.ImportStorageError as exc:
        raise _import_error(exc) from None


async def _import_async(call, *args, **kwargs):
    from app.services import import_storage

    try:
        return await call(*args, **kwargs)
    except import_storage.ImportStorageError as exc:
        raise _import_error(exc) from None


async def validate_staging(db, row, *, lock=False, expected_revision=None):
    from app.services import import_storage

    owner = staging_owner(row)
    await _import_async(import_storage.io_target, db, owner, lock=lock)
    return await import_storage.check_location(
        db, owner, expected_revision=expected_revision, lock=lock
    )


def upload_expires_in(allocation):
    deadline = allocation["allocated_at"] + timedelta(seconds=900)
    seconds = int((deadline - now()).total_seconds())
    if seconds <= 0:
        reject("managed_original_staging_expired")
    return min(seconds, 900)


async def prepare_upload_url(db, allocation):
    """Use the durable source owner before callers enter final authority locks."""
    from app.services import import_storage

    try:
        return await _import_async(
            import_storage.prepare_upload_url,
            db,
            staging_owner(allocation),
            "payload",
            content_type=allocation["content_type"],
            expires_in=upload_expires_in(allocation),
        )
    except ContentError:
        raise
    except Exception as exc:
        error = ContentError("managed_original_staging_unavailable", 503)
        if getattr(exc, "storage_operation_unsettled", False):
            storage_access.mark_unsettled(error)
        raise error from None


async def validate_upload_url(db, allocation, url, *, lock=True):
    """Recheck the exact signing credentials and deadline without provider I/O."""
    from app.services import import_storage

    return await _import_async(
        import_storage.validate_upload_url,
        db,
        url,
        owner=staging_owner(allocation),
        relative="payload",
        content_type=allocation["content_type"],
        lock=lock,
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
    return await _allocate(
        db,
        owner_kind="dataset",
        dataset_id=dataset_id,
        project_id=None,
        task_id=None,
        logical_path=logical_path,
        origin=origin,
        max_bytes=max_bytes,
        requested_by=requested_by,
        authority=authority,
        staging=staging,
        content_type=content_type,
    )


async def allocate_auxiliary(
    db,
    *,
    dataset_id,
    kind,
    source_contract,
    logical_path,
    max_bytes,
    authority,
    requested_by=None,
    content_type="application/octet-stream",
):
    from app.services.auxiliary_originals import contract

    if (
        not isinstance(dataset_id, uuid.UUID)
        or not isinstance(authority, dict)
        or not authority
    ):
        reject("managed_original_invalid_allocation", 422)
    frozen = contract(kind, source_contract)
    return await _allocate(
        db,
        owner_kind="dataset_auxiliary",
        dataset_id=dataset_id,
        project_id=None,
        task_id=None,
        logical_path=logical_path,
        origin="connector",
        max_bytes=max_bytes,
        requested_by=requested_by,
        authority={**authority, "auxiliary": {"kind": kind, "source_contract": frozen}},
        staging=False,
        content_type=content_type,
    )


async def allocate_task(
    db,
    *,
    project_id,
    task_id,
    logical_path,
    max_bytes,
    requested_by=None,
    authority=None,
    content_type="application/octet-stream",
):
    """Reserve an independent Task candidate, never a final browser PUT key."""
    if not isinstance(project_id, uuid.UUID) or not isinstance(task_id, uuid.UUID):
        reject("managed_original_invalid_allocation", 422)
    return await _allocate(
        db,
        owner_kind="task",
        dataset_id=None,
        project_id=project_id,
        task_id=task_id,
        logical_path=logical_path,
        origin="direct",
        max_bytes=max_bytes,
        requested_by=requested_by,
        authority=authority,
        staging=True,
        content_type=content_type,
    )


async def _allocate(
    db,
    *,
    owner_kind,
    dataset_id,
    project_id,
    task_id,
    logical_path,
    origin,
    max_bytes,
    requested_by,
    authority,
    staging,
    content_type,
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
    if owner_kind == "task":
        binding = await db.get(StorageBinding, "annotations", populate_existing=True)
        if binding is None:
            reject("managed_original_configuration_changed")
    configuration = await _configuration(db)
    revision = await storage_access.check(db, LEGACY_LOCATION_ID)
    creation_id = uuid.uuid4()
    staging_ref, staging_owner_value = None, None
    if staging:
        from app.services import import_storage

        prefix = f"{STAGING_PREFIX}/{creation_id}/"
        staging_ref = await _import_async(import_storage.allocate, db, prefix)
        staging_owner_value = _import_call(
            import_storage.resolve_ref, staging_ref, expected_prefix=prefix
        )
    object_key = (
        f"{TASK_ORIGINAL_PREFIX}/{task_id}/{creation_id}/payload"
        if owner_kind == "task"
        else (
            f"{AUXILIARY_ORIGINAL_PREFIX}/{dataset_id}/{creation_id}/payload"
            if owner_kind == "dataset_auxiliary"
            else managed_dataset_key(dataset_id, logical_path)
        )
    )
    if (
        len(object_key) > 1000
        or len(object_key.encode()) > 1024
        or len(binding.bucket.encode()) > 255
        or (
            staging_owner_value is not None
            and (
                len(staging_owner_value.key("payload")) > 1000
                or len(staging_owner_value.key("payload").encode()) > 1024
            )
        )
    ):
        reject("managed_original_invalid_allocation", 422)
    values = {
        "id": creation_id,
        "owner_kind": owner_kind,
        "dataset_id": dataset_id,
        "project_id": project_id,
        "task_id": task_id,
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
        "staging_ref": staging_ref,
        "staging_bucket": staging_owner_value.bucket if staging else None,
        "staging_key": staging_owner_value.key("payload") if staging else None,
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
                        Creation.owner_kind == "dataset",
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


async def for_task(db, *, task_id, object_key):
    """Find a Task-scoped allocation without borrowing a Dataset identity."""
    async with storage_access.engine(db).connect() as connection:
        row = (
            (
                await connection.execute(
                    select(Creation.__table__).where(
                        Creation.owner_kind == "task",
                        Creation.task_id == task_id,
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
    if (
        row is None
        or row.get("staging_ref") != allocation.get("staging_ref")
        or any(
            field not in allocation or row[field] != allocation[field]
            for field in (
                "owner_kind",
                "dataset_id",
                "project_id",
                "task_id",
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


async def _drain(task):
    """Keep ownership through repeated cancellation and signal BaseExceptions."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except BaseException:
            continue
    return task.result()


class _Physical:
    def __init__(self):
        self.interrupted = None

    async def run(self, function, *args, **kwargs):
        future = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        try:
            return await asyncio.shield(future)
        except BaseException as exc:
            if future.done() and (future.cancelled() or future.exception() is not None):
                return future.result()
            # Adopt late client/preparation/receipt results in the caller so its
            # normal persistence and finally-close path still owns every handle.
            if self.interrupted is None:
                self.interrupted = exc
            return await _drain(future)


async def _physical(db, row, stream, *, source_target=None):
    from app.services import managed_original_writer as writer

    client, source_client, source_context = None, None, None
    prepared, completion_started = None, False
    physical = _Physical()
    failure = None
    mutation = None
    evidence = {"physical_outcome": "unknown"}
    settled = False
    try:
        client = await physical.run(
            create_client, timeout_seconds=row["timeout_seconds"]
        )
        if row["origin"] == "direct":
            if source_target is None:
                reject("managed_original_creation_input_changed")
            source_context = source_target.client()
            source_client = await physical.run(source_context.__enter__)
            source = await physical.run(
                writer.inspect_staging,
                source_client,
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
            prepared = await physical.run(
                writer.prepare_copy,
                client,
                row["staging_bucket"],
                row["staging_key"],
                row["bucket"],
                row["object_key"],
                source,
                content_type=row["content_type"],
                timeout_seconds=row["timeout_seconds"],
                source_client=source_client,
            )
        else:
            prepared = await physical.run(
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
        _, mutation = await _change(
            db,
            row["id"],
            "prepared",
            record_mutation=True,
            status="writing",
            write_started_at=now(),
        )
        completion_started = True
        receipt = await physical.run(
            writer.complete,
            client,
            row["bucket"],
            row["object_key"],
            prepared,
            content_type=row["content_type"],
        )
        evidence.update(
            physical_outcome="written",
            physical_size_bytes=prepared.size_bytes,
            returned_version=receipt.response.get("VersionId"),
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
        verified = await physical.run(
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
        evidence["full_readback"] = True
        if receipt.version_id not in {None, "", "null"}:
            evidence["current_head_matches"] = await physical.run(
                accounting.current_matches,
                client,
                row["bucket"],
                row["object_key"],
                receipt.version_id,
                prepared.size_bytes,
            )
            if evidence["current_head_matches"]:
                evidence["current_delta_bytes"] = prepared.size_bytes
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
    except BaseException as exc:
        failure = exc
        if (
            mutation is not None
            and evidence["physical_outcome"] != "written"
            and getattr(exc, "outcome", None) == "conflict"
        ):
            evidence.update(physical_outcome="not_written", current_delta_bytes=0)
        if prepared is not None and not completion_started:
            # A failed freeze/config fence may clean only its exact unfinished
            # MPU. This never deletes or retries the final object.
            try:
                await physical.run(
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
        try:
            closing_failure = None
            for resource, context in (
                (source_client, source_context),
                (client, None),
            ):
                if resource is None:
                    continue
                try:
                    if context is not None:
                        await physical.run(context.__exit__, None, None, None)
                    else:
                        await physical.run(storage_access.close_registered, resource)
                except BaseException:
                    closing_failure = storage_access.mark_unsettled(
                        ContentError("managed_original_close_unknown", 503)
                    )
            if closing_failure is not None:
                raise closing_failure from None
            settled = True
        finally:
            if mutation is not None:
                # A separate best-effort transaction preserves physical facts
                # even when Creation or its caller's business publication fails.
                # Keep the existing permit until native close/drain has finished.
                publication = asyncio.create_task(
                    accounting.publish(
                        async_sessionmaker(
                            storage_access.engine(db), expire_on_commit=False
                        ),
                        mutation,
                        evidence,
                        settled=settled,
                    )
                )
                try:
                    await asyncio.shield(publication)
                except BaseException:
                    await _drain(publication)
                    raise
        if physical.interrupted is not None and failure is None:
            raise physical.interrupted


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
            (
                f"task_original_write_{row['id'].hex}"
                if row["owner_kind"] == "task"
                else "managed_original_creation"
            ),
            expected_revision=row["maintenance_revision"],
        ) as permit:
            await _change(db, row["id"], "preparing", operation_id=permit.id)
            if row["origin"] == "direct":
                from app.services import import_storage

                owner = staging_owner(row)
                target = await _import_async(import_storage.io_target, db, owner)
                async with await storage_access.acquire(
                    db,
                    owner.location_id,
                    "managed_original_source_" + row["id"].hex,
                ) as source_permit:
                    await validate_staging(
                        db, row, expected_revision=source_permit.revision
                    )
                    head_bytes = await _physical(db, row, stream, source_target=target)
                    await validate_staging(
                        db, row, expected_revision=source_permit.revision
                    )
            else:
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
    except BaseException:
        # Do not let request cancellation abandon its source or skip write-ack
        # persistence, readback, client close, or permit settlement.
        try:
            await _drain(task)
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
    if row is None or row["owner_kind"] != "dataset" or row["status"] != "verified":
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
