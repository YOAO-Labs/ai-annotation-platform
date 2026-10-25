"""Saved thumbnail responsibility shared by directory and ordinary image previews."""

import asyncio
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import uuid

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.dataset import DatasetItem
from app.db.models.task import Task
from app.db.models.thumbnail_output import ThumbnailOutput
from app.services import media_cache_storage, media_cache_urls, original_sources
from app.services import storage_access
from app.services.content_snapshot import ContentError
from app.services.filesystem_storage import FilesystemStorageError
from app.services.storage import storage_service

MAX_BYTES = 2 * 1024 * 1024
RELATIVE_KEY = "thumbnail.webp"
WRITE_SECONDS = 120


def now():
    return datetime.now(timezone.utc)


def purpose(identifier):
    return "thumbnail_" + uuid.UUID(str(identifier)).hex


def prefix_for(identifier):
    return f"thumbnails/objects/{uuid.UUID(str(identifier))}/"


def owner(output):
    reference = (
        output.get("storage_ref") if isinstance(output, dict) else output.storage_ref
    )
    identifier = output.get("id") if isinstance(output, dict) else output.id
    return media_cache_storage.resolve_ref(
        reference, expected_prefix=prefix_for(identifier)
    )


def source_facts(source):
    return json.loads(json.dumps(asdict(source), default=str))


def _write(target, reference, data):
    saved = media_cache_storage.resolve_ref(reference)
    key = saved.key(RELATIVE_KEY)
    expected = hashlib.sha256(data).hexdigest()
    attempted = False
    try:
        with target.client(read_timeout=30) as client:
            attempted = True
            try:
                uploaded = client.put_object(
                    Bucket=saved.bucket,
                    Key=key,
                    Body=data,
                    ContentType="image/webp",
                    IfNoneMatch="*",
                )
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code")
                if code in {
                    "AccessDenied",
                    "InvalidAccessKeyId",
                    "SignatureDoesNotMatch",
                    "403",
                    "NoSuchBucket",
                    "404",
                    "PreconditionFailed",
                    "412",
                    "ConditionalRequestConflict",
                    "409",
                }:
                    error = ContentError("thumbnail_write_rejected", 503)
                    error.thumbnail_write_refused = True
                    raise error from None
                raise storage_access.mark_unsettled(exc)
            except BaseException as exc:
                raise storage_access.mark_unsettled(exc)
            etag, version = uploaded.get("ETag"), uploaded.get("VersionId")
            if (
                uploaded.get("ResponseMetadata", {}).get("HTTPStatusCode") != 200
                or not isinstance(etag, str)
                or not etag
                or len(etag) > 1024
                or any(ord(c) < 32 or ord(c) == 127 for c in etag)
                or version is not None
                and (not isinstance(version, str) or not version or len(version) > 1024)
            ):
                raise storage_access.mark_unsettled(
                    ContentError("thumbnail_write_unproven", 503)
                )
            version = None if version == "null" else version
            params = {"Bucket": saved.bucket, "Key": key, "IfMatch": etag}
            if version is not None:
                params["VersionId"] = version
            response = client.get_object(**params)
            with storage_access.closing_registered(response["Body"]) as body:
                observed, extra = body.read(MAX_BYTES + 1), body.read(1)
            observed_version = response.get("VersionId")
            observed_version = None if observed_version == "null" else observed_version
            if (
                response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 200
                or response.get("DeleteMarker")
                or response.get("ContentRange") is not None
                or response.get("ContentLength") != len(data)
                or response.get("ETag") != etag
                or observed_version != version
                or extra
                or observed != data
            ):
                raise ContentError("thumbnail_write_unproven", 503)
            receipt = {
                "sha256": expected,
                "size_bytes": len(data),
                "etag": etag,
                "version_id": version,
            }
        return receipt
    except BaseException as exc:
        if not attempted and not getattr(exc, "storage_operation_unsettled", False):
            exc.thumbnail_write_refused = True
        raise


async def _persist_result(factory, identifier, reference, receipt, failure):
    async with factory() as current:
        output = await current.scalar(
            select(ThumbnailOutput)
            .where(ThumbnailOutput.id == identifier)
            .with_for_update()
        )
        if (
            output is None
            or output.storage_ref != reference
            or output.state != "building"
        ):
            raise ContentError("thumbnail_output_changed")
        if receipt is not None:
            for name, value in receipt.items():
                setattr(output, name, value)
            output.state, output.ready_at = "ready", now()
            output.uncertain_write, output.error_code = False, None
        else:
            output.state = "failed"
            output.uncertain_write = not getattr(
                failure, "thumbnail_write_refused", False
            )
            output.error_code = (
                "thumbnail_write_unproven"
                if output.uncertain_write
                else "thumbnail_write_rejected"
            )
        await current.commit()
        return output


async def create(db, *, owner_kind, owner_id, input_ref, body, path_digest=None):
    """Durably allocate and verify a candidate without committing business rows."""
    if (
        owner_kind not in {"directory_preview", "dataset_thumbnail", "task_thumbnail"}
        or not isinstance(owner_id, uuid.UUID)
        or not isinstance(input_ref, dict)
        or type(body) is not bytes
        or not 0 < len(body) <= MAX_BYTES
    ):
        raise ContentError("thumbnail_input_invalid", 422)
    identifier, allocated_at = uuid.uuid4(), now()
    factory = async_sessionmaker(storage_access.engine(db), expire_on_commit=False)
    frozen = json.loads(json.dumps(input_ref, default=str, allow_nan=False))
    frozen["encoded"] = {
        "sha256": hashlib.sha256(body).hexdigest(),
        "size_bytes": len(body),
    }
    async with factory() as current:
        reference = await media_cache_storage.allocate(current, prefix_for(identifier))
        output = ThumbnailOutput(
            id=identifier,
            owner_kind=owner_kind,
            owner_id=owner_id,
            path_digest=path_digest,
            input_ref=frozen,
            storage_ref=reference,
            state="building",
            writer_until=allocated_at + timedelta(seconds=WRITE_SECONDS),
            next_check_at=allocated_at + timedelta(seconds=WRITE_SECONDS),
            uncertain_write=True,
        )
        current.add(output)
        await current.commit()
    observed, entered, failure = [], [], None
    try:
        async with factory() as current:
            saved = owner(output)
            target = await media_cache_storage.target_for_ref(current, saved.ref)
            revision = await storage_access.check(current, saved.location_id)
            await current.commit()

            def write():
                entered.append(True)
                result = _write(target, reference, body)
                observed.append(result)
                return result

            await storage_access.run(
                current,
                saved.location_id,
                purpose(identifier),
                write,
                expected_revision=revision,
                timeout=WRITE_SECONDS,
            )
    except BaseException as exc:
        if not entered and not getattr(exc, "storage_operation_unsettled", False):
            exc.thumbnail_write_refused = True
        failure = exc
    checkpoint = asyncio.create_task(
        _persist_result(
            factory, identifier, reference, observed[0] if observed else None, failure
        )
    )
    try:
        result = await asyncio.shield(checkpoint)
    except BaseException:
        await storage_access.finish(checkpoint)
        raise
    if failure is not None:
        raise failure
    return result


async def _business_pointer(db, owner_kind, owner_id, path):
    if owner_kind == "task":
        task = await db.get(Task, owner_id, populate_existing=True)
        if task is None:
            return None
        if task.dataset_item_id is not None:
            item = await db.get(
                DatasetItem, task.dataset_item_id, populate_existing=True
            )
            if item is not None and item.thumbnail_path == path:
                return "dataset_thumbnail", item.id, item.thumbnail_output_id
        if task.thumbnail_path == path:
            return "task_thumbnail", task.id, task.thumbnail_output_id
    elif owner_kind == "dataset_item":
        item = await db.get(DatasetItem, owner_id, populate_existing=True)
        if item is not None and item.thumbnail_path == path:
            return "dataset_thumbnail", item.id, item.thumbnail_output_id
    return None


async def _source(db, kind, identifier):
    return await (
        original_sources.resolve_item(db, identifier)
        if kind == "dataset_thumbnail"
        else original_sources.resolve_task(db, identifier)
    )


def _legacy_path(path):
    return (
        isinstance(path, str)
        and path.startswith("thumbnails/")
        and not path.startswith("thumbnails/objects/")
        and len(path) <= 512
        and not any(ord(char) <= 32 or ord(char) == 127 for char in path)
        and not any(char in path for char in "\\?#%")
        and all(part not in {"", ".", ".."} for part in path.split("/"))
    )


def _verified_bytes(target, output):
    saved = owner(output)
    params = {"Bucket": saved.bucket, "Key": saved.key(RELATIVE_KEY)}
    if output.version_id not in (None, "null"):
        params["VersionId"] = output.version_id
    if getattr(output, "etag", None) is not None:
        params["IfMatch"] = output.etag
    with target.client(read_timeout=30) as client:
        response = client.get_object(**params)
        with storage_access.closing_registered(response["Body"]) as body:
            data = body.read(MAX_BYTES + 1)
            extra = body.read(1)
        if (
            response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 200
            or response.get("ContentRange") is not None
            or response.get("DeleteMarker")
            or getattr(output, "etag", None) is not None
            and response.get("ETag") != output.etag
            or output.version_id not in (None, "null")
            and response.get("VersionId") != output.version_id
            or response.get("ContentLength") != output.size_bytes
            or len(data) != output.size_bytes
            or len(data) > MAX_BYTES
            or extra
            or hashlib.sha256(data).hexdigest() != output.sha256
        ):
            raise ContentError("thumbnail_content_changed")
    return data


async def read_bytes(db, output_id):
    """Verify a ready saved output without committing caller-owned changes."""
    factory = async_sessionmaker(storage_access.engine(db), expire_on_commit=False)
    async with factory() as current:
        output = await current.get(ThumbnailOutput, output_id)
        if output is None or output.state != "ready" or output.uncertain_write:
            raise ContentError("thumbnail_output_changed")
        saved = owner(output)
        revision = await storage_access.check(current, saved.location_id)
        await current.commit()
        async with await storage_access.acquire(
            current, saved.location_id, purpose(output_id), expected_revision=revision
        ):
            async with original_sources._reads(current, True):
                output = await current.get(
                    ThumbnailOutput,
                    output_id,
                    populate_existing=True,
                    with_for_update={"read": True, "nowait": True},
                )
            if (
                output is None
                or output.state != "ready"
                or output.uncertain_write
                or output.storage_ref != saved.ref
            ):
                raise ContentError("thumbnail_output_changed")
            target = await media_cache_storage.target_for_ref(current, saved.ref)
            await storage_access.check(
                current, saved.location_id, expected_revision=revision
            )
            await current.commit()
            pending = asyncio.create_task(
                asyncio.to_thread(_verified_bytes, target, output)
            )
            try:
                return await asyncio.shield(pending)
            except BaseException as interrupted:
                try:
                    await storage_access.finish(pending)
                except BaseException as late:
                    if getattr(late, "storage_operation_unsettled", False):
                        storage_access.mark_unsettled(interrupted)
                raise


async def thumbnail_url(db, owner_kind, owner_id, path):
    """Prepare an authorized serializer's checked URL; never infer B from a path."""
    if not path or owner_kind not in {"task", "dataset_item"}:
        return None
    factory = async_sessionmaker(storage_access.engine(db), expire_on_commit=False)
    async with factory() as current:
        pointer = await _business_pointer(current, owner_kind, owner_id, path)
        if pointer is None:
            return None
        kind, identifier, output_id = pointer
        if output_id is None:
            return (
                storage_service.generate_download_url(
                    path, bucket=storage_service.media_cache_bucket
                )
                if _legacy_path(path)
                else None
            )
        try:
            output = await current.get(
                ThumbnailOutput, output_id, populate_existing=True
            )
            if (
                output is None
                or output.state != "ready"
                or output.uncertain_write
                or (output.owner_kind, output.owner_id) != (kind, identifier)
                or owner(output).key(RELATIVE_KEY) != path
                or source_facts(await _source(current, kind, identifier))
                != output.input_ref.get("source")
            ):
                return None
            saved = owner(output)
            revision = await storage_access.check(current, saved.location_id)
            await current.commit()
            await read_bytes(current, output.id)
            if await _business_pointer(
                current, owner_kind, owner_id, path
            ) != pointer or source_facts(
                await _source(current, kind, identifier)
            ) != output.input_ref.get("source"):
                return None
            output = await current.get(
                ThumbnailOutput, output_id, populate_existing=True
            )
            if output is None or output.state != "ready" or output.uncertain_write:
                return None
            await media_cache_storage.target_for_ref(current, saved.ref)
            await storage_access.check(
                current, saved.location_id, expected_revision=revision
            )
            return media_cache_urls.sign(
                "thumbnail",
                output.id,
                path,
                saved.ref,
                {"sha256": output.sha256, "size_bytes": output.size_bytes},
                3600,
            )
        except (
            ContentError,
            media_cache_storage.MediaCacheStorageError,
            FilesystemStorageError,
            BotoCoreError,
            ClientError,
        ):
            return None
