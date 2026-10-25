"""Ordinary image previews use canonical originals and independent output owners."""

from pathlib import Path

from sqlalchemy import select

from app.config import settings
from app.db.models.dataset import DatasetItem
from app.db.models.task import Task
from app.db.models.thumbnail_output import ThumbnailOutput
from app.services import image_pyramid_originals as originals
from app.services import original_sources, thumbnail_outputs
from app.services.content_snapshot import ContentError
from app.services.dataset_lifecycle import require_writable
from app.services.directory_policy import owns_media
from app.services.image_pyramid import pyramid_eligible


async def _business(db, kind, identifier, *, lock=False):
    model = DatasetItem if kind == "dataset_item" else Task
    statement = select(model).where(model.id == identifier)
    if lock:
        statement = statement.with_for_update(nowait=True)
    async with original_sources._reads(db, lock):
        return await db.scalar(statement.execution_options(populate_existing=True))


async def _current(db, kind, identifier, expected, lifecycle_revision):
    current = await originals.pin(db, kind, identifier, lock=True)
    if current != expected:
        raise ContentError("content_binding_changed")
    row = await _business(db, kind, identifier, lock=True)
    if row is None or row.file_type != "image":
        raise ContentError("content_binding_changed")
    if await owns_media(db, row.id if kind == "dataset_item" else row.dataset_item_id):
        raise ContentError("directory_cache_not_supported")
    if kind == "dataset_item":
        await require_writable(
            db, row.dataset_id, expected_revision=lifecycle_revision, lock=True
        )
    return row


def _encode(stage, encode, *, inspect_dimensions):
    path = Path(f"/proc/self/fd/{stage.file.fileno()}")
    if inspect_dimensions:
        from PIL import Image
        from app.services.storage import StorageService

        with Image.open(path) as image:
            dimensions = StorageService._logical_image_dimensions(image)
        if pyramid_eligible(*dimensions):
            return None
    return encode(path)


async def _error(db, kind, identifier, source, revision, exc):
    # Keep Dataset's existing stable error metadata, only for the same original.
    if kind != "dataset_item" or source is None:
        return
    await db.rollback()
    try:
        row = await _current(db, kind, identifier, source, revision)
        metadata = dict(row.metadata_ or {})
        metadata["thumbnail_error"] = getattr(
            exc, "code", "thumbnail_generation_failed"
        )
        row.metadata_ = metadata
        await db.commit()
    except Exception:
        await db.rollback()


async def generate(
    db,
    kind,
    identifier,
    *,
    encode,
    dimensions_probe,
    max_source_bytes,
    allow_pyramid=True,
):
    """No source/output I/O runs while final business publication locks are held."""
    source, revision = None, None
    try:
        row = await _business(db, kind, identifier)
        if row is None or row.file_type != "image":
            return
        if await owns_media(
            db, row.id if kind == "dataset_item" else row.dataset_item_id
        ):
            return
        if kind == "dataset_item":
            dataset = await require_writable(db, row.dataset_id)
            revision = dataset.lifecycle_revision
        source = await originals.pin(db, kind, identifier)
        dimensions = (row.width, row.height) if kind == "dataset_item" else None
        missing = kind == "dataset_item" and None in dimensions
        await db.rollback()

        if missing or kind == "task" and allow_pyramid:
            dimensions = await dimensions_probe(db, kind, identifier)
            await db.rollback()
            if missing:
                # Preserve the existing first-size commit, then pin its real facts.
                current = await _current(db, kind, identifier, source, revision)
                current.width, current.height = dimensions
                await db.commit()
                source = await originals.pin(db, kind, identifier)
                await db.rollback()
        if dimensions and pyramid_eligible(*dimensions):
            if allow_pyramid and settings.image_pyramid_auto_generate:
                from app.workers.image_pyramid import enqueue_image_pyramid

                enqueue_image_pyramid(
                    kind,
                    identifier,
                    **({"force": True} if kind == "task" else {}),
                    delegation=originals.system_authorization(kind, identifier, source),
                )
            return
        if source.size_bytes > max_source_bytes:
            raise ContentError("thumbnail_source_too_large", 413)
        authority = await originals.delegated(
            db, originals.system_authorization(kind, identifier, source)
        )
        await db.rollback()
        async with originals.media(db, authority, source) as stage:
            encoded = await originals.physical(
                _encode,
                stage,
                encode,
                inspect_dimensions=kind == "task" and not allow_pyramid,
            )
        await db.rollback()
        if encoded is None:
            return
        body, blurhash = encoded
        owner_kind = "dataset_thumbnail" if kind == "dataset_item" else "task_thumbnail"
        output = await thumbnail_outputs.create(
            db,
            owner_kind=owner_kind,
            owner_id=identifier,
            input_ref={"source": thumbnail_outputs.source_facts(source)},
            body=body,
        )
        # create() has closed its physical handles and persisted ready independently.
        # Re-pin under NOWAIT locks only now; an unattached candidate stays durable
        # if this final source/business check loses a race.
        row = await _current(db, kind, identifier, source, revision)
        async with original_sources._reads(db, True):
            ready = await db.scalar(
                select(ThumbnailOutput)
                .where(ThumbnailOutput.id == output.id)
                .with_for_update(read=True, nowait=True)
                .execution_options(populate_existing=True)
            )
        if (
            ready is None
            or ready.state != "ready"
            or ready.uncertain_write
            or ready.owner_kind != owner_kind
            or ready.owner_id != identifier
            or ready.input_ref != output.input_ref
            or ready.storage_ref != output.storage_ref
        ):
            raise ContentError("thumbnail_output_changed")
        previous_id = row.thumbnail_output_id
        if previous_id is not None and previous_id != ready.id:
            async with original_sources._reads(db, True):
                previous = await db.scalar(
                    select(ThumbnailOutput)
                    .where(ThumbnailOutput.id == previous_id)
                    .with_for_update(nowait=True)
                )
            if (
                previous is not None
                and previous.owner_kind == owner_kind
                and previous.owner_id == identifier
                and previous.state not in {"retiring", "deleted"}
            ):
                previous.state = "retiring"
                previous.retired_at = previous.next_check_at = thumbnail_outputs.now()
        row.thumbnail_output_id = ready.id
        row.thumbnail_path = thumbnail_outputs.owner(ready).key(
            thumbnail_outputs.RELATIVE_KEY
        )
        row.blurhash = blurhash
        await db.commit()
    except Exception as exc:
        await _error(db, kind, identifier, source, revision, exc)
    finally:
        await db.rollback()
