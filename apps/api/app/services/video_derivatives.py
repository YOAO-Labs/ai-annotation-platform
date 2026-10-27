"""Video logical slots, immutable generations and short publication fences."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import uuid
import base64
import binascii

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.db.models.dataset import (
    DatasetItem,
    VideoChunk,
    VideoFrameCache,
    VideoFrameIndex,
)
from app.db.models.video_derivative import VideoDerivativeOutput as Output
from app.schemas.task import VideoMetadata
from app.services import video_derivative_originals as originals
from app.services import video_derivative_storage as physical
from app.services import storage_access
from app.services.content_snapshot import ContentError


# Allocation is the only writer a reader can contend with. Readers reuse a
# committed attempt without any write lock, so this bound applies exactly to
# the slot row a concurrent allocator (or publisher) owns for a short window.
ALLOCATION_LOCK_TIMEOUT = "1s"


def now():
    return datetime.now(timezone.utc)


def reject(code="video_derivative_changed", status=409):
    raise ContentError(code, status)


async def row(db, model, identifier, *, lock=False, write=False):
    stmt = select(model).where(model.id == identifier)
    if lock:
        stmt = stmt.with_for_update(read=not write, nowait=True)
    return await db.scalar(stmt.execution_options(populate_existing=True))


def metadata_result(output):
    result = output.result
    if not (
        output.kind == "metadata"
        and output.state == "ready"
        and isinstance(result, dict)
        and set(result) == {"schema", "kind", "video", "frames"}
        and type(result["schema"]) is int
        and result["schema"] == 1
        and result["kind"] == "metadata"
        and isinstance(result["video"], dict)
        and isinstance(result["frames"], list)
    ):
        reject("video_metadata_unpinned")
    try:
        metadata = VideoMetadata.model_validate(result["video"])
    except ValidationError:
        reject("video_metadata_invalid")
    if (
        not metadata.fps
        or not math.isfinite(metadata.fps)
        or metadata.fps <= 0
        or not metadata.frame_count
        or metadata.frame_count <= 0
        or not metadata.width
        or metadata.width <= 0
        or not metadata.height
        or metadata.height <= 0
    ):
        reject("video_metadata_invalid")
    frames = result["frames"]
    if len(frames) > 1_000_000:
        reject("video_metadata_budget", 413)
    previous = -1
    for frame in frames:
        if not (
            isinstance(frame, dict)
            and set(frame)
            == {"frame_index", "pts_ms", "is_keyframe", "pict_type", "byte_offset"}
            and type(frame["frame_index"]) is int
            and frame["frame_index"] > previous
            and frame["frame_index"] < metadata.frame_count
            and type(frame["pts_ms"]) is int
            and -(2**31) <= frame["pts_ms"] < 2**31
            and type(frame["is_keyframe"]) is bool
            and (frame["pict_type"] is None or isinstance(frame["pict_type"], str))
            and (
                frame["byte_offset"] is None
                or (type(frame["byte_offset"]) is int and frame["byte_offset"] >= 0)
            )
        ):
            reject("video_metadata_invalid")
        previous = frame["frame_index"]
    # Some containers lack a complete timetable. Keep the previous estimated-PTS
    # fallback, while every existing exact entry belongs to this generation.
    return metadata, deepcopy(frames)


def validate_samples(diagnostics, byte_size, spec):
    """A packet receipt can only address bytes inside its own verified chunk."""
    if not isinstance(diagnostics, dict):
        reject("video_derivative_result_invalid")
    if "samples" not in diagnostics:
        return
    samples = diagnostics["samples"]
    if not (
        isinstance(samples, list)
        and 0 < len(samples) <= 1_000_000
        and isinstance(diagnostics.get("codec_string"), str)
        and 0 < len(diagnostics["codec_string"]) <= 128
        and all(
            type(diagnostics.get(name)) is int and 0 < diagnostics[name] <= 65536
            for name in ("width", "height")
        )
    ):
        reject("video_derivative_result_invalid")
    description = diagnostics.get("description")
    if description is not None:
        try:
            if (
                not isinstance(description, str)
                or not 0 < len(description) <= 1024 * 1024
            ):
                raise ValueError
            base64.b64decode(description, validate=True)
        except (ValueError, binascii.Error):
            reject("video_derivative_result_invalid")
    seen = set()
    for sample in samples:
        if not (
            isinstance(sample, dict)
            and set(sample)
            == {
                "frame_index",
                "pts_ms",
                "duration_ms",
                "is_keyframe",
                "size_bytes",
                "offset_in_chunk",
            }
            and type(sample["frame_index"]) is int
            and sample["frame_index"] >= 0
            and sample["frame_index"] not in seen
            and type(sample["pts_ms"]) is int
            and -(2**31) <= sample["pts_ms"] < 2**31
            and type(sample["duration_ms"]) is int
            and sample["duration_ms"] >= 0
            and type(sample["is_keyframe"]) is bool
            and type(sample["size_bytes"]) is int
            and sample["size_bytes"] > 0
            and type(sample["offset_in_chunk"]) is int
            and sample["offset_in_chunk"] >= 0
            and sample["offset_in_chunk"] + sample["size_bytes"] <= byte_size
        ):
            reject("video_derivative_result_invalid")
        seen.add(sample["frame_index"])
    if seen != set(range(spec["start_frame"], spec["end_frame"] + 1)):
        reject("video_derivative_result_invalid")


async def metadata(db, item, source, *, lock=False):
    if item is None or item.video_output_id is None:
        reject("video_metadata_unpinned")
    output = await row(db, Output, item.video_output_id, lock=lock)
    if (
        output is None
        or output.dataset_item_id != item.id
        or output.original_ref != originals.receipt(source)
    ):
        reject("video_metadata_changed")
    metadata_result(output)
    physical.validate_inventory(output)
    return output


def _pts(frames, frame_index, info):
    if (
        0 <= frame_index < len(frames)
        and frames[frame_index]["frame_index"] == frame_index
    ):
        return frames[frame_index]["pts_ms"]
    for frame in frames:
        if frame["frame_index"] == frame_index:
            return frame["pts_ms"]
    return int(round(frame_index / info.fps * 1000))


def frame_spec(output, frame_index, width, fmt):
    info, frames = metadata_result(output)
    if (
        type(frame_index) is not int
        or not 0 <= frame_index < info.frame_count
        or type(width) is not int
        or not 1 <= width <= 4096
        or not isinstance(fmt, str)
        or fmt not in {"webp", "jpeg"}
    ):
        reject("video_frame_invalid", 400)
    return {
        "schema": 1,
        "frame_index": frame_index,
        "width": width,
        "format": fmt,
        "pts_ms": _pts(frames, frame_index, info),
    }


def chunk_spec(output, chunk_id):
    info, frames = metadata_result(output)
    size = max(1, settings.video_chunk_size_frames)
    if type(chunk_id) is not int or chunk_id < 0 or chunk_id * size >= info.frame_count:
        reject("video_chunk_invalid", 400)
    start, end = chunk_id * size, min(info.frame_count - 1, (chunk_id + 1) * size - 1)
    return {
        "schema": 1,
        "chunk_id": chunk_id,
        "chunk_size_frames": size,
        "start_frame": start,
        "end_frame": end,
        "start_pts_ms": _pts(frames, start, info),
        "end_pts_ms": _pts(frames, end, info),
    }


async def _waiting_scalar(db, statement, timeout):
    """Acquire an owned row with the repository's bounded lock-timeout rule.

    `SET LOCAL` scopes the bound to this transaction; a failed wait aborts the
    transaction instead of leaving a shortened timeout for later statements.
    """
    previous = await db.scalar(text("SHOW lock_timeout"))
    await db.execute(
        text("SELECT set_config('lock_timeout', :timeout, true)"),
        {"timeout": timeout},
    )
    value = await db.scalar(statement)
    await db.execute(
        text("SELECT set_config('lock_timeout', :timeout, true)"),
        {"timeout": previous},
    )
    return value


async def _slot(db, item, kind, spec, *, lock=False, create=False, wait=False):
    if kind == "metadata":
        return item
    model = VideoFrameCache if kind == "frame" else VideoChunk
    predicates = [model.dataset_item_id == item.id]
    if kind == "frame":
        predicates += [
            model.frame_index == spec["frame_index"],
            model.width == spec["width"],
            model.format == spec["format"],
        ]
    elif kind == "chunk":
        predicates += [model.chunk_id == spec["chunk_id"]]
    else:
        reject()
    stmt = select(model).where(*predicates).execution_options(populate_existing=True)
    if lock:
        stmt = stmt.with_for_update(read=not create, nowait=not wait)
    slot = (
        await _waiting_scalar(db, stmt, ALLOCATION_LOCK_TIMEOUT)
        if wait
        else await db.scalar(stmt)
    )
    if slot is None and create:
        values = {"dataset_item_id": item.id, "status": "pending"}
        if kind == "frame":
            values.update((k, spec[k]) for k in ("frame_index", "width", "format"))
        else:
            values.update(
                (k, spec[k])
                for k in (
                    "chunk_id",
                    "start_frame",
                    "end_frame",
                    "start_pts_ms",
                    "end_pts_ms",
                )
            )
        slot = model(**values)
        try:
            # A concurrent allocator may commit the unique slot while this
            # transaction waits on the index. Keep the caller's transaction
            # usable and adopt the committed winner instead of failing a read.
            async with db.begin_nested():
                db.add(slot)
                await db.flush()
        except IntegrityError as exc:
            if (
                getattr(exc.orig, "sqlstate", None) != "23505"
                and getattr(exc.orig, "pgcode", None) != "23505"
            ):
                raise
            slot = (
                await _waiting_scalar(db, stmt, ALLOCATION_LOCK_TIMEOUT)
                if wait
                else await db.scalar(stmt)
            )
            if slot is None:
                raise
    return slot


async def _current_attempt(db, item_id, source, kind, spec):
    """Return a committed current attempt without taking any write lock.

    Overlapping reads of one item issue chunk, frame and prefetch requests for
    the same video, so a shared Item lock must be enough to serve them. Only
    allocation is a writer, and it re-reads the slot under its own lock.
    """
    if kind not in {"frame", "chunk"} or not isinstance(spec, dict):
        return None
    keys = ("frame_index", "width", "format") if kind == "frame" else ("chunk_id",)
    if not all(key in spec for key in keys):
        return None
    item = await row(db, DatasetItem, item_id, lock=True)
    if item is None or item.file_type != "video":
        return None
    generation = await metadata(db, item, source, lock=True)
    expected = (
        frame_spec(generation, spec["frame_index"], spec["width"], spec["format"])
        if kind == "frame"
        else chunk_spec(generation, spec["chunk_id"])
    )
    if spec != expected:
        return None
    slot = await _slot(db, item, kind, expected)
    if slot is None or slot.output_id is None:
        return None
    current = await row(db, Output, slot.output_id)
    if (
        current is not None
        and current.original_ref == originals.receipt(source)
        and current.spec == expected
        and current.metadata_output_id == generation.id
    ):
        if current.state == "ready":
            return current, False
        if current.state == "building" and (
            current.lease_token is None or current.lease_expires_at > now()
        ):
            return current, current.lease_token is None
    return None


async def prepare(db, item_id, authority, kind, *, spec=None, force=False):
    """Allocate metadata only; the caller commits and dispatches after success.

    A read for an already committed attempt never takes the Item or slot write
    lock, so chunk, frame and prefetch requests for one video coexist. Metadata
    allocation keeps the exclusive Item lock because it rewrites the active and
    building pointers.
    """
    from app.services import video_derivative_objects as objects

    source = await originals.check(db, authority, lock=True)
    if source.item_id != item_id:
        reject("content_binding_changed")
    if not force:
        reused = await _current_attempt(db, item_id, source, kind, spec)
        if reused is not None:
            return reused
    item = await row(db, DatasetItem, item_id, lock=True, write=kind == "metadata")
    if item is None or item.file_type != "video":
        reject("video_not_found", 404)
    metadata_output = None
    if kind == "metadata":
        expected_spec = {"schema": 1}
        if isinstance(spec, dict) and "timetable_stride" in spec:
            stride = spec["timetable_stride"]
            if type(stride) is not int or not 1 <= stride <= 1_000_000:
                reject("video_derivative_spec_changed")
            expected_spec["timetable_stride"] = stride
    else:
        metadata_output = await metadata(db, item, source, lock=True)
        if kind == "frame":
            expected_spec = frame_spec(
                metadata_output, spec["frame_index"], spec["width"], spec["format"]
            )
        elif kind == "chunk":
            expected_spec = chunk_spec(metadata_output, spec["chunk_id"])
        else:
            reject()
    if spec is not None and spec != expected_spec:
        reject("video_derivative_spec_changed")
    spec = expected_spec
    slot = await _slot(db, item, kind, spec, lock=True, create=True, wait=True)
    current_id = (
        (item.video_building_output_id or item.video_output_id)
        if kind == "metadata"
        else slot.output_id
    )
    current = await row(db, Output, current_id, lock=True) if current_id else None
    if (
        not force
        and current is not None
        and current.original_ref == originals.receipt(source)
        and current.spec == spec
        and current.metadata_output_id
        == (metadata_output.id if metadata_output else None)
    ):
        if current.state == "ready":
            return current, False
        if current.state == "building" and (
            current.lease_token is None or current.lease_expires_at > now()
        ):
            # Re-delivery of an unclaimed owner is idempotent. No new prefix or
            # replacement authority is created for a broker redelivery.
            return current, current.lease_token is None
    output = await objects.allocate(
        db,
        dataset_item_id=item.id,
        kind=kind,
        spec=spec,
        original_ref=originals.receipt(source),
        auth_ref=originals.authorization(authority),
        metadata_output_id=metadata_output.id if metadata_output else None,
    )
    if kind == "metadata":
        item.video_building_output_id = output.id
    else:
        slot.output_id, slot.status = output.id, "pending"
        slot.storage_key, slot.byte_size, slot.error = None, None, None
        if kind == "chunk":
            for name in ("start_frame", "end_frame", "start_pts_ms", "end_pts_ms"):
                setattr(slot, name, spec[name])
            slot.generation_mode, slot.diagnostics = None, {}
    await db.flush()
    return output, True


async def check(
    db,
    output,
    authority,
    *,
    token=None,
    gate_revision=None,
    read_plan=None,
    lock=True,
    write=False,
    building=False,
):
    """Compare the current logical slot, generation, authority and lease only."""
    source = await originals.check(db, authority, lock=lock)
    if (
        source.item_id != output.dataset_item_id
        or originals.receipt(source) != output.original_ref
    ):
        reject("content_binding_changed")
    item = await row(db, DatasetItem, output.dataset_item_id, lock=lock, write=write)
    if item is None:
        reject("video_not_found", 404)
    slot = await _slot(db, item, output.kind, output.spec, lock=lock, create=False)
    current = await row(db, Output, output.id, lock=lock, write=write)
    if current is None or slot is None:
        reject()
    if current.kind == "metadata":
        expected_id = (
            item.video_building_output_id
            if building or token is not None
            else item.video_output_id
        )
    else:
        expected_id = slot.output_id
        generation = await metadata(db, item, source, lock=lock)
        if generation.id != current.metadata_output_id:
            reject("video_metadata_changed")
        expected_spec = (
            frame_spec(
                generation,
                current.spec["frame_index"],
                current.spec["width"],
                current.spec["format"],
            )
            if current.kind == "frame"
            else chunk_spec(generation, current.spec["chunk_id"])
        )
        if current.spec != expected_spec:
            reject("video_derivative_spec_changed")
    if (
        expected_id != output.id
        or current.original_ref != output.original_ref
        or current.auth_ref != output.auth_ref
    ):
        reject()
    if building and token is None:
        if current.state != "building" or current.lease_token is not None:
            reject("video_derivative_lease_lost")
    elif token is None:
        if current.state != "ready" or (
            current.kind != "metadata" and slot.status != "ready"
        ):
            reject("video_derivative_not_ready")
    elif (
        current.state != "building"
        or current.lease_token != token
        or current.lease_expires_at is None
        or current.lease_expires_at <= now()
    ):
        reject("video_derivative_lease_lost")
    ref = physical.resolve(current).ref
    if token is None and not building and current.state == "ready":
        from app.services import video_derivative_objects as objects

        plan = read_plan or objects.consumed(db, output, authority)
        if plan is None:
            plan = await objects.read_plan(
                db, current, (physical.resolve(current).key("metadata.json"),)
            )
        await objects.check_reads(db, current, plan, lock=lock)
        revision = plan.revision
        if gate_revision is not None and gate_revision != revision:
            reject("storage_maintenance_changed")
    else:
        if read_plan is not None:
            reject()
        revision = await storage_access.check(
            db,
            uuid.UUID(ref["location_id"]),
            expected_revision=gate_revision,
            lock=lock,
        )
    originals.clock(authority)
    return item, slot, current, revision


def validate_result(output, result, inventory):
    owner = physical.resolve(output)
    objects = physical.validate_inventory(output, inventory)
    if (
        not isinstance(result, dict)
        or type(result.get("schema")) is not int
        or result["schema"] != 1
        or result.get("kind") != output.kind
    ):
        reject("video_derivative_result_invalid")
    expected = {owner.key("metadata.json")}
    if output.kind == "metadata":
        metadata_result(
            type(
                "Candidate",
                (),
                {"kind": output.kind, "state": "ready", "result": result},
            )()
        )
        paths = result["video"]
        if paths.get("poster_frame_path"):
            if paths["poster_frame_path"] != owner.key("poster.webp"):
                reject("video_derivative_result_invalid")
            expected.add(paths["poster_frame_path"])
        if paths.get("playback_path"):
            if paths["playback_path"] != owner.key("playback.mp4"):
                reject("video_derivative_result_invalid")
            expected.add(paths["playback_path"])
    else:
        fields = {"schema", "kind", "spec", "metadata_output_id"}
        if output.kind == "chunk":
            fields |= {"generation_mode", "diagnostics"}
        if set(result) != fields:
            reject("video_derivative_result_invalid")
        if result.get("spec") != output.spec or result.get("metadata_output_id") != str(
            output.metadata_output_id
        ):
            reject("video_derivative_result_invalid")
        relative = (
            f"frame.{output.spec['format']}" if output.kind == "frame" else "chunk.mp4"
        )
        expected.add(owner.key(relative))
    if set(objects) != expected:
        reject("video_derivative_result_invalid")
    if output.kind == "chunk":
        if not isinstance(result["generation_mode"], str) or result[
            "generation_mode"
        ] not in {"smart_copy", "transcode"}:
            reject("video_derivative_result_invalid")
        validate_samples(
            result["diagnostics"],
            objects[owner.key("chunk.mp4")]["size_bytes"],
            output.spec,
        )
    metadata_receipt = objects[owner.key("metadata.json")]
    serialized = json.dumps(
        result,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()

    if (
        metadata_receipt["size_bytes"] != len(serialized)
        or metadata_receipt["sha256"] != hashlib.sha256(serialized).hexdigest()
    ):
        reject("video_derivative_result_invalid")


async def publish(db, output, authority, token, result, inventory, gate_revision):
    item, slot, current, _ = await check(
        db, output, authority, token=token, gate_revision=gate_revision, write=True
    )
    validate_result(current, result, inventory)
    current.result, current.inventory, current.state = (
        deepcopy(result),
        deepcopy(inventory),
        "ready",
    )
    if current.kind == "metadata":
        info = VideoMetadata.model_validate(result["video"])
        await db.execute(
            delete(VideoFrameIndex).where(VideoFrameIndex.dataset_item_id == item.id)
        )
        frames = result["frames"]
        stride = current.spec.get("timetable_stride", 1)
        last = frames[-1]["frame_index"] if frames else None
        db.add_all(
            VideoFrameIndex(dataset_item_id=item.id, **frame)
            for frame in frames
            if stride <= 1
            or frame["frame_index"] % stride == 0
            or frame["frame_index"] == last
            or frame["is_keyframe"]
        )
        item.width, item.height = info.width, info.height
        item.metadata_ = {**(item.metadata_ or {}), "video": deepcopy(result["video"])}
        item.thumbnail_path = info.poster_frame_path
        item.video_output_id, item.video_building_output_id = current.id, None
    else:
        relative = (
            f"frame.{current.spec['format']}"
            if current.kind == "frame"
            else "chunk.mp4"
        )
        slot.storage_key = physical.resolve(current).key(relative)
        slot.byte_size = inventory["objects"][slot.storage_key]["size_bytes"]
        slot.status, slot.error, slot.last_accessed_at = "ready", None, now()
        if current.kind == "chunk":
            slot.generation_mode = result["generation_mode"]
            slot.diagnostics = deepcopy(result["diagnostics"])
    from app.services.video_asset_retry_jobs import sync_output

    await sync_output(db, current)
    await db.flush()
    originals.clock(authority)
    return current


async def verified(db, output, authority, *, relative=None, bytes_limit=None):
    """Return only completely verified saved-owner data after fresh authority.

    The route/service releases its initial metadata transaction before calling.
    All guards use short owned sessions; this helper never commits caller work.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.services import video_derivative_objects as objects

    receipt = objects.snapshot(output)
    validate_result(receipt, receipt.result, receipt.inventory)
    factory = async_sessionmaker(storage_access.engine(db), expire_on_commit=False)

    owner = physical.resolve(receipt)
    keys = [owner.key("metadata.json")]
    if relative is not None:
        keys.append(owner.key(relative))
    async with factory() as selected:
        plan = await objects.read_plan(selected, receipt, keys)

    async def guard(revision):
        async with factory() as fresh:
            await check(
                fresh, receipt, authority, gate_revision=revision, read_plan=plan
            )

    value = None
    async with objects.read_access(db, receipt, plan, guard) as storage:
        result_bytes = await originals.physical(
            physical.read_bytes, storage, receipt, owner.key("metadata.json")
        )
        try:
            result = json.loads(result_bytes)
        except (ValueError, UnicodeError):
            reject("video_derivative_result_invalid")
        if result != receipt.result:
            reject("video_derivative_result_invalid")
        if relative is not None:
            key = owner.key(relative)
            if bytes_limit is None:
                await originals.physical(physical.verify_file, storage, receipt, key)
                value = await originals.physical(
                    physical.signed_url, storage, receipt, key
                )
            else:
                value = await originals.physical(
                    physical.read_bytes, storage, receipt, key, max_bytes=bytes_limit
                )
    originals.clock(authority)
    objects.remember_reads(db, receipt, authority, plan)
    return value


async def ready_output(db, slot, authority, *, expected_output_id=None):
    if slot.output_id is None or slot.status != "ready":
        reject("video_derivative_not_ready")
    if expected_output_id is not None and slot.output_id != expected_output_id:
        reject("video_derivative_generation_changed")
    output = await row(db, Output, slot.output_id)
    if output is None or output.dataset_item_id != slot.dataset_item_id:
        reject()
    await check(db, output, authority)
    validate_result(output, output.result, output.inventory)
    return output


def identity(output, relative):
    receipt = physical.validate_inventory(output)[
        physical.resolve(output).key(relative)
    ]
    return {
        "output_id": output.id,
        "sha256": receipt["sha256"],
        "metadata_generation_id": output.metadata_output_id or output.id,
        "byte_size": receipt["size_bytes"],
    }


async def poster(db, item, authority):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.services import video_derivative_objects as objects

    objects.begin_reads(db, authority)
    factory = async_sessionmaker(storage_access.engine(db), expire_on_commit=False)
    async with factory() as fresh:
        source = await originals.check(fresh, authority)
        current_item = await row(fresh, DatasetItem, item.id)
        output = objects.snapshot(await metadata(fresh, current_item, source))
        paths = output.result["video"]
    return await verified(
        db,
        output,
        authority,
        relative="poster.webp" if paths.get("poster_frame_path") else None,
    )


async def thumbnail(db, *, item_id=None, task_id=None):
    """Saved-owner thumbnail for already-authorized internal list serializers.

    This technical delegation never authorizes a public request or queues work.
    Unknown/legacy media yields no URL, rather than a current-bucket fallback.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.services import video_derivative_objects as objects

    if (item_id is None) == (task_id is None):
        reject()
    kind, identifier = (
        ("task", task_id) if task_id is not None else ("dataset_item", item_id)
    )
    factory = async_sessionmaker(storage_access.engine(db), expire_on_commit=False)
    try:
        async with factory() as fresh:
            source = await originals.pin(fresh, kind, identifier)
            authority = await originals.delegated(
                fresh, originals.system_authorization(kind, identifier, source)
            )
            item = await row(fresh, DatasetItem, source.item_id)
            output = objects.snapshot(await metadata(fresh, item, source))
            path = output.result["video"].get("poster_frame_path")
        if path is None:
            return None
        objects.begin_reads(db, authority)
        url = await verified(db, output, authority, relative="poster.webp")
        return path, url
    except (ContentError, physical.VideoDerivativeStorageError, HTTPException):
        return None


def dispatch(output_ids):
    """Dispatch only previously committed physical attempts, never raw paths."""
    from app.workers.media import generate_video_derivatives

    identifiers = [str(uuid.UUID(str(value))) for value in output_ids]
    if identifiers:
        return generate_video_derivatives.delay(identifiers)
