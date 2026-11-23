"""Saved thumbnail responsibility shared by directory and ordinary image previews."""

import asyncio
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import uuid

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services import media_cache_storage, media_cache_urls, original_sources
from app.services import media_cache_accounting
from app.services import storage_access
from app.services import storage_migration_artifact_reads as migrated_reads
from app.services import storage_migration_read_publications as publications
from app.services.content_snapshot import ContentError
from app.services.labeling_core_read_relations import get_core_row, task_row
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


@dataclass(frozen=True, slots=True)
class ThumbnailFacts:
    id: uuid.UUID
    owner_kind: str
    owner_id: uuid.UUID
    path_digest: str | None
    input_ref: dict
    storage_ref: dict
    state: str
    uncertain_write: bool
    sha256: str | None
    size_bytes: int | None
    etag: str | None
    version_id: str | None
    writer_until: datetime


@dataclass(frozen=True, slots=True)
class ThumbnailBusinessFacts:
    id: uuid.UUID
    dataset_id: uuid.UUID | None
    dataset_item_id: uuid.UUID | None
    project_id: uuid.UUID | None
    file_type: str
    width: int | None
    height: int | None
    thumbnail_output_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class DirectoryPreviewFacts:
    source_id: uuid.UUID
    path_digest: str
    thumbnail_output_id: uuid.UUID | None
    source_sha256: str
    spec: str
    location_id: uuid.UUID | None
    bucket: str | None
    object_key: str | None
    sha256: str | None
    size_bytes: int | None


def _uuid(value):
    return uuid.UUID(value) if value is not None else None


def _facts(record):
    if record is None:
        return None
    return ThumbnailFacts(
        **{
            **record,
            "id": uuid.UUID(record["id"]),
            "owner_id": uuid.UUID(record["owner_id"]),
            "writer_until": datetime.fromisoformat(record["writer_until"]),
        }
    )


async def get_output(db, identifier, *, lock=False):
    if db.autoflush:
        await db.flush()
    return _facts(
        await db.scalar(
            text("SELECT yoao_assets_v1.legacy_thumbnail_output(:id, :lock)"),
            {"id": identifier, "lock": lock},
        )
    )


async def business_facts(db, kind, identifier, *, lock=False):
    record = await db.scalar(
        text("SELECT yoao_assets_v1.legacy_thumbnail_business(:kind, :id, :lock)"),
        {"kind": kind, "id": identifier, "lock": lock},
    )
    if record is None:
        return None
    return ThumbnailBusinessFacts(
        **{
            **record,
            "id": uuid.UUID(record["id"]),
            "dataset_id": _uuid(record["dataset_id"]),
            "dataset_item_id": _uuid(record["dataset_item_id"]),
            "project_id": _uuid(record["project_id"]),
            "thumbnail_output_id": _uuid(record["thumbnail_output_id"]),
        }
    )


async def set_item_dimensions(db, identifier, width, height):
    await db.execute(
        text("SELECT yoao_assets_v1.set_legacy_item_dimensions(:id, :width, :height)"),
        {"id": identifier, "width": width, "height": height},
    )


async def record_item_error(db, identifier, error):
    await db.execute(
        text("SELECT yoao_assets_v1.record_legacy_item_thumbnail_error(:id, :error)"),
        {"id": identifier, "error": error},
    )


async def publish_ordinary(db, kind, identifier, output, blurhash):
    async with original_sources._reads(db, True):
        applied = await db.scalar(
            text(
                "SELECT yoao_assets_v1.publish_legacy_thumbnail("
                ":kind, :owner, :id, :input, :ref, :blurhash, :at)"
            ).bindparams(
                bindparam("input", type_=JSONB), bindparam("ref", type_=JSONB)
            ),
            {
                "kind": kind,
                "owner": identifier,
                "id": output.id,
                "input": output.input_ref,
                "ref": output.storage_ref,
                "blurhash": blurhash,
                "at": now(),
            },
        )
    if not applied:
        raise ContentError("thumbnail_output_changed")


async def directory_candidate(db, identifier, source_id, path_digest, directory):
    return _facts(
        await db.scalar(
            text(
                "SELECT yoao_assets_v1.legacy_directory_thumbnail_candidate("
                ":id, :source, :path, :directory)"
            ).bindparams(bindparam("directory", type_=JSONB)),
            {
                "id": identifier,
                "source": source_id,
                "path": path_digest,
                "directory": directory,
            },
        )
    )


async def retire_directory_output(db, identifier):
    await db.execute(
        text(
            "SELECT yoao_assets_v1.retire_legacy_directory_thumbnail("
            ":id, :retired_at, :next_check_at)"
        ),
        {"id": identifier, "retired_at": now(), "next_check_at": now()},
    )


async def directory_evidence(db, source_ids):
    if db.autoflush:
        await db.flush()
    rows = await db.execute(
        text("SELECT * FROM yoao_assets_v1.directory_thumbnail_evidence(:sources)"),
        {"sources": source_ids},
    )
    return (
        (
            DirectoryPreviewFacts(
                **{
                    **preview,
                    "source_id": uuid.UUID(preview["source_id"]),
                    "thumbnail_output_id": _uuid(preview["thumbnail_output_id"]),
                    "location_id": _uuid(preview["location_id"]),
                }
            ),
            _facts(output),
            location_id,
        )
        for preview, output, location_id in rows
    )


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


def _write(target, reference, data, *, accounting=None):
    saved = media_cache_storage.resolve_ref(reference)
    key = saved.key(RELATIVE_KEY)
    expected = hashlib.sha256(data).hexdigest()
    attempted = False
    try:
        with target.client(
            read_timeout=30,
            **({"single_attempt": True} if accounting is not None else {}),
        ) as client:

            def submitting():
                nonlocal attempted
                attempted = True

            try:
                uploaded, attempt = media_cache_accounting.put(
                    accounting,
                    client,
                    key,
                    {"sha256": expected, "size_bytes": len(data)},
                    Bucket=saved.bucket,
                    Key=key,
                    Body=data,
                    ContentType="image/webp",
                    IfNoneMatch="*",
                    submitting=submitting,
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
                if attempted:
                    storage_access.mark_unsettled(exc)
                raise
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
            if attempt is not None:
                attempt.verified(client, observed_version)
            receipt = {
                "sha256": expected,
                "size_bytes": len(data),
                "etag": etag,
                "version_id": version,
            }
        return receipt
    except BaseException as exc:
        if not attempted and not storage_access.failure_unsettled(exc):
            exc.thumbnail_write_refused = True
        raise


async def _persist_result(factory, identifier, reference, receipt, failure):
    async with factory() as current:
        unsettled = storage_access.failure_unsettled(failure)
        ready = receipt is not None and not unsettled
        uncertain = not ready and (
            unsettled or not getattr(failure, "thumbnail_write_refused", False)
        )
        record = await current.scalar(
            text(
                "SELECT yoao_assets_v1.record_legacy_thumbnail_result("
                ":id, :ref, :sha256, :size_bytes, :etag, :version_id, :ready, :uncertain, :at)"
            ).bindparams(bindparam("ref", type_=JSONB)),
            {
                "id": identifier,
                "ref": reference,
                **(
                    receipt
                    if receipt is not None
                    else dict.fromkeys(("sha256", "size_bytes", "etag", "version_id"))
                ),
                "ready": ready,
                "uncertain": uncertain,
                "at": now() if ready else None,
            },
        )
        if record is None:
            raise ContentError("thumbnail_output_changed")
        output = _facts(record)
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
    async with (
        storage_access.engine(db).connect() as business,
        AsyncSession(bind=business, expire_on_commit=False) as current,
    ):
        from app.services import fixture_media_execution

        execution = fixture_media_execution.current()
        scope = nullcontext()
        if execution is not None:
            item_value = input_ref["source"]["item_id"]
            item_id = uuid.UUID(item_value) if item_value else None
            item = (
                await business_facts(current, "dataset_item", item_id)
                if item_id is not None
                else None
            )
            dataset_id = item.dataset_id if item is not None else None
            task_id = (
                owner_id if item_id is None and owner_kind == "task_thumbnail" else None
            )
            scope = execution.birth_scope(current, dataset_id, item_id, task_id)
        async with scope:
            reference = await media_cache_storage.allocate(
                current, prefix_for(identifier)
            )
            if not await current.scalar(
                text(
                    "SELECT yoao_assets_v1.allocate_legacy_thumbnail("
                    ":id, :kind, :owner, :path, :input, :ref, :at)"
                ).bindparams(
                    bindparam("input", type_=JSONB), bindparam("ref", type_=JSONB)
                ),
                {
                    "id": identifier,
                    "kind": owner_kind,
                    "owner": owner_id,
                    "path": path_digest,
                    "input": frozen,
                    "ref": reference,
                    "at": allocated_at,
                },
            ):
                raise media_cache_storage.MediaCacheStorageError(
                    "media_cache_storage_binding_unproven"
                )
            await current.commit()
    recorder = media_cache_accounting.Recorder(
        factory,
        "thumbnail_outputs",
        identifier,
        reference["endpoint_url"],
        reference["bucket"],
    )
    observed, entered, failure = [], [], None
    try:
        async with factory() as current:
            saved = media_cache_storage.resolve_ref(
                reference, expected_prefix=prefix_for(identifier)
            )
            target = await media_cache_storage.target_for_ref(current, saved.ref)
            revision = await storage_access.check(current, saved.location_id)
            await current.commit()

            def write():
                entered.append(True)
                result = _write(target, reference, body, accounting=recorder)
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
        if not entered and not storage_access.failure_unsettled(exc):
            exc.thumbnail_write_refused = True
        failure = exc
    try:
        await recorder.finish(settled=not storage_access.failure_unsettled(failure))
    except BaseException as exc:
        failure = (
            exc
            if failure is None
            else BaseExceptionGroup(
                "Thumbnail write and accounting settlement failed", [failure, exc]
            )
        )
    checkpoint = asyncio.create_task(
        _persist_result(
            factory, identifier, reference, observed[0] if observed else None, failure
        )
    )
    try:
        result = await asyncio.shield(checkpoint)
    except BaseException as interrupted:
        failures = [interrupted] if failure is None else [failure, interrupted]
        try:
            await storage_access.finish(checkpoint)
        except BaseException as checkpoint_error:
            if checkpoint_error is not interrupted:
                failures.append(checkpoint_error)
        if len(failures) > 1:
            raise BaseExceptionGroup(
                "Thumbnail publication and checkpoint failed", failures
            ) from None
        raise
    if failure is not None:
        raise failure
    return result


async def _item_pointer(db, identifier, path):
    if db.autoflush:
        await db.flush()
    row = (
        await db.execute(
            text(
                "SELECT output_id FROM yoao_assets_v1.legacy_item_thumbnail_pointer(:id, :path)"
            ),
            {"id": identifier, "path": path},
        )
    ).one_or_none()
    return ("dataset_thumbnail", identifier, row.output_id) if row is not None else None


async def _business_pointer(db, owner_kind, owner_id, path):
    if owner_kind == "task":
        task = await get_core_row(db, task_row, owner_id, populate_existing=True)
        if task is None:
            return None
        if task.dataset_item_id is not None:
            pointer = await _item_pointer(db, task.dataset_item_id, path)
            if pointer is not None:
                return pointer
        if task.thumbnail_path == path:
            return "task_thumbnail", task.id, task.thumbnail_output_id
    elif owner_kind == "dataset_item":
        return await _item_pointer(db, owner_id, path)
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
    with target.client(read_timeout=30) as client:
        return read_from_client(client, output)


def read_from_client(client, output):
    """Verify saved logical bytes while retaining the actual transport headers."""
    saved = owner(output)
    route = client.route if isinstance(client, migrated_reads.ReadClient) else None
    expected_version = (
        route.target_version_id if route is not None else output.version_id
    )
    expected_etag = (
        route.target_etag if route is not None else getattr(output, "etag", None)
    )
    params = {"Bucket": saved.bucket, "Key": saved.key(RELATIVE_KEY)}
    if output.version_id not in (None, "null"):
        params["VersionId"] = output.version_id
    if getattr(output, "etag", None) is not None:
        params["IfMatch"] = output.etag
    response = client.get_object(**params)
    with storage_access.closing_registered(response["Body"]) as body:
        data = body.read(MAX_BYTES + 1)
        extra = body.read(1)
    if (
        response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 200
        or response.get("ContentRange") is not None
        or response.get("DeleteMarker")
        or expected_etag is not None
        and response.get("ETag") != expected_etag
        or expected_version not in (None, "null")
        and response.get("VersionId") != expected_version
        or response.get("ContentLength") != output.size_bytes
        or len(data) != output.size_bytes
        or len(data) > MAX_BYTES
        or extra
        or hashlib.sha256(data).hexdigest() != output.sha256
    ):
        raise ContentError("thumbnail_content_changed")
    return data


async def qualify_read(db, output):
    """Qualify saved reads without committing or opening a physical client."""
    saved = owner(output)
    route = await migrated_reads.resolve(
        db,
        saved.ref,
        saved.key(RELATIVE_KEY),
        output.sha256,
        output.size_bytes,
        source_version_id=(
            output.version_id
            if output.version_id is not None
            else publications.ANY_VERSION
        ),
        source_etag=getattr(output, "etag", None),
    )
    if route is not None:
        return route, None
    await media_cache_storage.target_for_ref(db, saved.ref)
    return None, await storage_access.check(db, saved.location_id)


async def _recheck_read(db, saved, route, revision):
    if route is not None:
        await publications.recheck(db, route, lock=True)
    else:
        target = await media_cache_storage.target_for_ref(db, saved.ref)
        await storage_access.check(db, saved.location_id, expected_revision=revision)
        return target


def _receipt(output):
    return (
        deepcopy(output.storage_ref),
        output.sha256,
        output.size_bytes,
        getattr(output, "etag", None),
        output.version_id,
    )


def _same_source(output, source):
    current, expected = source_facts(source), output.input_ref.get("source")
    if not isinstance(expected, dict):
        return False
    transport = {"maintenance_revision", "read_route"}
    return {key: value for key, value in current.items() if key not in transport} == {
        key: value for key, value in expected.items() if key not in transport
    }


async def read_bytes(db, output_id):
    """Verify a ready saved output without committing caller-owned changes."""
    factory = async_sessionmaker(storage_access.engine(db), expire_on_commit=False)
    async with factory() as current:
        output = await get_output(current, output_id)
        if output is None or output.state != "ready" or output.uncertain_write:
            raise ContentError("thumbnail_output_changed")
        saved = owner(output)
        expected = _receipt(output)
        route, revision = await qualify_read(current, output)
        await current.commit()
        target = None

        async def acquired(permit):
            nonlocal output, target
            async with original_sources._reads(current, True):
                output = await get_output(current, output_id, lock=True)
            if (
                output is None
                or output.state != "ready"
                or output.uncertain_write
                or _receipt(output) != expected
            ):
                raise ContentError("thumbnail_output_changed")
            target = await _recheck_read(current, saved, route, revision)
            await current.commit()

        if route is not None:
            return await migrated_reads.run(
                current,
                route,
                purpose(output_id),
                lambda client: read_from_client(client, output),
                on_acquired=acquired,
            )
        async with await storage_access.acquire(
            current, saved.location_id, purpose(output_id), expected_revision=revision
        ) as permit:
            await acquired(permit)
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
            output = await get_output(current, output_id)
            if (
                output is None
                or output.state != "ready"
                or output.uncertain_write
                or (output.owner_kind, output.owner_id) != (kind, identifier)
                or owner(output).key(RELATIVE_KEY) != path
            ):
                return None
            saved = owner(output)
            expected = _receipt(output)
            route, revision = await qualify_read(current, output)
            source = await _source(current, kind, identifier)
            if not _same_source(output, source):
                return None
            await original_sources.recheck_read_route(current, source)
            await current.commit()
            await read_bytes(current, output.id)
            if await _business_pointer(current, owner_kind, owner_id, path) != pointer:
                return None
            source = await _source(current, kind, identifier)
            if not _same_source(output, source):
                return None
            await original_sources.recheck_read_route(current, source)
            output = await get_output(current, output_id)
            if (
                output is None
                or output.state != "ready"
                or output.uncertain_write
                or _receipt(output) != expected
            ):
                return None
            await _recheck_read(current, saved, route, revision)
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
