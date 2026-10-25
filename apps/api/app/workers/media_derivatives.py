"""Physically drained video generation from pinned originals and saved owners."""

from copy import deepcopy
from dataclasses import dataclass
import asyncio
import logging
from pathlib import Path
import re
import subprocess
import tempfile
import uuid

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.exc import DBAPIError

from app.config import settings
from app.db.models.dataset import DatasetItem
from app.db.models.video_derivative import VideoDerivativeOutput as Output
from app.services import media_cache_accounting
from app.services import video_derivative_objects as objects
from app.services import video_derivative_originals as originals
from app.services import storage_access
from app.services import video_derivative_storage as physical
from app.services import video_derivatives as contract
from app.services.content_snapshot import ContentError

log = logging.getLogger(__name__)


def _known_decoder_failure(exc):
    if getattr(exc, "storage_operation_unsettled", False):
        raise exc
    if not isinstance(exc, (RuntimeError, subprocess.TimeoutExpired, ValueError)):
        raise exc


def _decoder_config(path):
    from app.workers import media_codec
    import base64

    # All generated chunks use fragmented MP4 with an initial moov. Reading the
    # entire (potentially GiB-sized) mdat to find its codec config is unnecessary.
    with storage_access.closing_registered(path.open("rb")) as stream:
        data = stream.read(1024 * 1024)
    avcc = media_codec._find_mp4_box_payload(data, b"avcC")
    if avcc is not None:
        return media_codec._avc_codec_string(avcc), base64.b64encode(avcc).decode(
            "ascii"
        )
    hvcc = media_codec._find_mp4_box_payload(data, b"hvcC")
    if hvcc is not None:
        return media_codec._hevc_codec_string(hvcc), base64.b64encode(hvcc).decode(
            "ascii"
        )
    return None, None


def _samples(path, start_frame):
    """The derived-file probe owns a descriptor and the strict native runner."""
    from app.workers.media_probe import probe_chunk_samples
    from app.workers.media_process import decoder_input

    with storage_access.closing_registered(path.open("rb")) as stream:
        stage = type(
            "DerivedStage", (), {"file": stream, "size": path.stat().st_size}
        )()
        input_path, descriptors = decoder_input(stage)
        return probe_chunk_samples(input_path, start_frame, pass_fds=descriptors)


def _metadata(output, input_path, fds, directory):
    from app.workers import media

    info = media.probe_video_file(input_path, pass_fds=fds)
    frames = []
    try:
        frames = media.probe_video_frame_timetable(input_path, pass_fds=fds)
        info["frame_timetable_frame_count"] = len(frames)
    except Exception as exc:
        _known_decoder_failure(exc)
        info["frame_timetable_error"] = "frame_timetable_failed"
    paths = []
    owner = physical.resolve(output)
    if info.get("codec") and info["codec"] not in media.BROWSER_PLAYABLE_VIDEO_CODECS:
        playback = directory / "playback.mp4"
        try:
            media.transcode_video_for_browser(input_path, playback, pass_fds=fds)
        except Exception as exc:
            _known_decoder_failure(exc)
            playback.unlink(missing_ok=True)
            info["playback_error"] = "playback_transcode_failed"
        else:
            info["playback_path"] = owner.key("playback.mp4")
            info["playback_codec"] = "h264"
            paths.append((playback, "playback.mp4", "video/mp4"))
    poster = directory / "poster.webp"
    try:
        media.extract_video_poster(input_path, poster, pass_fds=fds)
    except Exception as exc:
        _known_decoder_failure(exc)
        poster.unlink(missing_ok=True)
        info["poster_error"] = "poster_extraction_failed"
    else:
        info["poster_frame_path"] = owner.key("poster.webp")
        paths.append((poster, "poster.webp", "image/webp"))
    result = {"schema": 1, "kind": "metadata", "video": info, "frames": frames}
    contract.metadata_result(
        type(
            "Candidate", (), {"kind": "metadata", "state": "ready", "result": result}
        )()
    )
    return result, paths


def _chunk(output, metadata, input_path, fds, directory):
    from app.workers import media
    from app.workers.media_codec import _webcodecs_codec_string

    info, frames = contract.metadata_result(metadata)
    spec = output.spec
    by_index = {frame["frame_index"]: frame for frame in frames}
    start = by_index.get(spec["start_frame"])
    end = by_index.get(spec["end_frame"])
    # Always describe the canonical original, even when a browser transcode is
    # present in metadata. Its codec/frame numbering cannot select this input.
    codec = media._normalize_video_codec(info.codec)
    keyframe = start["is_keyframe"] if start else None
    reason = media._transcode_fallback_reason(codec, keyframe, start is not None)
    diagnostics = {
        "source_codec": codec,
        "output_codec": None,
        "keyframe_aligned": keyframe,
        "start_byte_offset": start["byte_offset"] if start else None,
        "end_byte_offset": end["byte_offset"] if end else None,
        "smart_copy_eligible": reason is None,
        "fallback_reason": reason,
    }
    path = directory / "chunk.mp4"
    generation_mode = "transcode"
    if reason is None:
        duration = max(
            1, spec["end_pts_ms"] - spec["start_pts_ms"] + round(1000 / info.fps)
        )
        try:
            media.extract_video_chunk_smart_copy(
                input_path, path, spec["start_pts_ms"], duration, pass_fds=fds
            )
            copied_samples = _samples(path, spec["start_frame"])
            expected = set(range(spec["start_frame"], spec["end_frame"] + 1))
            if {sample["frame_index"] for sample in copied_samples} != expected:
                raise ValueError("video_chunk_frame_count_mismatch")
        except Exception as exc:
            _known_decoder_failure(exc)
            path.unlink(missing_ok=True)
            diagnostics.update(
                smart_copy_eligible=False, fallback_reason="smart_copy_failed"
            )
        else:
            generation_mode = "smart_copy"
            diagnostics["output_codec"] = codec
    if generation_mode == "transcode":
        media.extract_video_chunk(
            input_path,
            path,
            spec["start_pts_ms"],
            spec["end_frame"] - spec["start_frame"] + 1,
            pass_fds=fds,
        )
        diagnostics["output_codec"] = "h264"
    try:
        samples = _samples(path, spec["start_frame"])
        if samples:
            codec_string, description = _decoder_config(path)
            diagnostics.update(
                codec_string=codec_string
                or _webcodecs_codec_string(diagnostics["output_codec"]),
                width=info.width,
                height=info.height,
                samples=samples,
            )
            if description:
                diagnostics["description"] = description
    except Exception as exc:
        _known_decoder_failure(exc)
    # A known probe failure may keep ordinary video playback available. A
    # present precise packet table must describe every promised frame exactly.
    contract.validate_samples(diagnostics, path.stat().st_size, spec)
    return {
        "schema": 1,
        "kind": "chunk",
        "metadata_output_id": str(metadata.id),
        "spec": deepcopy(spec),
        "generation_mode": generation_mode,
        "diagnostics": diagnostics,
    }, [(path, "chunk.mp4", "video/mp4")]


def build(storage, output, stage, metadata=None, *, accounting=None):
    """No ORM or remote-original I/O; the caller retains both durable permits."""
    from app.workers.media_process import decoder_input
    from app.workers.media_frames import extract_video_frame_image

    input_path, fds = decoder_input(stage)
    directory = tempfile.TemporaryDirectory(prefix=f"anno-video-derived-{output.id}-")
    unsettled = False
    try:
        temporary = Path(directory.name)
        if output.kind == "metadata":
            result, paths = _metadata(output, input_path, fds, temporary)
        elif output.kind == "chunk":
            result, paths = _chunk(output, metadata, input_path, fds, temporary)
        elif output.kind == "frame":
            spec = output.spec
            relative = f"frame.{spec['format']}"
            path = temporary / relative
            extract_video_frame_image(
                input_path, path, spec["pts_ms"], spec["width"], pass_fds=fds
            )
            result = {
                "schema": 1,
                "kind": "frame",
                "metadata_output_id": str(metadata.id),
                "spec": deepcopy(spec),
            }
            paths = [
                (
                    path,
                    relative,
                    "image/webp" if spec["format"] == "webp" else "image/jpeg",
                )
            ]
        else:
            contract.reject("video_derivative_result_invalid")
        owner = physical.resolve(output)
        inventory = {"schema": 1, "objects": {}}
        for path, relative, content_type in paths:
            inventory["objects"][owner.key(relative)] = physical.upload_file(
                storage,
                output,
                path,
                relative,
                content_type,
                **({"accounting": accounting} if accounting is not None else {}),
            )
        inventory["objects"][owner.key("metadata.json")] = physical.write_result(
            storage,
            output,
            result,
            **({"accounting": accounting} if accounting is not None else {}),
        )
        physical.verify_inventory(storage, output, inventory)
        contract.validate_result(output, result, inventory)
        return result, inventory
    except BaseException as exc:
        unsettled = bool(getattr(exc, "storage_operation_unsettled", False))
        raise
    finally:
        if unsettled:
            # An unconfirmed child may still own output files. Its durable owner
            # and permit retain responsibility; no destructor may erase them.
            directory._finalizer.detach()
        else:
            try:
                directory.cleanup()
            except BaseException as exc:
                directory._finalizer.detach()
                raise storage_access.mark_unsettled(exc)


@dataclass(frozen=True)
class Claimed:
    output: object
    authority: originals.Authority
    source: object
    token: uuid.UUID
    metadata: object = None


def error_code(exc):
    code = getattr(exc, "code", None)
    if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,127}", code):
        code = "video_derivative_generation_failed"
    return code


async def fail(factory, identifier, token, code):
    """A failed or duplicate delivery cannot fail a different physical attempt."""
    async with factory() as db:
        located = await contract.row(db, Output, identifier)
        if located is None:
            return
        item = await contract.row(
            db, DatasetItem, located.dataset_item_id, lock=True, write=True
        )
        slot = (
            await contract._slot(db, item, located.kind, located.spec, lock=True)
            if item
            else None
        )
        current = await contract.row(db, Output, identifier, lock=True, write=True)
        if (
            current is None
            or current.state != "building"
            or current.lease_token != token
        ):
            return
        if item is not None and current.kind == "metadata":
            if item.video_building_output_id == identifier:
                item.video_building_output_id = None
                if item.video_output_id is None:
                    video = {
                        **((item.metadata_ or {}).get("video") or {}),
                        "probe_error": code,
                    }
                    item.metadata_ = {**(item.metadata_ or {}), "video": video}
        elif slot is not None and slot.output_id == identifier:
            slot.output_id, slot.status, slot.error = None, "failed", code
            slot.storage_key, slot.byte_size = None, None
            if current.kind == "chunk":
                slot.generation_mode, slot.diagnostics = None, {}
        current.state, current.error_code = "retiring", code
        from app.services.video_asset_retry_jobs import sync_output

        await sync_output(db, current)
        await db.commit()


async def _claim(factory, identifier):
    async with factory() as db:
        output = await contract.row(db, Output, identifier)
        if (
            output is None
            or output.state != "building"
            or output.lease_token is not None
        ):
            return None
        authority = await originals.delegated(db, output.auth_ref)
        source = await originals.check(db, authority)
        _, _, current, _ = await contract.check(
            db, output, authority, building=True, write=True
        )
        token = await objects.claim(db, current, ttl_seconds=originals.JOB_SECONDS)
        if token is None:
            return None
        frozen = objects.snapshot(current)
        metadata = None
        if frozen.metadata_output_id is not None:
            metadata = objects.snapshot(
                await contract.row(db, Output, frozen.metadata_output_id)
            )
        await db.commit()
        return Claimed(frozen, authority, source, token, metadata)


async def claim_with_retry(factory, identifier):
    """Short reader locks never turn an untouched attempt into a failed owner."""
    for attempt in range(8):
        try:
            return await _claim(factory, identifier)
        except (DBAPIError, ContentError) as exc:
            if not transient(exc):
                raise
            if attempt == 7:
                # The owner remains unclaimed. A subsequent request redelivers
                # the same attempt instead of changing its immutable authority.
                log.info("video derivative claim deferred: code=content_busy")
                return None
            await asyncio.sleep(0.05 * (attempt + 1))


def transient(exc):
    return (isinstance(exc, DBAPIError) and storage_access.unavailable(exc)) or (
        isinstance(exc, ContentError)
        and exc.code
        in {"storage_original_busy", "storage_maintenance_busy", "content_busy"}
    )


async def fail_with_retry(factory, identifier, token, code):
    for attempt in range(8):
        try:
            await fail(factory, identifier, token, code)
            return
        except (DBAPIError, ContentError) as exc:
            if not transient(exc):
                raise
            if attempt == 7:
                # No lease is renewed and no responsibility is erased. The
                # reconciler can retire the abandoned owner after its deadline.
                log.info("video derivative failure deferred: code=content_busy")
                return
            await asyncio.sleep(0.05 * (attempt + 1))


async def run_outputs(identifiers):
    """Reuse one full capture for a batch with the same frozen input/authority."""
    engine = create_async_engine(settings.database_url, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    groups = {}
    try:
        for raw in dict.fromkeys(identifiers):
            identifier = uuid.UUID(str(raw))
            try:
                claimed = await claim_with_retry(factory, identifier)
            except Exception as exc:
                await fail_with_retry(factory, identifier, None, error_code(exc))
                log.warning("video derivative claim refused: code=%s", error_code(exc))
                continue
            if claimed is None:
                continue
            key = originals.encoded(
                [
                    claimed.output.original_ref,
                    claimed.output.auth_ref,
                    str(claimed.output.metadata_output_id),
                ]
            )
            groups.setdefault(key, []).append(claimed)
        for claims in groups.values():
            completed = []
            try:
                async with factory() as admission:
                    first = claims[0]
                    async with originals.media(
                        admission, first.authority, first.source
                    ) as stage:
                        for claimed in claims:
                            revisions = []

                            async def guard(revision, claimed=claimed):
                                async with factory() as fresh:
                                    await contract.check(
                                        fresh,
                                        claimed.output,
                                        claimed.authority,
                                        token=claimed.token,
                                        gate_revision=revision,
                                    )
                                if not revisions:
                                    revisions.append(revision)
                                elif revisions[0] != revision:
                                    contract.reject("storage_maintenance_changed")

                            recorder = media_cache_accounting.Recorder(
                                factory,
                                "video_derivative_outputs",
                                claimed.output.id,
                                claimed.output.storage_ref["endpoint_url"],
                                claimed.output.storage_ref["bucket"],
                            )
                            settled = True
                            try:
                                async with objects.access(
                                    admission,
                                    claimed.output,
                                    guard,
                                    single_attempt=True,
                                ) as storage:
                                    result, inventory = await originals.physical(
                                        build,
                                        storage,
                                        claimed.output,
                                        stage,
                                        claimed.metadata,
                                        accounting=recorder,
                                        timeout=originals.JOB_SECONDS,
                                    )
                            except BaseException as exc:
                                settled = not getattr(
                                    exc, "storage_operation_unsettled", False
                                )
                                raise
                            finally:
                                await recorder.finish(settled=settled)
                            completed.append((claimed, result, inventory, revisions[0]))
                # Every decoder, output file/client and the shared source stage
                # has really closed before any member of this batch is ready.
                for claimed, result, inventory, revision in completed:
                    async with factory() as final:
                        await contract.publish(
                            final,
                            claimed.output,
                            claimed.authority,
                            claimed.token,
                            result,
                            inventory,
                            revision,
                        )
                        await final.commit()
            except BaseException as exc:
                code = error_code(exc)
                for claimed in claims:
                    await originals.drain(
                        asyncio.create_task(
                            fail_with_retry(
                                factory, claimed.output.id, claimed.token, code
                            )
                        )
                    )
                log.warning("video derivative generation refused: code=%s", code)
                if not isinstance(exc, Exception):
                    raise
    finally:
        await originals.drain(asyncio.create_task(engine.dispose()))


async def run_legacy(
    kind,
    item_id,
    *,
    delegation=None,
    frame_indices=None,
    width=512,
    fmt="webp",
    chunk_ids=None,
):
    """Compatibility task entry, requiring a pinned, explicit producer receipt."""
    if not isinstance(delegation, dict):
        log.warning(
            "video derivative legacy task refused: code=video_derivative_authority_unrecorded"
        )
        return
    engine = create_async_engine(settings.database_url, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    queued = []
    try:
        async with factory() as db:
            authority = await originals.delegated(db, delegation)
            source = await originals.check(db, authority)
            identifier = uuid.UUID(item_id)
            if source.item_id != identifier:
                contract.reject("content_binding_changed")
            if kind == "metadata":
                item = await contract.row(db, DatasetItem, identifier)
                force = False
                if item.video_output_id and not item.video_building_output_id:
                    active = await contract.row(db, Output, item.video_output_id)
                    force = (
                        active is not None
                        and active.state == "ready"
                        and not ((active.result or {}).get("video") or {}).get(
                            "poster_frame_path"
                        )
                    )
                output, needed = await contract.prepare(
                    db, identifier, authority, kind, force=force
                )
                if needed:
                    queued.append(output.id)
            else:
                item = await contract.row(db, DatasetItem, identifier)
                generation = await contract.metadata(db, item, source)
                specs = (
                    [
                        contract.frame_spec(generation, index, width, fmt)
                        for index in frame_indices
                    ]
                    if kind == "frame"
                    else [contract.chunk_spec(generation, index) for index in chunk_ids]
                )
                for spec in specs:
                    output, needed = await contract.prepare(
                        db, identifier, authority, kind, spec=spec
                    )
                    if needed:
                        queued.append(output.id)
            await db.commit()
        await run_outputs(queued)
    finally:
        await originals.drain(asyncio.create_task(engine.dispose()))
