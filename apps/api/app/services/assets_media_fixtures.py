"""Test-only publication of deterministic WebCodecs fixtures through Assets media.

The E2E seed endpoint owns its surrounding SQLAlchemy transaction. This adapter
uses the same native worker functions on that caller connection so the test rows
and accepted media jobs commit together; only fixture bytes are uploaded to the
attempt-scoped media cache.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
from uuid import UUID, uuid4, uuid5, NAMESPACE_URL

from aap_assets_media.contract import (
    BATCH_SIZE,
    Artifact,
    ChunkResult,
    ChunkSpec,
    ClaimRequest,
    FailRequest,
    FinishRequest,
    FrameEntry,
    MediaFailure,
    MetadataResult,
    MetadataSpec,
    ObjectIdentity,
    ScheduleWorkRequest,
    StageTimetableRequest,
)
from app.services.assets_media import SqlAlchemyMediaDatabase, media_storage


def _operation_id(dataset_item_id: UUID, operation: str) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"aap:e2e:assets-media:{dataset_item_id}:{operation}",
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _claim(native, job_id: UUID, kind: str):
    claimed = await native.call(
        "claim_video_media",
        ClaimRequest(worker_id=uuid4(), job_id=job_id, kinds=[kind]),
    )
    if claimed.claim is None:
        raise MediaFailure("invalid_response")
    return claimed.claim


async def _source_facts(storage, claim) -> tuple[ObjectIdentity, str]:
    source = claim.source
    if source is None or source.object_identity is None:
        raise MediaFailure("source_changed")
    with tempfile.TemporaryDirectory(prefix="anno-media-fixture-source-") as tmp:
        path = Path(tmp) / "source"
        await storage.download_source(source, path)
        return source.object_identity, _sha256_file(path)


async def _fail_claim(native, claim) -> None:
    try:
        await native.call(
            "fail_video_media",
            FailRequest(
                job_id=claim.job_id,
                generation=claim.generation,
                lease_token=claim.lease_token,
                error_code="seed_fixture_failed",
                error_detail="WebCodecs test fixture publication failed",
            ),
        )
    except Exception:
        # Preserve the triggering exception. Native lease fencing prevents an
        # obsolete failure report from changing a newer attempt.
        pass


async def _materialize_metadata(native, storage, dataset_item_id: UUID, meta: dict):
    accepted = await native.call(
        "schedule_video_media",
        ScheduleWorkRequest(
            operation_id=_operation_id(dataset_item_id, "metadata"),
            dataset_item_id=dataset_item_id,
            spec=MetadataSpec(),
        ),
    )
    claim = await _claim(native, accepted.work.job_id, "video_metadata")
    try:
        source_identity, source_sha256 = await _source_facts(storage, claim)
        entries = [FrameEntry.model_validate(row) for row in meta["frame_timetable"]]
        for offset in range(0, len(entries), BATCH_SIZE):
            await native.call(
                "stage_video_timetable",
                StageTimetableRequest(
                    job_id=claim.job_id,
                    generation=claim.generation,
                    lease_token=claim.lease_token,
                    rows=entries[offset : offset + BATCH_SIZE],
                ),
            )

        metadata_patch = {
            key: meta[key]
            for key in ("codec", "fps", "frame_count", "width", "height", "duration_ms")
            if key in meta
        }
        metadata_patch.setdefault("codec", "h264")
        result = MetadataResult(
            kind="video_metadata",
            source_identity=source_identity,
            source_sha256=source_sha256,
            metadata_patch=metadata_patch,
            width=meta["width"],
            height=meta["height"],
            timetable_total_frames=len(entries),
            timetable_staged_count=len(entries),
        )
        outcome = await native.call(
            "finish_video_media",
            FinishRequest(
                job_id=claim.job_id,
                generation=claim.generation,
                lease_token=claim.lease_token,
                result=result,
            ),
        )
        if outcome.outcome not in {"published", "replayed"}:
            raise MediaFailure("source_changed")
        return source_identity, source_sha256
    except Exception:
        await _fail_claim(native, claim)
        raise


def _chunk_fixtures(meta: dict, chunk_size_frames: int) -> list[dict]:
    chunks = meta.get("chunks")
    if chunks:
        return chunks
    return [
        {
            "chunk_id": 0,
            "generation_mode": "transcode",
            "start_frame": 0,
            "end_frame": int(meta["frame_count"]) - 1,
            "start_pts_ms": 0,
            "end_pts_ms": int(meta["duration_ms"]),
            "bytes": meta["mp4_bytes"],
            "samples": meta["samples"],
            "codec_string": meta["codec_string"],
            "description": meta["description"],
        }
    ]


async def _publish_chunk(
    native,
    storage,
    dataset_item_id: UUID,
    fixture_chunk: dict,
    *,
    chunk_size_frames: int,
    source_facts: tuple[ObjectIdentity, str] | None = None,
) -> str:
    chunk_id = int(fixture_chunk["chunk_id"])
    accepted = await native.call(
        "schedule_video_media",
        ScheduleWorkRequest(
            operation_id=_operation_id(dataset_item_id, f"chunk:{chunk_id}"),
            dataset_item_id=dataset_item_id,
            spec=ChunkSpec(
                chunk_id=chunk_id,
                chunk_size_frames=chunk_size_frames,
            ),
        ),
    )
    if accepted.work.status == "ready":
        return "ready"
    if accepted.work.status in {"failed", "canceled"}:
        return "failed"
    claimed = await native.call(
        "claim_video_media",
        ClaimRequest(worker_id=uuid4(), job_id=accepted.work.job_id, kinds=["chunk"]),
    )
    claim = claimed.claim
    if claim is None:
        return "pending"
    try:
        if source_facts is None:
            source_identity, source_sha256 = await _source_facts(storage, claim)
        else:
            source_identity, source_sha256 = source_facts
            if claim.source is None or claim.source.object_identity != source_identity:
                raise MediaFailure("source_changed")
        if claim.output_prefix is None:
            raise MediaFailure("invalid_response")
        with tempfile.TemporaryDirectory(prefix="anno-media-fixture-chunk-") as tmp:
            path = Path(tmp) / "chunk.mp4"
            path.write_bytes(fixture_chunk["bytes"])
            artifact: Artifact = await storage.upload_cache(
                path, claim.output_prefix + "chunk.mp4"
            )

        diagnostics = {
            "samples": fixture_chunk["samples"],
            "codec_string": fixture_chunk["codec_string"],
            "description": fixture_chunk["description"],
            "width": int(fixture_chunk.get("width", 0) or 0),
            "height": int(fixture_chunk.get("height", 0) or 0),
            "source_codec": "h264",
            "output_codec": "h264",
        }
        outcome = await native.call(
            "finish_video_media",
            FinishRequest(
                job_id=claim.job_id,
                generation=claim.generation,
                lease_token=claim.lease_token,
                result=ChunkResult(
                    source_identity=source_identity,
                    source_sha256=source_sha256,
                    artifact=artifact,
                    generation_mode=fixture_chunk["generation_mode"],
                    diagnostics=diagnostics,
                    start_pts_ms=fixture_chunk["start_pts_ms"],
                    end_pts_ms=fixture_chunk["end_pts_ms"],
                ),
            ),
        )
        if outcome.outcome not in {"published", "replayed"}:
            raise MediaFailure("source_changed")
        return "ready"
    except Exception:
        await _fail_claim(native, claim)
        raise


async def seed_video_fixture_media(
    db,
    *,
    dataset_item_id: UUID,
    meta: dict,
    chunk_size_frames: int,
    ready: bool,
) -> list[int]:
    """Stage a seeded source and optionally publish its deterministic chunks."""
    native = SqlAlchemyMediaDatabase(db, commit=False)
    storage = media_storage()
    source_facts = await _materialize_metadata(native, storage, dataset_item_id, meta)
    chunks = _chunk_fixtures(meta, chunk_size_frames)
    for fixture_chunk in chunks:
        fixture_chunk.setdefault("width", meta["width"])
        fixture_chunk.setdefault("height", meta["height"])
        chunk_id = int(fixture_chunk["chunk_id"])
        await native.call(
            "schedule_video_media",
            ScheduleWorkRequest(
                operation_id=_operation_id(dataset_item_id, f"chunk:{chunk_id}"),
                dataset_item_id=dataset_item_id,
                spec=ChunkSpec(
                    chunk_id=chunk_id,
                    chunk_size_frames=chunk_size_frames,
                ),
            ),
        )
    if ready:
        for fixture_chunk in chunks:
            await _publish_chunk(
                native,
                storage,
                dataset_item_id,
                fixture_chunk,
                chunk_size_frames=chunk_size_frames,
                source_facts=source_facts,
            )
    return [int(fixture_chunk["chunk_id"]) for fixture_chunk in chunks]


async def transition_video_fixture_chunk_ready(
    db,
    *,
    dataset_item_id: UUID,
    meta: dict,
    chunk_id: int,
    chunk_size_frames: int,
) -> str:
    native = SqlAlchemyMediaDatabase(db, commit=False)
    storage = media_storage()
    chunks = _chunk_fixtures(meta, chunk_size_frames)
    fixture_chunk = next(
        (chunk for chunk in chunks if int(chunk["chunk_id"]) == chunk_id), None
    )
    if fixture_chunk is None:
        raise KeyError("chunk_not_found")
    fixture_chunk.setdefault("width", meta["width"])
    fixture_chunk.setdefault("height", meta["height"])
    return await _publish_chunk(
        native,
        storage,
        dataset_item_id,
        fixture_chunk,
        chunk_size_frames=chunk_size_frames,
    )
