"""Saved thumbnail responsibility shared by directory and ordinary image previews."""

from dataclasses import asdict
import hashlib
import json
import uuid

from botocore.exceptions import BotoCoreError, ClientError
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
    with target.client(read_timeout=30) as client:
        response = client.get_object(**params)
        with storage_access.closing_registered(response["Body"]) as body:
            data = body.read(MAX_BYTES + 1)
            extra = body.read(1)
        if (
            response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 200
            or response.get("ContentRange") is not None
            or response.get("DeleteMarker")
            or response.get("ContentLength") != output.size_bytes
            or len(data) != output.size_bytes
            or len(data) > MAX_BYTES
            or extra
            or hashlib.sha256(data).hexdigest() != output.sha256
        ):
            raise ContentError("thumbnail_content_changed")


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
            target = await media_cache_storage.target_for_ref(current, saved.ref)
            revision = await storage_access.check(current, saved.location_id)
            await current.commit()
            await storage_access.run(
                current,
                saved.location_id,
                "thumbnail_sign",
                _verified_bytes,
                target,
                output,
                expected_revision=revision,
            )
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
