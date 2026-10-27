"""Training-specific original identity, authority and physically drained I/O."""

import asyncio
from contextlib import asynccontextmanager, contextmanager
from contextvars import copy_context
from dataclasses import dataclass, field
from functools import partial
import threading
import tempfile
import uuid

from fastapi import HTTPException
from PIL import Image

from app.db.models.user import User
from app.services import managed_original_reader as reader
from app.services import original_content_access as access
from app.services import original_sources, storage_access
from app.services.content_snapshot import ContentError
from app.services.original_binary_response import close_stage
from app.services.training_dataset_bridge import TrainingDatasetError, content_digest

MAX_BYTES = 256 * 1024 * 1024
MAX_PIXELS = 100_000_000
SCOPES = frozenset({"datasets:read", "annotations:read"})
_slots = threading.BoundedSemaphore(2)
_FIELDS = {"schema", "owner_kind", "content_id", "sha256", "size_bytes", "location_id"}


class BorrowedFile:
    """Pillow owns its decoder, while the outer stage owns this stream."""

    def __init__(self, source):
        self.source = source

    def __getattr__(self, name):
        return getattr(self.source, name)

    def close(self):
        pass


class Admission(threading.Semaphore):
    """Retain capacity if any decoder/copy resource has uncertain closure."""

    def __init__(self):
        super().__init__(0)
        self.retained = False

    def acquire(self, *args, **kwargs):
        return _slots.acquire(*args, **kwargs)

    def release(self):
        if not self.retained:
            _slots.release()


def output_purpose(version_id):
    return "training_dataset_" + version_id.hex


@contextmanager
def temporary_directory(**kwargs):
    temporary = tempfile.TemporaryDirectory(**kwargs)
    try:
        yield temporary.name
    finally:
        try:
            temporary.cleanup()
        except BaseException as exc:
            raise storage_access.mark_unsettled(exc)


@dataclass(frozen=True)
class Authority:
    project_id: uuid.UUID
    actor_id: uuid.UUID
    key_id: uuid.UUID | None
    key: tuple | None = field(repr=False)
    proof: object = field(default=None, repr=False)


def fingerprint(key):
    return content_digest(
        [key[0], list(key[1]), key[2].isoformat() if key[2] else None]
        if key is not None
        else None
    )


def authorization(authority):
    return {
        "actor_id": str(authority.actor_id),
        "api_key_id": str(authority.key_id) if authority.key_id else None,
        "original_contract": 1,
        "api_key_fingerprint": fingerprint(authority.key),
    }


async def current_key(db, actor_id, key_id, proof=None):
    for attempt in range(4):
        try:
            connection = await db.connection()
            async with connection.begin_nested():
                return await access.actor_and_key(
                    db, actor_id, key_id, "datasets:read", proof
                )
        except HTTPException as exc:
            if (
                proof is not None
                or attempt == 3
                or exc.status_code != 409
                or not isinstance(exc.detail, dict)
                or exc.detail.get("code") != "content_busy"
            ):
                raise
            await asyncio.sleep(0.05 * (attempt + 1))


async def check(db, authority):
    from app.services.training_datasets import bind_training_authority

    _actor, key = await current_key(
        db, authority.actor_id, authority.key_id, authority.proof
    )
    if key != authority.key:
        raise HTTPException(403, {"code": "api_key_authority_revoked"})
    await bind_training_authority(db).assert_authority(
        project_id=authority.project_id,
        actor_id=authority.actor_id,
        api_key_id=str(authority.key_id) if authority.key_id else None,
        required_scopes=SCOPES,
    )
    clock(authority)


def clock(authority):
    access.check_clock(authority.proof, authority.key)


async def authorize(
    db, project_id, actor_id, *, http_request=None, api_key_id=None, trusted_actor=False
):
    key_id = uuid.UUID(str(api_key_id)) if api_key_id else None
    if http_request is not None:
        actor = await db.get(User, actor_id)
        if actor is None:
            raise HTTPException(403, {"code": "content_authority_revoked"})
        proof = await access.capture_credentials(db, actor, http_request)
        if proof is None or proof.key_id != key_id:
            raise HTTPException(403, {"code": "content_authority_revoked"})
        authority = Authority(project_id, actor_id, key_id, proof.key_proof, proof)
    elif trusted_actor:
        _actor, key = await current_key(db, actor_id, key_id)
        authority = Authority(project_id, actor_id, key_id, key)
    else:
        raise HTTPException(403, {"code": "content_authority_unrecorded"})
    await check(db, authority)
    return authority


async def delegated(db, version):
    value = version.snapshot.get("authorization") or {}
    key_id = uuid.UUID(str(value["api_key_id"])) if value.get("api_key_id") else None
    _actor, key = await current_key(db, version.creator_id, key_id)
    authority = Authority(version.project_id, version.creator_id, key_id, key)
    if "managed_original_contract" in version.snapshot and (
        type(version.snapshot["managed_original_contract"]) is not int
        or version.snapshot["managed_original_contract"] != 1
        or type(value.get("original_contract")) is not int
        or value.get("original_contract") != 1
        or value.get("actor_id") != str(version.creator_id)
        or value.get("api_key_fingerprint") != fingerprint(key)
    ):
        raise HTTPException(403, {"code": "api_key_authority_revoked"})
    await check(db, authority)
    return authority


def receipt(source):
    if (
        source.media_type != "image"
        or source.bucket is None
        or source.location_id is None
        or not 0 < source.size_bytes <= MAX_BYTES
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
    try:
        valid = (
            isinstance(value, dict)
            and set(value) == _FIELDS
            and type(value["schema"]) is int
            and value["schema"] == 1
            and value["owner_kind"] in {"dataset_asset", "task_original"}
            and str(uuid.UUID(value["content_id"])) == value["content_id"]
            and str(uuid.UUID(value["location_id"])) == value["location_id"]
            and type(value["size_bytes"]) is int
            and 0 < value["size_bytes"] <= MAX_BYTES
            and isinstance(value["sha256"], str)
            and len(value["sha256"]) == 64
            and all(c in "0123456789abcdef" for c in value["sha256"])
        )
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise ContentError("original_input_unrecorded")
    return value


async def pin(db, project_id, sample, *, lock=False):
    source = await original_sources.resolve_task(
        db, uuid.UUID(sample["task_id"]), lock=lock
    )
    if (
        source.relation.project_id != project_id
        or source.relation.file_type != "image"
        or source.relation.file_path != sample["source_key"]
    ):
        raise ContentError("content_binding_changed")
    expected = sample.get("original")
    if expected is not None and receipt(source) != validate_receipt(expected):
        raise ContentError("content_binding_changed")
    return source


async def recheck(db, authority, source, *, commit=False, guard=None):
    await check(db, authority)
    if guard is not None:
        await guard()
    current = await original_sources.resolve_task(
        db, source.relation.task_id, lock=True
    )
    if current != source:
        raise ContentError("content_binding_changed")
    await original_sources.recheck_read_route(db, source)
    if commit:
        await db.commit()
    clock(authority)


async def physical(call, *args, timeout=None, **kwargs):
    """Drain native work before its files, directory or durable permit can end."""
    try:
        pending = asyncio.get_running_loop().run_in_executor(
            None, copy_context().run, partial(call, *args, **kwargs)
        )
    except BaseException as exc:
        raise storage_access.mark_unsettled(exc)
    try:
        return (
            await asyncio.shield(pending)
            if timeout is None
            else await asyncio.wait_for(asyncio.shield(pending), timeout)
        )
    except (asyncio.CancelledError, TimeoutError) as interrupted:
        try:
            await storage_access.finish(pending)
        except BaseException as late:
            if getattr(late, "storage_operation_unsettled", False):
                storage_access.mark_unsettled(interrupted)
        raise


def inspect_image(stage, sample):
    stage.file.seek(0)
    with storage_access.closing_registered(
        Image.open(BorrowedFile(stage.file))
    ) as image:
        if (
            image.format not in {"JPEG", "PNG", "WEBP", "BMP", "TIFF"}
            or image.width * image.height > MAX_PIXELS
        ):
            raise TrainingDatasetError(
                "unsupported_image", "图片格式或像素数量不适合当前训练器"
            )
        image.load()
        orientation = image.getexif().get(274, 1)
        if type(orientation) is not int or not 1 <= orientation <= 8:
            raise TrainingDatasetError("invalid_image", "图片方向信息无效")
        width, height = (
            (image.height, image.width) if orientation in {5, 6, 7, 8} else image.size
        )
    if (sample.get("expected_width") and width != sample["expected_width"]) or (
        sample.get("expected_height") and height != sample["expected_height"]
    ):
        raise TrainingDatasetError(
            "image_dimensions_changed", "图片尺寸与冻结的标注坐标范围不一致"
        )
    return {
        "sha256": stage.sha256,
        "size_bytes": stage.size,
        "width": width,
        "height": height,
    }


@asynccontextmanager
async def media(db, authority, sample, *, guard=None):
    """Only an owned preparation/worker session may commit these read phases."""
    validate_receipt(sample.get("original"))
    await check(db, authority)
    if guard is not None:
        await guard()
    source = await pin(db, authority.project_id, sample, lock=True)
    await db.commit()
    clock(authority)
    plan = original_sources.read_plan(source)
    permit = await storage_access.acquire(
        db,
        plan.location_id,
        plan.purpose("training_dataset_original"),
        expected_revision=plan.maintenance_revision,
    )
    async with permit:
        stage, failure = None, None
        admission = Admission()
        try:
            await recheck(db, authority, source, commit=True, guard=guard)
            stage = await reader.capture(
                plan.spec,
                max_bytes=MAX_BYTES,
                timeout_seconds=120,
                transport_timeout_seconds=30,
                **(await original_sources.read_options(db, source)),
                admission=admission,
            )
            yield stage, source
        except BaseException as exc:
            failure = exc
            raise
        finally:
            if getattr(failure, "storage_operation_unsettled", False):
                admission.retained = True
            if stage is not None:
                try:
                    await close_stage(stage)
                except BaseException as closing:
                    if getattr(failure, "storage_operation_unsettled", False):
                        storage_access.mark_unsettled(closing)
                    raise
            if failure is None or (
                isinstance(failure, (ValueError, OSError))
                and not getattr(failure, "storage_operation_unsettled", False)
            ):
                await recheck(db, authority, source, commit=True, guard=guard)


async def preflight(db, authority, sample):
    async with media(db, authority, sample) as (stage, source):
        result = await physical(inspect_image, stage, sample, timeout=30)
    return result, source
