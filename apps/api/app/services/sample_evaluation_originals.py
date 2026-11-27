"""Current evaluation inputs use saved original facts and request authority."""

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import threading
import uuid

from fastapi import HTTPException
from PIL import Image

from app.services import managed_original_reader as reader
from app.services import original_content_access as access
from app.services import original_sources, storage_access
from app.services.content_snapshot import ContentError
from app.services.bounded_thread import bounded_thread
from app.services.original_binary_response import close_stage
from app.services.training_authority import bind_training_authority

COPY_SECONDS = 120
_slots = threading.BoundedSemaphore(2)


@dataclass(frozen=True)
class Authority:
    project_id: uuid.UUID
    proof: object = field(repr=False)
    key: tuple | None = field(repr=False)


async def authorize(db, project_id, actor, request, api_key_id=None):
    proof = await access.capture_credentials(db, actor, request)
    try:
        expected_key = uuid.UUID(str(api_key_id)) if api_key_id is not None else None
    except (TypeError, ValueError, AttributeError):
        expected_key = object()
    if proof is None or proof.key_id != expected_key:
        raise HTTPException(403, {"code": "content_authority_revoked"})
    authority = Authority(project_id, proof, proof.key_proof)
    await recheck_authority(db, authority)
    return authority


async def recheck_authority(db, authority):
    proof = authority.proof
    _actor, key = await access.actor_and_key(
        db, proof.actor_id, proof.key_id, "datasets:read", proof
    )
    if key != authority.key:
        raise HTTPException(403, {"code": "api_key_authority_revoked"})
    await bind_training_authority(db).assert_authority(
        project_id=authority.project_id,
        actor_id=proof.actor_id,
        api_key_id=str(proof.key_id) if proof.key_id else None,
    )
    await db.commit()
    access.check_clock(proof, key)


def _aligned(source, authority, snapshot):
    relation = source.relation
    return (
        relation is not None
        and str(relation.task_id) == snapshot["task_id"]
        and relation.project_id == authority.project_id
        and str(relation.project_id) == snapshot["project_id"]
        and (str(relation.batch_id) if relation.batch_id else None)
        == snapshot["batch_id"]
        and relation.file_path == snapshot["source_key"]
        and relation.file_type == snapshot["file_type"] == "image"
        and source.media_type == "image"
        and source.bucket is not None
        and 0 < source.size_bytes
    )


async def freeze_source(db, authority, snapshot):
    await recheck_authority(db, authority)
    source = await original_sources.resolve_task(
        db, uuid.UUID(snapshot["task_id"]), lock=True
    )
    if not _aligned(source, authority, snapshot):
        raise ContentError("content_binding_changed")
    await db.commit()
    access.check_clock(authority.proof, authority.key)
    return source


async def recheck_source(db, authority, source):
    await recheck_authority(db, authority)
    current = await original_sources.resolve_task(
        db, source.relation.task_id, lock=True
    )
    if current != source:
        raise ContentError("content_binding_changed")
    await original_sources.recheck_read_route(db, source)
    await db.commit()
    access.check_clock(authority.proof, authority.key)


def inspect_image(stage, snapshot):
    from app.services import sample_evaluation_engine as engine

    stage.file.seek(0)
    with storage_access.closing_registered(Image.open(stage.file)) as image:
        if (
            image.width * image.height > engine.MAX_PIXELS
            or getattr(image, "n_frames", 1) != 1
        ):
            raise ValueError("Current source exceeds pixel/frame budget")
        image.load()
        orientation = image.getexif().get(274, 1)
        if type(orientation) is not int or not 1 <= orientation <= 8:
            raise ValueError("Current source orientation is invalid")
        width, height = (
            (image.height, image.width) if orientation in {5, 6, 7, 8} else image.size
        )
    if (
        snapshot["expected_width"]
        and snapshot["expected_height"]
        and (width, height) != (snapshot["expected_width"], snapshot["expected_height"])
    ):
        raise ValueError("Current source logical dimensions differ from its item")
    return {"sha256": stage.sha256, "width": width, "height": height}


@asynccontextmanager
async def current_media(db, authority, snapshot):
    """Own the raw stage and admission through classification and final checks."""
    from app.services import sample_evaluation_engine as engine

    try:
        if snapshot["file_type"] != "image":
            raise ContentError("content_media_unsupported")
        source = await freeze_source(db, authority, snapshot)
        if source.size_bytes > engine.MAX_RAW_BYTES:
            raise ContentError("content_too_large", 413)
        plan = original_sources.read_plan(source)
        permit = await storage_access.acquire(
            db,
            plan.location_id,
            plan.purpose("evaluation_original_read"),
            expected_revision=plan.maintenance_revision,
        )
    except ContentError as exc:
        if getattr(exc, "storage_operation_unsettled", False):
            raise
        await db.commit()
        yield None, None
        await recheck_authority(db, authority)
        return

    async with permit:
        stage = None
        media = None
        failure = None
        try:
            await recheck_source(db, authority, source)
            try:
                stage = await reader.capture(
                    plan.spec,
                    max_bytes=engine.MAX_RAW_BYTES,
                    timeout_seconds=COPY_SECONDS,
                    admission=_slots,
                    transport_timeout_seconds=30,
                    **(await original_sources.read_options(db, source, permit=permit)),
                )
                media = await bounded_thread(inspect_image, stage, snapshot)
            except Exception as exc:
                if getattr(exc, "storage_operation_unsettled", False):
                    raise
            yield source, media
        except BaseException as exc:
            failure = exc
            raise
        finally:
            if stage is not None:
                try:
                    await close_stage(stage)
                except BaseException as closing:
                    if getattr(failure, "storage_operation_unsettled", False):
                        storage_access.mark_unsettled(closing)
                    raise
        await recheck_source(db, authority, source)
