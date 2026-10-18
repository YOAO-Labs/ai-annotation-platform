from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import stat
import subprocess
import threading
from uuid import uuid4

import pytest

from aap_assets_media.contract import (
    Artifact,
    ChunkSpec,
    ClaimGarbageRequest,
    ClaimedWork,
    CompleteGarbageRequest,
    ExpireResult,
    FrameResult,
    FrameSpec,
    GarbageAck,
    GarbageBatch,
    GarbageItem,
    FrameEntry,
    MediaClaim,
    MediaFailure,
    MetadataResult,
    MediaSource,
    MediaUsage,
    MetadataSpec,
    ObjectIdentity,
    StageResult,
    TimetableSpec,
    WorkOutcome,
    WorkTiming,
)
from aap_assets_media_runtime.config import MediaExecutionSettings
from aap_assets_media_runtime.processing import (
    ProcessedArtifact,
    ProcessedWork,
    derive_anchor_frames,
    process_claim,
    probe_source_timetable,
    resolve_pts_ms_sparse,
    select_sparse_anchor_rows,
)
from aap_assets_media_runtime.worker import (
    MediaExecutor,
    VerifiedSourceCache,
)


class _Storage:
    def __init__(self, body: bytes = b"video-bytes"):
        self.body = body
        self.identity = ObjectIdentity(
            etag="etag-v1", byte_size=len(body), version_id="version-1"
        )
        self.downloads = 0
        self.heads = 0
        self.uploads: list[Artifact] = []
        self.deleted: list[tuple[str, object]] = []

    async def head_source(self, key: str) -> ObjectIdentity:
        self.heads += 1
        return self.identity

    async def download_source(self, source: MediaSource, destination: Path) -> None:
        assert source.object_identity == self.identity
        self.downloads += 1
        destination.write_bytes(self.body)

    async def upload_cache(self, path: Path, key: str) -> Artifact:
        body = path.read_bytes()
        artifact = Artifact(
            key=key, byte_size=len(body), sha256=hashlib.sha256(body).hexdigest()
        )
        self.uploads.append(artifact)
        return artifact

    async def delete_cache_keys(self, keys: list[str]) -> int:
        self.deleted.append(("keys", list(keys)))
        return len(keys)

    async def delete_cache_prefix(self, prefix: str) -> int:
        self.deleted.append(("prefix", prefix))
        return 2


def _source(storage: _Storage, *, generation: int = 1, content_hash=None):
    return MediaSource(
        dataset_item_id=uuid4(),
        dataset_id=uuid4(),
        source_generation=generation,
        source_snapshot=f"{generation:064x}",
        timetable_revision=0,
        materialized_generation=0,
        object_identity=storage.identity,
        source_drifted=False,
        file_name="clip.mp4",
        file_path="dataset/clip.mp4",
        file_type="video",
        file_size=len(storage.body),
        content_hash=content_hash,
        width=32,
        height=24,
        thumbnail_path=None,
        blurhash=None,
        video_metadata={"fps": 5.0, "codec": "h264"},
    )


@pytest.mark.asyncio
async def test_verified_source_cache_reuses_one_download_and_rechecks_identity(
    tmp_path, monkeypatch
):
    import aap_assets_media_runtime.worker as worker

    storage = _Storage()
    cache = VerifiedSourceCache(directory=tmp_path)
    md5 = hashlib.md5(storage.body, usedforsecurity=False).hexdigest()
    source = _source(storage, content_hash=md5)
    digest_calls = []
    digest = worker._source_digests

    def count_digest(path, *, need_md5=False):
        digest_calls.append((threading.get_ident(), need_md5))
        return digest(path, need_md5=need_md5)

    monkeypatch.setattr(worker, "_source_digests", count_digest)

    main_thread = threading.get_ident()
    verified = None
    for _ in range(12):
        async with cache.lease(source, storage) as lease:
            verified = lease
            assert lease.path.read_bytes() == storage.body
            assert lease.path.stat().st_mode & stat.S_IWUSR == 0
    assert verified is not None
    await cache.verify_current(verified, storage)

    assert len(digest_calls) == 1
    assert digest_calls[0][0] != main_thread
    assert digest_calls[0][1] is True
    assert storage.downloads == 1
    assert storage.heads == 14
    await cache.close()
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_verified_source_cache_detects_cached_file_mutation(tmp_path):
    storage = _Storage()
    cache = VerifiedSourceCache(directory=tmp_path)
    source = _source(storage)

    async with cache.lease(source, storage) as verified:
        verified.path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        verified.path.write_bytes(b"tampered!!!")

    with pytest.raises(MediaFailure, match="source_changed"):
        async with cache.lease(source, storage):
            pass
    assert not list(tmp_path.iterdir())
    await cache.close()


@pytest.mark.asyncio
async def test_verified_source_cache_validates_declared_md5_and_sha256(tmp_path):
    storage = _Storage()
    cache = VerifiedSourceCache(directory=tmp_path)
    md5 = hashlib.md5(storage.body, usedforsecurity=False).hexdigest()
    sha256 = hashlib.sha256(storage.body).hexdigest()

    async with cache.lease(_source(storage, content_hash=md5), storage):
        pass
    async with cache.lease(
        _source(storage, generation=2, content_hash=sha256), storage
    ):
        pass
    await cache.close()

    bad = VerifiedSourceCache(directory=tmp_path)
    with pytest.raises(MediaFailure, match="source_changed"):
        async with bad.lease(_source(storage, content_hash="0" * 64), storage):
            pass
    await bad.close()


@pytest.mark.asyncio
async def test_verified_source_cache_rejects_identity_drift_after_download(tmp_path):
    class _ChangesAfterRead(_Storage):
        async def download_source(self, source: MediaSource, destination: Path) -> None:
            await super().download_source(source, destination)
            self.identity = ObjectIdentity(
                etag="etag-v2", byte_size=len(self.body), version_id="version-2"
            )

    storage = _ChangesAfterRead()
    source = _source(storage)
    cache = VerifiedSourceCache(directory=tmp_path)

    with pytest.raises(MediaFailure, match="source_changed"):
        async with cache.lease(source, storage):
            pass

    assert storage.downloads == 1
    assert not list(tmp_path.iterdir())
    await cache.close()


class _Database:
    def __init__(self, claims=None, *, garbage=None):
        self.claims = list(claims or [])
        self.garbage = garbage or GarbageBatch(items=[])
        self.calls: list[tuple[str, object]] = []
        self.expire_results = [
            ExpireResult(frames_expired=2, chunks_expired=1, more=True),
            ExpireResult(frames_expired=0, chunks_expired=0, more=False),
        ]

    async def call(self, name: str, request):
        self.calls.append((name, request))
        if name == "claim_video_media":
            return ClaimedWork(claim=self.claims.pop(0) if self.claims else None)
        if name == "finish_video_media":
            return WorkOutcome(
                job_id=request.job_id,
                generation=request.generation,
                outcome="published",
            )
        if name == "stage_video_timetable":
            return StageResult(
                job_id=request.job_id,
                generation=request.generation,
                lease_token=request.lease_token,
                staged_count=len(request.rows),
            )
        if name == "expire_video_media":
            return self.expire_results.pop(0)
        if name == "claim_video_media_garbage":
            assert isinstance(request, ClaimGarbageRequest)
            return self.garbage
        if name == "complete_video_media_garbage":
            assert isinstance(request, CompleteGarbageRequest)
            return GarbageAck(garbage_id=request.garbage_id, completed=True)
        if name == "read_video_media_usage":
            return MediaUsage(
                frame_bytes=123,
                chunk_bytes=456,
                pending=1,
                running=2,
                failed=3,
            )
        raise AssertionError(f"unexpected database call {name}")


def _claim(source: MediaSource, *, generation: int, token=None):
    job_id = uuid4()
    lease_token = token or uuid4()
    prefix = f"media-assets/{job_id}/g{generation}/a{lease_token}/"
    return MediaClaim(
        job_id=job_id,
        generation=generation,
        lease_token=lease_token,
        lease_expires_at="2026-10-19T00:00:00Z",
        source=source,
        spec=FrameSpec(frame_index=0, width=32, format="webp"),
        output_prefix=prefix,
        timetable_revision=0,
        timing=WorkTiming(anchors=[]),
    )


def _h264_fixture(tmp_path: Path) -> Path:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe are unavailable")
    output = tmp_path / "clip.mp4"
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=64x48:r=5",
            "-frames:v",
            "10",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-g",
            "3",
            "-keyint_min",
            "3",
            "-sc_threshold",
            "0",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(output),
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        pytest.skip("ffmpeg does not provide libx264")
    return output


@pytest.mark.asyncio
async def test_executor_reuses_verified_source_across_frame_jobs(monkeypatch):
    storage = _Storage()
    source = _source(storage)
    claims = [_claim(source, generation=1), _claim(source, generation=2)]
    database = _Database(claims)

    selected_probe = object()
    observed_probe_clients = []

    def fake_process(
        claim, source_path, output_dir, identity, source_sha256, *, probe_client=None
    ):
        observed_probe_clients.append(probe_client)
        body = b"frame image"
        output = output_dir / "frame.webp"
        output.write_bytes(body)
        artifact = Artifact(
            key=claim.output_prefix + "frame.webp",
            byte_size=len(body),
            sha256=hashlib.sha256(body).hexdigest(),
        )
        return ProcessedWork(
            FrameResult(
                source_identity=identity, source_sha256=source_sha256, artifact=artifact
            ),
            (ProcessedArtifact(artifact, output),),
        )

    monkeypatch.setattr("aap_assets_media_runtime.worker.process_claim", fake_process)
    executor = MediaExecutor(
        database,
        storage,
        settings=MediaExecutionSettings(lease_seconds=30),
        probe_client=selected_probe,
    )

    first = await executor.execute_once()
    second = await executor.execute_once()

    assert first is not None and first.outcome == "published"
    assert second is not None and second.outcome == "published"
    assert storage.downloads == 1
    assert len(storage.uploads) == 2
    assert observed_probe_clients == [selected_probe, selected_probe]
    assert [name for name, _ in database.calls].count("finish_video_media") == 2
    await executor.close()


@pytest.mark.asyncio
async def test_executor_stages_timetable_in_bounded_pages(monkeypatch):
    import aap_assets_media_runtime.worker as worker

    storage = _Storage()
    source = _source(storage)
    claim = _claim(source, generation=1).model_copy(
        update={"spec": TimetableSpec(sparse_stride=1)}
    )
    database = _Database([claim])
    rows = tuple(
        FrameEntry(frame_index=index, pts_ms=index * 33, is_keyframe=index % 30 == 0)
        for index in range(1001)
    )
    source_digest = hashlib.sha256(storage.body).hexdigest()

    monkeypatch.setattr(
        worker,
        "process_claim",
        lambda *_args, **_kwargs: ProcessedWork(
            MetadataResult(
                kind="video_timetable",
                source_identity=storage.identity,
                source_sha256=source_digest,
                metadata_patch={"frame_timetable_frame_count": len(rows)},
                clear_errors=["frame_timetable_error"],
                timetable_total_frames=len(rows),
                timetable_staged_count=len(rows),
            ),
            timetable_rows=rows,
        ),
    )
    executor = MediaExecutor(
        database,
        storage,
        settings=MediaExecutionSettings(lease_seconds=30),
    )

    result = await executor.execute_once()

    assert result is not None and result.outcome == "published"
    stages = [
        request for name, request in database.calls if name == "stage_video_timetable"
    ]
    assert [len(request.rows) for request in stages] == [500, 500, 1]
    await executor.close()


@pytest.mark.asyncio
async def test_maintenance_expires_claims_scoped_cleanup_and_returns_usage():
    storage = _Storage()
    prefix = f"media-assets/{uuid4()}/g1/a{uuid4()}/"
    key = f"media-assets/{uuid4()}/g1/a{uuid4()}/poster.webp"
    garbage = GarbageBatch(
        items=[
            GarbageItem(
                garbage_id=uuid4(), lease_token=uuid4(), prefix=prefix, keys=[]
            ),
            GarbageItem(
                garbage_id=uuid4(), lease_token=uuid4(), prefix=None, keys=[key]
            ),
        ]
    )
    database = _Database(garbage=garbage)
    executor = MediaExecutor(database, storage)

    usage = await executor.maintenance_once()

    assert usage.frame_bytes == 123 and usage.pending == 1
    assert storage.deleted == [("prefix", prefix), ("keys", [key])]
    names = [name for name, _ in database.calls]
    assert names.count("expire_video_media") == 2
    assert (
        names.index("claim_video_media_garbage")
        < names.index("complete_video_media_garbage")
        < names.index("read_video_media_usage")
    )
    await executor.close()


@pytest.mark.asyncio
async def test_real_probe_verifies_source_and_returns_sparse_source_frame_numbers(
    tmp_path,
):
    clip = _h264_fixture(tmp_path).read_bytes()
    storage = _Storage(clip)
    source = _source(
        storage,
        content_hash=hashlib.sha256(clip).hexdigest(),
    )

    rows = await probe_source_timetable(source, storage, sparse_stride=3)

    assert [row.frame_index for row in rows] == [0, 3, 6, 9]
    assert all(row.is_keyframe for row in rows)
    assert storage.downloads == 1
    assert storage.heads == 2


@pytest.mark.asyncio
async def test_source_timetable_uses_selected_probe_client(tmp_path):
    storage = _Storage()
    source = _source(
        storage,
        content_hash=hashlib.sha256(storage.body).hexdigest(),
    )

    class _Probe:
        calls = 0

        def probe_video_frame_timetable(self, path):
            self.calls += 1
            assert Path(path).read_bytes() == storage.body
            return [
                {"frame_index": 0, "pts_ms": 0, "is_keyframe": True},
                {"frame_index": 1, "pts_ms": 200, "is_keyframe": False},
                {"frame_index": 2, "pts_ms": 400, "is_keyframe": True},
            ]

    probe = _Probe()
    rows = await probe_source_timetable(
        source, storage, sparse_stride=2, probe_client=probe
    )

    assert [row.frame_index for row in rows] == [0, 2]
    assert probe.calls == 1
    assert storage.downloads == 1
    assert storage.heads == 2


def test_real_chunk_path_smartcopies_keyframe_aligned_h264(tmp_path):
    clip_path = _h264_fixture(tmp_path)
    from aap_media_probe.probe import probe_video_file, probe_video_frame_timetable

    metadata = probe_video_file(clip_path)
    rows = [
        FrameEntry.model_validate(row) for row in probe_video_frame_timetable(clip_path)
    ]
    storage = _Storage(clip_path.read_bytes())
    source = _source(storage).model_copy(update={"video_metadata": metadata})
    job_id = uuid4()
    lease_token = uuid4()
    claim = MediaClaim(
        job_id=job_id,
        generation=1,
        lease_token=lease_token,
        lease_expires_at="2026-10-19T00:00:00Z",
        source=source,
        spec=ChunkSpec(chunk_id=0, chunk_size_frames=3),
        output_prefix=f"media-assets/{job_id}/g1/a{lease_token}/",
        timetable_revision=0,
        timing=WorkTiming(
            start_frame=0,
            end_frame=2,
            anchors=[rows[0], rows[2]],
            is_keyframe=rows[0].is_keyframe,
        ),
    )
    output = tmp_path / "outputs"

    processed = process_claim(
        claim,
        clip_path,
        output,
        storage.identity,
        hashlib.sha256(clip_path.read_bytes()).hexdigest(),
    )

    assert processed.result.kind == "chunk"
    assert processed.result.generation_mode == "smart_copy"
    assert processed.result.diagnostics["source_codec"] == "h264"
    assert processed.result.diagnostics["output_codec"] == "h264"
    assert processed.artifacts[0].path.is_file()

    frame_job = uuid4()
    frame_token = uuid4()
    frame_claim = MediaClaim(
        job_id=frame_job,
        generation=1,
        lease_token=frame_token,
        lease_expires_at="2026-10-19T00:00:00Z",
        source=source,
        spec=FrameSpec(frame_index=2, width=32, format="jpeg"),
        output_prefix=f"media-assets/{frame_job}/g1/a{frame_token}/",
        timetable_revision=0,
        timing=WorkTiming(anchors=[rows[0], rows[2]]),
    )
    frame_result = process_claim(
        frame_claim,
        clip_path,
        tmp_path / "frame-output",
        storage.identity,
        hashlib.sha256(clip_path.read_bytes()).hexdigest(),
    )
    assert frame_result.result.kind == "frame"
    assert frame_result.artifacts[0].path.suffix == ".jpeg"
    assert frame_result.artifacts[0].path.is_file()


@pytest.mark.parametrize("vfr", [False, True])
def test_frame_pixels_match_source_ordinals_with_sparse_timing(tmp_path, vfr):
    from PIL import Image
    from aap_media_probe.probe import probe_video_file, probe_video_frame_timetable

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe are unavailable")
    for index in range(12):
        Image.new("RGB", (48, 32), (index * 17, 240 - index * 19, index * 11)).save(
            tmp_path / f"source-{index:02d}.png"
        )
    clip = tmp_path / "source.mp4"
    command = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-framerate",
        "30",
        "-i",
        str(tmp_path / "source-%02d.png"),
    ]
    if vfr:
        command += ["-vf", "setpts=PTS+if(gte(N\\,3)\\,0.1/TB\\,0)"]
    command += [
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-crf",
        "0",
        "-g",
        "4",
        "-fps_mode",
        "vfr",
        str(clip),
    ]
    subprocess.run(command, check=True, capture_output=True, timeout=30)
    reference = subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-i",
            str(clip),
            "-map",
            "0:v:0",
            "-fps_mode",
            "passthrough",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    ).stdout
    frame_size = 48 * 32 * 3
    assert len(reference) == 12 * frame_size
    rows = [FrameEntry.model_validate(row) for row in probe_video_frame_timetable(clip)]
    anchors = [rows[index] for index in (0, 4, 8, 11)]
    storage = _Storage(clip.read_bytes())
    source = _source(storage).model_copy(
        update={"video_metadata": probe_video_file(clip)}
    )
    for index in (1, 2, 5):
        job, lease = uuid4(), uuid4()
        claim = MediaClaim(
            job_id=job,
            generation=1,
            lease_token=lease,
            lease_expires_at="2026-10-19T00:00:00Z",
            source=source,
            spec=FrameSpec(frame_index=index, width=48, format="jpeg"),
            output_prefix=f"media-assets/{job}/g1/a{lease}/",
            timetable_revision=0,
            timing=WorkTiming(anchors=anchors),
        )
        work = process_claim(
            claim,
            clip,
            tmp_path / f"result-{index}",
            storage.identity,
            hashlib.sha256(clip.read_bytes()).hexdigest(),
        )
        expected = Image.frombytes(
            "RGB", (48, 32), reference[index * frame_size : (index + 1) * frame_size]
        ).getpixel((24, 16))
        with Image.open(work.artifacts[0].path) as image:
            actual = image.convert("RGB").getpixel((24, 16))
        assert max(abs(a - b) for a, b in zip(actual, expected)) <= 5, (
            index,
            actual,
            expected,
        )

    job, lease = uuid4(), uuid4()
    chunk_claim = MediaClaim(
        job_id=job,
        generation=1,
        lease_token=lease,
        lease_expires_at="2026-10-19T00:00:00Z",
        source=source,
        spec=ChunkSpec(chunk_id=1, chunk_size_frames=2),
        output_prefix=f"media-assets/{job}/g1/a{lease}/",
        timetable_revision=0,
        timing=WorkTiming(
            start_frame=2, end_frame=3, anchors=anchors, is_keyframe=False
        ),
    )
    chunk = process_claim(
        chunk_claim,
        clip,
        tmp_path / "chunk",
        storage.identity,
        hashlib.sha256(clip.read_bytes()).hexdigest(),
    )
    decoded = subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-i",
            str(chunk.artifacts[0].path),
            "-map",
            "0:v:0",
            "-fps_mode",
            "passthrough",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    ).stdout
    assert len(decoded) == 2 * frame_size
    for offset, index in enumerate((2, 3)):
        expected = Image.frombytes(
            "RGB", (48, 32), reference[index * frame_size : (index + 1) * frame_size]
        ).getpixel((24, 16))
        actual = Image.frombytes(
            "RGB", (48, 32), decoded[offset * frame_size : (offset + 1) * frame_size]
        ).getpixel((24, 16))
        assert max(abs(a - b) for a, b in zip(actual, expected)) <= 5, (
            index,
            actual,
            expected,
        )
    assert [
        sample["frame_index"] for sample in chunk.result.diagnostics["samples"]
    ] == [2, 3]


def test_processing_uses_selected_probe_client_for_all_published_probe_operations(
    monkeypatch, tmp_path
):
    import aap_assets_media_runtime.processing as processing

    storage = _Storage()
    source = _source(storage)
    rows = [
        {"frame_index": index, "pts_ms": index * 200, "is_keyframe": index == 0}
        for index in range(3)
    ]
    samples = [
        {
            "frame_index": 0,
            "pts_ms": 0,
            "duration_ms": 200,
            "is_keyframe": True,
            "size_bytes": 12,
            "offset_in_chunk": 40,
        }
    ]

    class _Probe:
        def __init__(self):
            self.calls = []

        def probe_video_file(self, _path):
            self.calls.append("metadata")
            return {
                "codec": "h264",
                "fps": 5.0,
                "frame_count": 3,
                "width": 64,
                "height": 48,
                "duration_ms": 600,
            }

        def probe_video_frame_timetable(self, _path):
            self.calls.append("frames")
            return rows

        def probe_chunk_samples(self, _path, start_frame):
            self.calls.append("packets")
            assert start_frame == 0
            return samples

    probe = _Probe()
    monkeypatch.setattr(
        processing,
        "probe_video_file",
        lambda *_args: pytest.fail("remote selection must not invoke local ffprobe"),
    )
    monkeypatch.setattr(
        processing,
        "probe_video_frame_timetable",
        lambda *_args: pytest.fail("remote selection must not invoke local ffprobe"),
    )
    monkeypatch.setattr(
        processing,
        "probe_chunk_samples",
        lambda *_args: pytest.fail("remote selection must not invoke local ffprobe"),
    )

    metadata_job = uuid4()
    metadata_token = uuid4()
    metadata_claim = MediaClaim(
        job_id=metadata_job,
        generation=1,
        lease_token=metadata_token,
        lease_expires_at="2026-10-19T00:00:00Z",
        source=source,
        spec=MetadataSpec(
            generate_playback=False, generate_poster=False, probe_timetable=True
        ),
        output_prefix=f"media-assets/{metadata_job}/g1/a{metadata_token}/",
        timetable_revision=0,
        timing=WorkTiming(),
    )
    metadata_result = process_claim(
        metadata_claim,
        tmp_path / "source.mp4",
        tmp_path / "metadata-output",
        storage.identity,
        hashlib.sha256(storage.body).hexdigest(),
        probe_client=probe,
    )
    assert metadata_result.result.kind == "video_metadata"
    assert len(metadata_result.timetable_rows) == 3

    chunk_job = uuid4()
    chunk_token = uuid4()
    chunk_claim = MediaClaim(
        job_id=chunk_job,
        generation=1,
        lease_token=chunk_token,
        lease_expires_at="2026-10-19T00:00:00Z",
        source=source.model_copy(
            update={
                "video_metadata": {
                    "codec": "h264",
                    "fps": 5.0,
                    "width": 64,
                    "height": 48,
                }
            }
        ),
        spec=ChunkSpec(chunk_id=0, chunk_size_frames=3),
        output_prefix=f"media-assets/{chunk_job}/g1/a{chunk_token}/",
        timetable_revision=0,
        timing=WorkTiming(
            start_frame=0,
            end_frame=2,
            anchors=[
                FrameEntry.model_validate(rows[0]),
                FrameEntry.model_validate(rows[2]),
            ],
            is_keyframe=True,
        ),
    )
    monkeypatch.setattr(
        processing,
        "extract_video_chunk_smart_copy",
        lambda _source, output, _start, _duration: Path(output).write_bytes(b"chunk"),
    )
    chunk_result = process_claim(
        chunk_claim,
        tmp_path / "source.mp4",
        tmp_path / "chunk-output",
        storage.identity,
        hashlib.sha256(storage.body).hexdigest(),
        probe_client=probe,
    )

    assert chunk_result.result.kind == "chunk"
    assert chunk_result.result.diagnostics["samples"] == samples
    assert probe.calls == ["metadata", "frames", "packets"]


def test_sparse_timetable_helpers_keep_source_indices_and_interpolate_pts():
    rows = [
        {"frame_index": 0, "pts_ms": 0, "is_keyframe": True},
        {"frame_index": 1, "pts_ms": 120, "is_keyframe": False},
        {"frame_index": 2, "pts_ms": 400, "is_keyframe": False},
        {"frame_index": 3, "pts_ms": 600, "is_keyframe": True},
        {"frame_index": 4, "pts_ms": 800, "is_keyframe": False},
    ]

    sparse = select_sparse_anchor_rows(rows, 4)
    anchors = [FrameEntry.model_validate(row) for row in sparse]

    assert derive_anchor_frames(10, 3) == [0, 3, 6, 9]
    assert [row["frame_index"] for row in sparse] == [0, 3, 4]
    assert resolve_pts_ms_sparse(2, anchors, None, 4) == 400
    assert resolve_pts_ms_sparse(2, [anchors[0], anchors[-1]], None, 4) == 400


def test_metadata_transcodes_hevc_source_even_when_browser_codec_is_h264(
    monkeypatch, tmp_path
):
    import aap_assets_media_runtime.processing as processing

    storage = _Storage()
    source = _source(storage).model_copy(
        update={
            "video_metadata": {
                "codec": "hevc",
                "playback_path": "old/playback.mp4",
                "playback_codec": "h264",
            }
        }
    )
    job_id = uuid4()
    lease_token = uuid4()
    claim = MediaClaim(
        job_id=job_id,
        generation=1,
        lease_token=lease_token,
        lease_expires_at="2026-10-19T00:00:00Z",
        source=source,
        spec=MetadataSpec(
            generate_playback=True, generate_poster=False, probe_timetable=False
        ),
        output_prefix=f"media-assets/{job_id}/g1/a{lease_token}/",
        timetable_revision=0,
        timing=WorkTiming(),
    )
    calls = []

    monkeypatch.setattr(processing, "probe_video_file", lambda _path: {"codec": "hevc"})

    def write_h264(_source, output):
        calls.append("transcoded")
        Path(output).write_bytes(b"h264 playback")

    monkeypatch.setattr(processing, "transcode_video_for_browser", write_h264)
    processed = process_claim(
        claim,
        tmp_path / "source.mp4",
        tmp_path / "out",
        storage.identity,
        hashlib.sha256(storage.body).hexdigest(),
    )

    assert calls == ["transcoded"]
    assert processed.result.metadata_patch["playback_codec"] == "h264"
    assert processed.result.playback is not None


def test_chunk_uses_original_unsupported_codec_not_h264_playback(monkeypatch, tmp_path):
    import aap_assets_media_runtime.processing as processing

    storage = _Storage()
    source = _source(storage).model_copy(
        update={
            "video_metadata": {
                "codec": "vp9",
                "playback_codec": "h264",
                "fps": 5.0,
                "width": 64,
                "height": 48,
            }
        }
    )
    job_id = uuid4()
    lease_token = uuid4()
    claim = MediaClaim(
        job_id=job_id,
        generation=1,
        lease_token=lease_token,
        lease_expires_at="2026-10-19T00:00:00Z",
        source=source,
        spec=ChunkSpec(chunk_id=0, chunk_size_frames=3),
        output_prefix=f"media-assets/{job_id}/g1/a{lease_token}/",
        timetable_revision=0,
        timing=WorkTiming(
            start_frame=0,
            end_frame=2,
            anchors=[
                FrameEntry(frame_index=0, pts_ms=0, is_keyframe=True),
                FrameEntry(frame_index=2, pts_ms=400, is_keyframe=False),
            ],
            is_keyframe=True,
        ),
    )
    calls = []

    def transcode(_source_path, output_path, start_ms, frame_count):
        calls.append((start_ms, frame_count))
        Path(output_path).write_bytes(b"transcoded chunk")

    def smart_copy(*_args):
        raise AssertionError("VP9 source must not use a smart-copy path")

    monkeypatch.setattr(processing, "extract_video_chunk", transcode)
    monkeypatch.setattr(processing, "extract_video_chunk_smart_copy", smart_copy)
    monkeypatch.setattr(processing, "probe_chunk_samples", lambda *_args: [])

    processed = process_claim(
        claim,
        tmp_path / "source.mp4",
        tmp_path / "out",
        storage.identity,
        hashlib.sha256(storage.body).hexdigest(),
    )

    assert calls == [(0, 3)]
    assert processed.result.generation_mode == "transcode"
    assert processed.result.diagnostics["source_codec"] == "vp9"
    assert processed.result.diagnostics["output_codec"] == "h264"
    assert processed.result.diagnostics["fallback_reason"] == "unsupported_codec"
