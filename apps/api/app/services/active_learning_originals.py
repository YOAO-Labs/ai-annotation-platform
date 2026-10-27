"""Frozen AL content ownership and physically drained original operations."""

from contextlib import asynccontextmanager
import asyncio
from dataclasses import dataclass, field
import hashlib
import io
import threading
import uuid

from fastapi import HTTPException
from PIL import Image

from app.db.models.user import User
from app.services import managed_original_reader as reader
from app.services import original_content_access as access
from app.services import original_sources, storage_access
from app.services import sample_evaluation_engine as detection
from app.services.original_binary_response import close_stage
from app.services.sample_evaluation_originals import bounded_thread
from app.services.training_dataset_bridge import content_digest
from app.services.training_datasets import assert_training_authority
from app.services.content_snapshot import ContentError

SCOPES = frozenset({"datasets:read", "datasets:write", "annotations:read"})
_slots = threading.BoundedSemaphore(2)
_FIELDS = {"schema", "owner_kind", "content_id", "sha256", "size_bytes", "location_id"}


@dataclass(frozen=True)
class Authority:
    project_id: uuid.UUID
    actor_id: uuid.UUID
    key_id: uuid.UUID | None
    key: tuple | None = field(repr=False)
    proof: object = field(default=None, repr=False)


def key_fingerprint(key):
    return content_digest(
        [key[0], list(key[1]), key[2].isoformat() if key[2] else None]
        if key is not None
        else None
    )


async def current_actor_key(db, actor_id, key_id, proof=None):
    if proof is not None:
        return await access.actor_and_key(db, actor_id, key_id, "datasets:read", proof)
    # API Key last_used_at writes can briefly conflict with the worker's SHARE
    # NOWAIT lock. A savepoint recovers the failed statement without committing
    # or rolling back the caller's transaction; a real revocation is never retried.
    for attempt in range(4):
        try:
            connection = await db.connection()
            async with connection.begin_nested():
                return await access.actor_and_key(db, actor_id, key_id, "datasets:read")
        except HTTPException as exc:
            if (
                attempt == 3
                or exc.status_code != 409
                or not isinstance(exc.detail, dict)
                or exc.detail.get("code") != "content_busy"
            ):
                raise
            await asyncio.sleep(0.05 * (attempt + 1))


async def check(db, authority, *, commit=True, guard=None):
    _actor, key = await current_actor_key(
        db, authority.actor_id, authority.key_id, authority.proof
    )
    if key != authority.key:
        raise HTTPException(403, {"code": "api_key_authority_revoked"})
    await assert_training_authority(
        db,
        project_id=authority.project_id,
        actor_id=authority.actor_id,
        api_key_id=str(authority.key_id) if authority.key_id else None,
        required_scopes=SCOPES,
    )
    if guard is not None:
        await guard()
    if commit:
        await db.commit()
    access.check_clock(authority.proof, key)


async def authorize(db, project_id, actor_id, request, api_key_id=None):
    actor = await db.get(User, actor_id)
    if actor is None:
        raise HTTPException(403, {"code": "content_authority_revoked"})
    proof = await access.capture_credentials(db, actor, request)
    if proof is None or (str(proof.key_id) if proof.key_id else None) != (
        str(api_key_id) if api_key_id else None
    ):
        raise HTTPException(403, {"code": "content_authority_revoked"})
    context = Authority(project_id, actor_id, proof.key_id, proof.key_proof, proof)
    await check(db, context)
    return context


async def delegated(db, round, *, commit=True):
    key_id = round.authorization.get("api_key_id")
    key_id = uuid.UUID(str(key_id)) if key_id else None
    _actor, key = await current_actor_key(db, round.creator_id, key_id)
    if round.spec.get("original_contract") == 1 and (
        round.authorization.get("original_contract") != 1
        or round.authorization.get("api_key_fingerprint") != key_fingerprint(key)
    ):
        raise HTTPException(403, {"code": "api_key_authority_revoked"})
    context = Authority(round.project_id, round.creator_id, key_id, key)
    await check(db, context, commit=commit)
    return context


def receipt(source):
    if (
        source.media_type != "image"
        or source.bucket is None
        or source.location_id is None
        or not 0 < source.size_bytes <= detection.MAX_RAW_BYTES
    ):
        raise ContentError("original_input_unsupported")
    return {
        "schema": 1,
        "owner_kind": source.owner_kind,
        "content_id": str(source.content_id),
        "sha256": source.sha256,
        "size_bytes": source.size_bytes,
        "location_id": str(source.location_id),
    }


def validate_receipt(value):
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise ContentError("original_input_unrecorded")
    try:
        valid = (
            type(value["schema"]) is int
            and value["schema"] == 1
            and value["owner_kind"] in {"dataset_asset", "task_original"}
            and str(uuid.UUID(value["content_id"])) == value["content_id"]
            and str(uuid.UUID(value["location_id"])) == value["location_id"]
            and type(value["size_bytes"]) is int
            and 0 < value["size_bytes"] <= detection.MAX_RAW_BYTES
            and isinstance(value["sha256"], str)
            and len(value["sha256"]) == 64
            and all(c in "0123456789abcdef" for c in value["sha256"])
        )
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise ContentError("original_input_unrecorded")
    return value


def aligned(source, snapshot):
    relation = source.relation
    batch = snapshot.get("batch")
    return (
        relation is not None
        and str(relation.task_id) == snapshot["task_id"]
        and str(relation.project_id) == snapshot["project_id"]
        and (str(relation.batch_id) if relation.batch_id else None)
        == (batch["id"] if batch else None)
        and relation.file_path == snapshot["source_key"]
        and relation.file_type == snapshot["file_type"] == "image"
        and (str(relation.direct_item_id) if relation.direct_item_id else None)
        == snapshot["dataset_item_id"]
    )


async def pin(db, snapshot):
    source = await original_sources.resolve_task(
        db, uuid.UUID(snapshot["task_id"]), lock=True
    )
    if not aligned(source, snapshot):
        raise ContentError("content_binding_changed")
    return {**snapshot, "original": receipt(source)}


async def freeze(db, authority, snapshot, *, guard=None):
    expected = validate_receipt(snapshot.get("original"))
    await check(db, authority, commit=False, guard=guard)
    source = await original_sources.resolve_task(
        db, uuid.UUID(snapshot["task_id"]), lock=True
    )
    if not aligned(source, snapshot) or receipt(source) != expected:
        raise ContentError("content_binding_changed")
    await db.commit()
    access.check_clock(authority.proof, authority.key)
    return source


async def recheck(db, authority, source, *, commit=True, guard=None):
    await check(db, authority, commit=False, guard=guard)
    current = await original_sources.resolve_task(
        db, source.relation.task_id, lock=True
    )
    if current != source:
        raise ContentError("content_binding_changed")
    await original_sources.recheck_read_route(db, source)
    from app.services.managed_original_reclaim_fence import protect_source

    await protect_source(db, current)
    if commit:
        await db.commit()
    access.check_clock(authority.proof, authority.key)


def normalize(stage, snapshot):
    stage.file.seek(0)
    raw = stage.file.read(detection.MAX_RAW_BYTES + 1)
    if len(raw) != stage.size or hashlib.sha256(raw).hexdigest() != stage.sha256:
        raise ContentError("content_changed")
    with storage_access.closing_registered(Image.open(io.BytesIO(raw))) as image:
        if (
            image.width * image.height > detection.MAX_PIXELS
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
        snapshot.get("expected_width")
        and snapshot.get("expected_height")
        and (width, height) != (snapshot["expected_width"], snapshot["expected_height"])
    ):
        raise ValueError("Current source logical dimensions differ")
    png, preprocessing = detection.normalize_image(raw, width=width, height=height)
    return png, {
        "sha256": stage.sha256,
        "width": width,
        "height": height,
        "size_bytes": stage.size,
        "derived_sha256": preprocessing["derived_sha256"],
        "preprocessing": preprocessing,
    }


@asynccontextmanager
async def input_image(db, authority, snapshot, *, guard=None):
    source = await freeze(db, authority, snapshot, guard=guard)
    plan = original_sources.read_plan(source)
    permit = await storage_access.acquire(
        db,
        plan.location_id,
        plan.purpose("active_learning_original"),
        expected_revision=plan.maintenance_revision,
    )
    async with permit:
        stage, failure = None, None
        try:
            await recheck(db, authority, source, guard=guard)
            stage = await reader.capture(
                plan.spec,
                max_bytes=detection.MAX_RAW_BYTES,
                timeout_seconds=120,
                admission=_slots,
                transport_timeout_seconds=30,
                **(await original_sources.read_options(db, source)),
            )
            png, media = await bounded_thread(normalize, stage, snapshot)
            await recheck(db, authority, source, guard=guard)
            yield png, media, source
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
        await recheck(db, authority, source, guard=guard)


async def read_input(db, authority, snapshot, *, guard=None):
    async with input_image(db, authority, snapshot, guard=guard) as result:
        pass
    return result
