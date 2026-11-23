"""Ordinary image previews use canonical originals and independent output owners."""

import sys
from pathlib import Path

from app.config import settings
from app.services import image_pyramid_originals as originals
from app.services import original_sources, storage_access, thumbnail_outputs
from app.services.content_snapshot import ContentError
from app.services.dataset_lifecycle import require_writable
from app.services.directory_policy import owns_media
from app.services.image_pyramid import pyramid_eligible


async def _business(db, kind, identifier, *, lock=False):
    async with original_sources._reads(db, lock):
        return await thumbnail_outputs.business_facts(db, kind, identifier, lock=lock)


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
    if kind != "dataset_item" or source is None:
        return
    await db.rollback()
    try:
        await _current(db, kind, identifier, source, revision)
        await thumbnail_outputs.record_item_error(
            db, identifier, getattr(exc, "code", "thumbnail_generation_failed")
        )
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
    source, revision, source_guard = None, None, None
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
        from app.services import fixture_media_execution

        execution = fixture_media_execution.current()
        if execution is not None:
            execution.require_source(originals.receipt(source))
        dimensions = (row.width, row.height) if kind == "dataset_item" else None
        missing = kind == "dataset_item" and None in dimensions
        await db.rollback()

        if missing or kind == "task" and allow_pyramid:
            dimensions = await dimensions_probe(db, kind, identifier)
            await db.rollback()
            if missing:
                await _current(db, kind, identifier, source, revision)
                await thumbnail_outputs.set_item_dimensions(db, identifier, *dimensions)
                await db.commit()
                source = await originals.pin(db, kind, identifier)
                await db.rollback()
        if dimensions and pyramid_eligible(*dimensions):
            if allow_pyramid and settings.image_pyramid_auto_generate:
                from app.services import fixture_media_execution

                delegation = originals.system_authorization(kind, identifier, source)
                execution = fixture_media_execution.current()
                if execution is not None and execution.kind == "backfill":
                    if kind != "dataset_item":
                        raise ContentError("fixture_media_scope_mismatch")
                    await fixture_media_execution.enqueue_pyramid(
                        db, identifier, delegation
                    )
                else:
                    from app.workers.image_pyramid import enqueue_image_pyramid

                    await enqueue_image_pyramid(
                        db,
                        kind,
                        identifier,
                        **({"force": True} if kind == "task" else {}),
                        delegation=delegation,
                    )
            return
        if source.size_bytes > max_source_bytes:
            raise ContentError("thumbnail_source_too_large", 413)
        authority = await originals.delegated(
            db, originals.system_authorization(kind, identifier, source)
        )
        await db.rollback()
        pending_guard = originals.media(db, authority, source)
        stage = await pending_guard.__aenter__()
        source_guard = pending_guard
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
        await _current(db, kind, identifier, source, revision)
        await thumbnail_outputs.publish_ordinary(db, kind, identifier, output, blurhash)
        await db.commit()
    except Exception as exc:
        from app.services import fixture_media_execution

        if (
            storage_access.failure_unsettled(exc)
            or fixture_media_execution.current() is not None
        ):
            raise
        await _error(db, kind, identifier, source, revision, exc)
    finally:
        failure = sys.exception()
        errors = []
        try:
            await db.rollback()
        except BaseException as rollback_error:
            errors.append(rollback_error)
        if source_guard is not None:
            try:
                await source_guard.__aexit__(
                    type(failure) if failure is not None else None,
                    failure,
                    failure.__traceback__ if failure is not None else None,
                )
            except BaseException as close_error:
                errors.append(storage_access.mark_unsettled(close_error))
        if errors:
            if failure is not None:
                errors.insert(0, failure)
            combined = BaseExceptionGroup(
                "Thumbnail generation and cleanup failed", errors
            )
            storage_access.mark_unsettled(combined)
            raise combined from None
