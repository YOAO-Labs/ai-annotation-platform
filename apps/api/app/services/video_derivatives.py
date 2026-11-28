"""Video logical slots, immutable generations and short publication fences."""

from copy import deepcopy
from datetime import datetime, timezone
from dataclasses import dataclass
import hashlib
import json
import math
import uuid
import base64
import binascii

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.schemas.task import VideoMetadata
from app.services import video_derivative_objects as objects
from app.services import video_derivative_originals as originals
from app.services import video_derivative_storage as physical
from app.services import storage_access
from app.services.content_snapshot import ContentError


ALLOCATION_LOCK_TIMEOUT = "1s"


def now():
    return datetime.now(timezone.utc)


def reject(code="video_derivative_changed", status=409):
    raise ContentError(code, status)


@dataclass(frozen=True, slots=True)
class VideoItem:
    id: uuid.UUID
    file_type: str
    file_path: str
    width: int | None
    height: int | None
    video_output_id: uuid.UUID | None
    video_building_output_id: uuid.UUID | None


async def item_row(db, identifier, *, lock=False, write=False) -> VideoItem | None:
    if db.autoflush:
        await db.flush()
    row = (
        (
            await db.execute(
                text("SELECT * FROM yoao_assets_v1.legacy_video_item(:item, :lock)"),
                {
                    "item": identifier,
                    "lock": ("update" if write else "share") if lock else "read",
                },
            )
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else VideoItem(**row)


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
    output = await objects.read_attempt(
        db, item.id, "metadata", item.video_output_id, lock="share" if lock else "read"
    )
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


@dataclass(frozen=True, slots=True)
class Slot:
    id: uuid.UUID
    dataset_item_id: uuid.UUID
    output_id: uuid.UUID | None
    status: str
    kind: str


async def slot_for(db, item, kind, spec, *, lock=False, create=False, write=False):
    if kind == "metadata":
        return item
    if kind not in {"frame", "chunk"}:
        reject()
    if db.autoflush:
        await db.flush()
    keys = {"item": item.id, "kind": kind, "spec": spec}
    read = text(
        "SELECT * FROM yoao_assets_v1.legacy_video_slot(:item, :kind, :spec, :lock)"
    ).bindparams(bindparam("spec", type_=JSONB))
    lock_mode = ("update" if create or write else "share") if lock else "read"

    async def selected():
        if lock:
            previous = await db.scalar(text("SHOW lock_timeout"))
            await db.execute(
                text("SELECT set_config('lock_timeout', :timeout, true)"),
                {"timeout": ALLOCATION_LOCK_TIMEOUT},
            )
        row = (
            (await db.execute(read, {**keys, "lock": lock_mode}))
            .mappings()
            .one_or_none()
        )
        if lock:
            await db.execute(
                text("SELECT set_config('lock_timeout', :timeout, true)"),
                {"timeout": previous},
            )
        return row

    facts = await selected()
    if facts is None and create:
        try:
            async with db.begin_nested():
                facts = (
                    (
                        await db.execute(
                            text(
                                "SELECT * FROM yoao_assets_v1.legacy_video_create_slot(:item, :kind, :spec)"
                            ).bindparams(bindparam("spec", type_=JSONB)),
                            keys,
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
        except IntegrityError as exc:
            if (
                getattr(exc.orig, "sqlstate", None) != "23505"
                and getattr(exc.orig, "pgcode", None) != "23505"
            ):
                raise
            facts = await selected()
            if facts is None:
                raise
    return None if facts is None else Slot(kind=kind, **facts)


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
    item = await item_row(db, item_id, lock=True)
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
    slot = await slot_for(db, item, kind, expected)
    if slot is None or slot.output_id is None:
        return None
    current = await objects.read_attempt(db, item.id, kind, slot.output_id)
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

    source = await originals.check(db, authority, lock=True)
    if source.item_id != item_id:
        reject("content_binding_changed")
    if not force:
        reused = await _current_attempt(db, item_id, source, kind, spec)
        if reused is not None:
            return reused
    item = await item_row(db, item_id, lock=True, write=kind == "metadata")
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
    slot = await slot_for(db, item, kind, spec, lock=True, create=True)
    if slot is None:
        reject()
    current_id = (
        (item.video_building_output_id or item.video_output_id)
        if kind == "metadata"
        else slot.output_id
    )
    current = (
        await objects.read_attempt(db, item.id, kind, current_id, lock="share")
        if current_id is not None
        else None
    )
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
    await objects.attach(
        db,
        output,
        slot_id=None if kind == "metadata" else slot.id,
        previous_output_id=item.video_building_output_id
        if kind == "metadata"
        else current_id,
    )
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
    source = await originals.check(
        db,
        authority,
        lock=lock,
        wait_for_item=(
            token is not None or (not (write or building) and output.kind == "metadata")
        ),
    )
    if (
        source.item_id != output.dataset_item_id
        or originals.receipt(source) != output.original_ref
    ):
        reject("content_binding_changed")
    item = await item_row(
        db, output.dataset_item_id, lock=lock, write=write and output.kind == "metadata"
    )
    if item is None:
        reject("video_not_found", 404)
    slot = await slot_for(db, item, output.kind, output.spec, lock=lock, write=write)
    current = await objects.read_attempt(
        db,
        item.id,
        output.kind,
        output.id,
        lock=("update" if write else "share") if lock else "read",
    )
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
    _, _, current, _ = await check(
        db, output, authority, token=token, gate_revision=gate_revision, write=True
    )
    validate_result(current, result, inventory)
    info = (
        VideoMetadata.model_validate(result["video"])
        if current.kind == "metadata"
        else None
    )
    published = (
        await db.execute(
            text(
                "SELECT * FROM yoao_assets_v1.legacy_video_publish_attempt("
                ":output, :token, :result, :inventory, :accessed, :width, :height, :poster)"
            ).bindparams(
                bindparam("result", type_=JSONB), bindparam("inventory", type_=JSONB)
            ),
            {
                "output": current.id,
                "token": token,
                "result": result,
                "inventory": inventory,
                "accessed": now(),
                "width": info.width if info else None,
                "height": info.height if info else None,
                "poster": info.poster_frame_path if info else None,
            },
        )
    ).one_or_none()
    if published is None:
        reject("video_derivative_lease_lost")
    current.result, current.inventory, current.state = (
        deepcopy(result),
        deepcopy(inventory),
        "ready",
    )
    current.updated_at, current.fixture_request_versions = published
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
    output = await objects.read_attempt(
        db, slot.dataset_item_id, slot.kind, slot.output_id
    )
    if output is None:
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

    objects.begin_reads(db, authority)
    factory = async_sessionmaker(storage_access.engine(db), expire_on_commit=False)
    async with factory() as fresh:
        source = await originals.check(fresh, authority)
        current_item = await item_row(fresh, item.id)
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
            item = await item_row(fresh, source.item_id)
            output = objects.snapshot(await metadata(fresh, item, source))
            path = output.result["video"].get("poster_frame_path")
        if path is None:
            return None
        objects.begin_reads(db, authority)
        url = await verified(db, output, authority, relative="poster.webp")
        return path, url
    except (ContentError, physical.VideoDerivativeStorageError, HTTPException):
        return None


async def prepare_dispatch(db, output_ids, *, delivery_id=None):
    """Bind each real output source in the caller's existing transaction."""
    from app.services import media_dispatch

    identifiers = [uuid.UUID(str(value)) for value in output_ids]
    if not identifiers:
        return []
    if delivery_id is not None and len(identifiers) != 1:
        raise ContentError("fixture_media_scope_mismatch", 409)
    if db.autoflush:
        await db.flush()
    rows = (
        await db.execute(
            text(
                "SELECT * FROM yoao_assets_v1.legacy_video_dispatch_sources(:outputs)"
            ),
            {"outputs": identifiers},
        )
    ).all()
    outputs = {row.id: row for row in rows}
    if any(identifier not in outputs for identifier in identifiers):
        raise ContentError("video_derivative_output_missing", 409)
    groups = {}
    for identifier in identifiers:
        output = outputs[identifier]
        key = (output.dataset_item_id, originals.encoded(output.original_ref))
        groups.setdefault(key, []).append(identifier)

    async def prepare_group(group):
        owner_id = outputs[group[0]].dataset_item_id
        source = {
            "schema": "aap-video-derivative-inputs/v1",
            "outputs": [
                {
                    "output_id": str(identifier),
                    "original": outputs[identifier].original_ref,
                }
                for identifier in group
            ],
        }
        values = [str(identifier) for identifier in group]
        preparation = await media_dispatch.prepare(
            db,
            owner_kind="dataset_item",
            owner_id=owner_id,
            producer_kind="video_derivatives",
            source=source,
            task_name="app.workers.media.generate_video_derivatives",
            args=[values],
            delivery_id=delivery_id,
        )
        return preparation, values

    fixture, ordinary_ids = [], set()
    for group in groups.values():
        preparation, values = await prepare_group(group)
        if delivery_id is not None and preparation.ownership != "fixture":
            raise ContentError("fixture_media_scope_untracked", 409)
        if preparation.ownership == "fixture":
            fixture.append((preparation, values))
        else:
            ordinary_ids.update(group)
    if ordinary_ids:
        ordinary_group = [
            identifier for identifier in identifiers if identifier in ordinary_ids
        ]
        preparation, values = await prepare_group(ordinary_group)
        if preparation.ownership != "ordinary":
            raise ContentError("fixture_media_scope_changed", 409)
        fixture.append((preparation, values))
    return fixture


async def publish_dispatch(prepared):
    from app.services import media_dispatch
    from app.workers.media import generate_video_derivatives

    for preparation, identifiers in prepared:
        await media_dispatch.publish(
            preparation, lambda: generate_video_derivatives.delay(identifiers)
        )
