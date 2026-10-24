"""Explicit canonical video inputs and immutable outputs for legacy API suites.

HTTP tests replace only the physical transport boundary because their database
fixture is an uncommitted savepoint. Decoder tests use a real anonymous stage and
the saved-owner storage implementation against the in-memory provider instead.
"""

from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

from sqlalchemy import select

from app.db.models.dataset import DatasetItem, VideoFrameIndex
from app.db.models.dataset_asset import DatasetAsset
from app.services import storage_access
from app.services import video_derivatives as core
from app.services import video_derivative_objects as objects
from app.services import video_derivative_originals as originals
from app.services import video_derivative_storage as physical
from app.services.dataset_assets import LEGACY_LOCATION_ID, register_item_asset

RAW_VIDEO = b"\x00\x00\x00\x18ftypmp42" + b"canonical-original-video" * 8


def saved_url(output, relative):
    owner = physical.resolve(output)
    return f"https://saved-video.test/{owner.bucket}/{owner.key(relative)}"


async def video_authority(db, item, *, register=True):
    if register:
        asset = await db.scalar(
            select(DatasetAsset).where(DatasetAsset.dataset_item_id == item.id)
        )
        if asset is None:
            item.file_size = len(RAW_VIDEO)
            await db.flush()
            await register_item_asset(
                db,
                item,
                sha256=hashlib.sha256(RAW_VIDEO).hexdigest(),
                size_bytes=len(RAW_VIDEO),
            )
            await storage_access.ensure(db, LEGACY_LOCATION_ID)
        else:
            # Re-registration refreshes replica.verified_at, which is part of
            # the immutable source receipt. A later fixture consumer must only
            # read the already registered source and never rewrite its facts.
            assert asset.media_type == "video"
            assert asset.verification_state == "verified"
            assert asset.checksum_algorithm == "sha256"
            assert asset.checksum_value == hashlib.sha256(RAW_VIDEO).hexdigest()
            assert asset.size_bytes == item.file_size == len(RAW_VIDEO)
    source = await originals.pin(db, "dataset_item", item.id)
    return await originals.delegated(
        db, originals.system_authorization("dataset_item", item.id, source)
    )


def inventory(output, result, payload=None):
    serialized = json.dumps(
        result,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    return {
        "schema": 1,
        "objects": {
            physical.resolve(output).key(name): {
                "sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
            }
            for name, data in {"metadata.json": serialized, **(payload or {})}.items()
        },
    }


async def publish_result(db, output, authority, result, receipts):
    token = output.lease_token or await objects.claim(db, output)
    revision = await storage_access.check(db, LEGACY_LOCATION_ID)
    await core.publish(db, output, authority, token, result, receipts, revision)
    await db.commit()
    return output


async def owned_metadata(
    db, item, *, video=None, frames=None, force=False, poster=None, playback=None
):
    authority = await video_authority(db, item)
    raw = dict(
        video if video is not None else (item.metadata_ or {}).get("video") or {}
    )
    raw = {
        "fps": 30,
        "frame_count": 90,
        "width": 640,
        "height": 360,
        "duration_ms": 3000,
        "codec": "h264",
        **raw,
    }
    poster = bool(raw.get("poster_frame_path")) if poster is None else poster
    playback = bool(raw.get("playback_path")) if playback is None else playback
    if frames is None:
        rows = (
            await db.scalars(
                select(VideoFrameIndex)
                .where(VideoFrameIndex.dataset_item_id == item.id)
                .order_by(VideoFrameIndex.frame_index)
            )
        ).all()
        frames = [
            {
                name: getattr(row, name)
                for name in (
                    "frame_index",
                    "pts_ms",
                    "is_keyframe",
                    "pict_type",
                    "byte_offset",
                )
            }
            for row in rows
        ]
    output, _ = await core.prepare(
        db, item.id, authority, "metadata", spec={"schema": 1}, force=force
    )
    if output.state == "ready":
        await db.commit()
        return output
    owner = physical.resolve(output)
    raw["poster_frame_path"] = owner.key("poster.webp") if poster else None
    raw["playback_path"] = owner.key("playback.mp4") if playback else None
    if playback:
        raw["playback_codec"] = "h264"
    result = {"schema": 1, "kind": "metadata", "video": raw, "frames": deepcopy(frames)}
    payload = {}
    if poster:
        payload["poster.webp"] = b"owned video poster"
    if playback:
        payload["playback.mp4"] = b"owned browser playback"
    return await publish_result(
        db, output, authority, result, inventory(output, result, payload)
    )


async def owned_output(
    db,
    item,
    kind,
    *,
    frame_index=0,
    width=512,
    fmt="webp",
    chunk_id=0,
    ready=True,
    claimed=False,
    diagnostics=None,
    generation_mode="transcode",
    data=None,
):
    authority = await video_authority(db, item)
    source = await originals.check(db, authority)
    generation = await core.metadata(db, item, source)
    spec = (
        core.frame_spec(generation, frame_index, width, fmt)
        if kind == "frame"
        else core.chunk_spec(generation, chunk_id)
    )
    output, _ = await core.prepare(db, item.id, authority, kind, spec=spec, force=True)
    if claimed or ready:
        await objects.claim(db, output)
    if not ready:
        await db.commit()
        return output
    result = {
        "schema": 1,
        "kind": kind,
        "spec": deepcopy(spec),
        "metadata_output_id": str(generation.id),
    }
    relative = f"frame.{fmt}" if kind == "frame" else "chunk.mp4"
    if kind == "chunk":
        result.update(
            generation_mode=generation_mode, diagnostics=deepcopy(diagnostics or {})
        )
    data = data or (b"owned frame image" if kind == "frame" else b"owned video chunk")
    return await publish_result(
        db, output, authority, result, inventory(output, result, {relative: data})
    )


def install_video_transport(monkeypatch):
    state = SimpleNamespace(calls=[], queued=[])

    async def verified(db, output, authority, *, relative=None, bytes_limit=None):
        assert not db.in_transaction(), (
            "release initial business locks before physical reads"
        )
        core.validate_result(output, output.result, output.inventory)
        await core.check(db, output, authority)
        await db.commit()
        state.calls.append((output.id, relative))
        if bytes_limit is not None:
            raise AssertionError("array decoding needs an explicit byte fixture")
        return saved_url(output, relative) if relative else None

    async def thumbnail(db, *, item_id=None, task_id=None):
        kind, identifier = ("task", task_id) if task_id else ("dataset_item", item_id)
        try:
            source = await originals.pin(db, kind, identifier)
            item = await db.get(DatasetItem, source.item_id)
            output = await core.metadata(db, item, source)
            core.validate_result(output, output.result, output.inventory)
            path = output.result["video"].get("poster_frame_path")
            return (path, saved_url(output, "poster.webp")) if path else None
        except (ValueError, physical.VideoDerivativeStorageError):
            return None

    def dispatch(identifiers):
        if identifiers:
            state.queued.append(list(identifiers))

    monkeypatch.setattr(core, "verified", verified)
    monkeypatch.setattr(core, "dispatch", dispatch)
    monkeypatch.setattr(core, "thumbnail", thumbnail)
    return state
