from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest

from aap_assets_media.contract import (
    AcceptedWork,
    Artifact,
    ChunkResult,
    ClaimRequest,
    ClaimedWork,
    FinishRequest,
    MediaClaim,
    MediaSource,
    MetadataResult,
    MetadataSpec,
    ObjectIdentity,
    StageResult,
    WorkOutcome,
    WorkReference,
    WorkTiming,
)
from app.services import assets_media_fixtures


@pytest.mark.asyncio
async def test_seed_fixture_media_uses_native_timetable_and_attempt_scoped_chunk(
    monkeypatch,
):
    item_id, dataset_id = uuid4(), uuid4()
    identity = ObjectIdentity(etag='"source-etag"', byte_size=15)
    source = MediaSource(
        dataset_item_id=item_id,
        dataset_id=dataset_id,
        source_generation=1,
        source_snapshot="c" * 64,
        timetable_revision=0,
        materialized_generation=0,
        object_identity=identity,
        source_drifted=False,
        file_name="fixture.mp4",
        file_path="e2e/fixture/source.mp4",
        file_type="video",
        file_size=15,
        content_hash=None,
        width=160,
        height=120,
        thumbnail_path=None,
        blurhash=None,
        video_metadata={},
    )
    metadata_job, chunk_job = uuid4(), uuid4()
    metadata_lease, chunk_lease = uuid4(), uuid4()
    staged_rows = []
    finished = []
    scheduled = []

    class FakeNative:
        async def call(self, operation, request):
            if operation == "schedule_video_media":
                scheduled.append(request)
                job_id = (
                    metadata_job
                    if isinstance(request.spec, MetadataSpec)
                    else chunk_job
                )
                kind = request.spec.kind
                return AcceptedWork(
                    operation_id=request.operation_id,
                    source=source,
                    work=WorkReference(
                        job_id=job_id,
                        generation=1,
                        dataset_item_id=item_id,
                        source_generation=1,
                        kind=kind,
                        status="pending",
                    ),
                    spec=request.spec,
                )
            if operation == "claim_video_media":
                assert isinstance(request, ClaimRequest)
                is_metadata = request.job_id == metadata_job
                return ClaimedWork(
                    claim=MediaClaim(
                        job_id=request.job_id,
                        generation=1,
                        lease_token=metadata_lease if is_metadata else chunk_lease,
                        lease_expires_at=datetime.now(UTC) + timedelta(minutes=10),
                        source=source,
                        spec=MetadataSpec() if is_metadata else scheduled[-1].spec,
                        output_prefix=(
                            f"media-assets/{request.job_id}/g1/a"
                            f"{metadata_lease if is_metadata else chunk_lease}/"
                        ),
                        timetable_revision=0,
                        timing=WorkTiming(),
                    )
                )
            if operation == "stage_video_timetable":
                staged_rows.extend(request.rows)
                return StageResult(
                    job_id=request.job_id,
                    generation=request.generation,
                    lease_token=request.lease_token,
                    staged_count=len(request.rows),
                )
            if operation == "finish_video_media":
                assert isinstance(request, FinishRequest)
                finished.append(request)
                return WorkOutcome(
                    job_id=request.job_id,
                    generation=request.generation,
                    outcome="published",
                )
            pytest.fail(f"unexpected native operation {operation}")

    class FakeStorage:
        async def download_source(self, observed_source, destination: Path):
            assert observed_source.object_identity == identity
            destination.write_bytes(b"original source")

        async def upload_cache(self, path: Path, key: str):
            return Artifact(
                key=key,
                byte_size=path.stat().st_size,
                sha256=sha256(path.read_bytes()).hexdigest(),
            )

    native = FakeNative()
    monkeypatch.setattr(
        assets_media_fixtures,
        "SqlAlchemyMediaDatabase",
        lambda _db, commit: native,
    )
    monkeypatch.setattr(assets_media_fixtures, "media_storage", FakeStorage)
    fixture_samples = [
        {
            "frame_index": 0,
            "pts_ms": 0,
            "duration_ms": 33,
            "is_keyframe": True,
            "size_bytes": 7,
            "offset_in_chunk": 0,
        }
    ]
    meta = {
        "frame_timetable": [
            {
                "frame_index": 0,
                "pts_ms": 0,
                "is_keyframe": True,
                "pict_type": "I",
                "byte_offset": 0,
            }
        ],
        "codec": "h264",
        "fps": 30,
        "frame_count": 1,
        "width": 160,
        "height": 120,
        "duration_ms": 33,
        "chunks": [
            {
                "chunk_id": 0,
                "generation_mode": "smart_copy",
                "start_frame": 0,
                "end_frame": 0,
                "start_pts_ms": 0,
                "end_pts_ms": 33,
                "bytes": b"fixture chunk",
                "samples": fixture_samples,
                "codec_string": "avc1.64000a",
                "description": "fixture-avcc",
            }
        ],
    }

    chunk_ids = await assets_media_fixtures.seed_video_fixture_media(
        object(),
        dataset_item_id=item_id,
        meta=meta,
        chunk_size_frames=1,
        ready=True,
    )

    assert chunk_ids == [0]
    assert [row.frame_index for row in staged_rows] == [0]
    metadata_result = finished[0].result
    assert isinstance(metadata_result, MetadataResult)
    assert metadata_result.timetable_total_frames == 1
    assert metadata_result.timetable_staged_count == 1
    assert metadata_result.metadata_patch["codec"] == "h264"
    chunk_result = finished[1].result
    assert isinstance(chunk_result, ChunkResult)
    assert chunk_result.source_sha256 == sha256(b"original source").hexdigest()
    assert chunk_result.generation_mode == "smart_copy"
    assert chunk_result.diagnostics["samples"] == fixture_samples
    assert chunk_result.artifact.key == (
        f"media-assets/{chunk_job}/g1/a{chunk_lease}/chunk.mp4"
    )
    assert scheduled[1].spec.chunk_size_frames == 1
