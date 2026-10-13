import json
import subprocess
import uuid

import pytest
from pydantic import TypeAdapter
from sqlalchemy import select

from app.db.models.annotation import Annotation
from app.db.models.dataset import (
    Dataset,
    DatasetItem,
    VideoChunk,
    VideoFrameCache,
    VideoFrameIndex,
)
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.schemas._jsonb_types import Geometry
from app.schemas.task import VideoMetadata
from app.services.exporting.service import ExportService, UnsupportedExportError
from app.workers.media import (
    FFMPEG_CHUNK_TIMEOUT_SECONDS,
    FFMPEG_POSTER_TIMEOUT_SECONDS,
    FFMPEG_TRANSCODE_TIMEOUT_SECONDS,
    FFPROBE_TIMEOUT_SECONDS,
    _store_frame_cache_image,
    _store_video_chunk,
    extract_video_chunk_smart_copy,
    extract_video_poster,
    parse_ffprobe_video_metadata,
    parse_ffprobe_frame_timetable,
    probe_video_file,
    probe_video_frame_timetable,
    transcode_video_for_browser,
)
from tests.factory import create_membership, build_tool_bindings


def test_parse_ffprobe_video_metadata_computes_fps_and_frame_count():
    meta = parse_ffprobe_video_metadata(
        {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1280,
                    "height": 720,
                    "avg_frame_rate": "30000/1001",
                    "nb_frames": "N/A",
                    "duration": "2.002",
                }
            ],
            "format": {"duration": "2.002"},
        }
    )

    assert meta == {
        "duration_ms": 2002,
        "fps": 29.97,
        "frame_count": 60,
        "width": 1280,
        "height": 720,
        "codec": "h264",
    }


def test_parse_ffprobe_frame_timetable_extracts_frame_pts():
    frames = parse_ffprobe_frame_timetable(
        {
            "frames": [
                {
                    "media_type": "video",
                    "key_frame": 1,
                    "best_effort_timestamp_time": "0.000000",
                    "pict_type": "I",
                    "pkt_pos": "48",
                },
                {
                    "media_type": "video",
                    "key_frame": 0,
                    "pkt_pts_time": "0.033367",
                    "pict_type": "P",
                    "pkt_pos": "4096",
                },
                {
                    "media_type": "audio",
                    "best_effort_timestamp_time": "0.040000",
                },
            ]
        }
    )

    assert frames == [
        {
            "frame_index": 0,
            "pts_ms": 0,
            "is_keyframe": True,
            "pict_type": "I",
            "byte_offset": 48,
        },
        {
            "frame_index": 1,
            "pts_ms": 33,
            "is_keyframe": False,
            "pict_type": "P",
            "byte_offset": 4096,
        },
    ]


def test_probe_video_file_uses_timeout(tmp_path, monkeypatch):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake")
    seen: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        seen["args"] = args[0]
        seen["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout='{"streams":[{"codec_type":"video","width":1,"height":1}]}',
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    probe_video_file(video)

    assert seen["timeout"] == FFPROBE_TIMEOUT_SECONDS
    assert any("stream=codec_type,codec_name" in arg for arg in seen["args"])


def test_probe_video_frame_timetable_uses_timeout(tmp_path, monkeypatch):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake")
    seen: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        seen["args"] = args[0]
        seen["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout='{"frames":[{"media_type":"video","best_effort_timestamp_time":"0","key_frame":1}]}',
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    frames = probe_video_frame_timetable(video)

    assert frames[0]["pts_ms"] == 0
    assert seen["timeout"] == FFPROBE_TIMEOUT_SECONDS
    assert "-show_frames" in seen["args"]


def test_extract_video_poster_uses_timeout(tmp_path, monkeypatch):
    video = tmp_path / "clip.mp4"
    poster = tmp_path / "poster.webp"
    video.write_bytes(b"fake")
    seen: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    extract_video_poster(video, poster)

    assert seen["timeout"] == FFMPEG_POSTER_TIMEOUT_SECONDS


async def test_store_frame_cache_image_uses_shared_video_frame_cache(
    db_session, tmp_path, super_admin, monkeypatch
):
    user, _ = super_admin
    dataset = Dataset(
        display_id=f"D-VID-{uuid.uuid4().hex[:6]}",
        name="videos",
        data_type="video",
        created_by=user.id,
    )
    db_session.add(dataset)
    await db_session.flush()
    item = DatasetItem(
        dataset_id=dataset.id,
        file_name="clip.mp4",
        file_path="videos/clip.mp4",
        file_type="video",
        metadata_={"video": {"fps": 30, "frame_count": 10}},
    )
    db_session.add(item)
    await db_session.flush()
    row = VideoFrameCache(
        dataset_item_id=item.id,
        frame_index=0,
        width=512,
        format="webp",
        status="pending",
    )
    db_session.add(row)
    await db_session.flush()
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"fake")

    class FakeClient:
        def __init__(self):
            self.puts = []

        def put_object(self, **kwargs):
            self.puts.append(kwargs)

    class FakeStorage:
        datasets_bucket = "datasets"
        media_cache_bucket = "media-cache"

        def __init__(self):
            self.client = FakeClient()

        def ensure_bucket(self, bucket):
            assert bucket == "media-cache"

    def fake_extract(input_path, output_path, pts_ms, width):
        assert input_path == source
        assert pts_ms == 0
        assert width == 512
        output_path.write_bytes(b"poster")

    storage = FakeStorage()
    monkeypatch.setattr("app.workers.media.extract_video_frame_image", fake_extract)

    await _store_frame_cache_image(
        db_session,
        storage,
        item,
        VideoMetadata.model_validate({"fps": 30, "frame_count": 10}),
        source,
        tmp_path,
        row,
    )

    assert row.status == "ready"
    assert row.storage_key == f"videos/{item.id}/frames/0_512.webp"
    assert storage.client.puts[0]["Key"] == row.storage_key


def test_transcode_video_for_browser_uses_timeout(tmp_path, monkeypatch):
    video = tmp_path / "clip.mp4"
    playback = tmp_path / "playback.mp4"
    video.write_bytes(b"fake")
    seen: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        seen["args"] = args[0]
        seen["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    transcode_video_for_browser(video, playback)

    assert seen["timeout"] == FFMPEG_TRANSCODE_TIMEOUT_SECONDS
    assert "libx264" in seen["args"]


def test_extract_video_chunk_smart_copy_uses_copy_and_timeout(tmp_path, monkeypatch):
    video = tmp_path / "clip.mp4"
    chunk = tmp_path / "chunk.mp4"
    video.write_bytes(b"fake")
    seen: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        seen["args"] = args[0]
        seen["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    extract_video_chunk_smart_copy(video, chunk, 1000, 2000)

    assert seen["timeout"] == FFMPEG_CHUNK_TIMEOUT_SECONDS
    assert seen["args"][seen["args"].index("-c:v") + 1] == "copy"


class _FakeVideoChunkClient:
    def __init__(self):
        self.puts = []

    def put_object(self, **kwargs):
        self.puts.append(kwargs)


class _FakeVideoChunkStorage:
    datasets_bucket = "datasets"
    media_cache_bucket = "media-cache"

    def __init__(self):
        self.client = _FakeVideoChunkClient()

    def ensure_bucket(self, bucket):
        assert bucket == "media-cache"


async def _make_video_chunk_fixture(db_session, owner_id, *, codec="h264"):
    dataset = Dataset(
        display_id=f"D-VCH-{uuid.uuid4().hex[:6]}",
        name="videos",
        data_type="video",
        created_by=owner_id,
    )
    db_session.add(dataset)
    await db_session.flush()
    item = DatasetItem(
        dataset_id=dataset.id,
        file_name="clip.mp4",
        file_path="videos/clip.mp4",
        file_type="video",
        metadata_={"video": {"fps": 30, "frame_count": 60, "codec": codec}},
    )
    db_session.add(item)
    await db_session.flush()
    chunk = VideoChunk(
        dataset_item_id=item.id,
        chunk_id=0,
        start_frame=0,
        end_frame=29,
        start_pts_ms=0,
        end_pts_ms=967,
        status="pending",
    )
    db_session.add(chunk)
    await db_session.flush()
    return item, chunk


async def test_store_video_chunk_uses_smart_copy_when_keyframe_aligned(
    db_session, tmp_path, super_admin, monkeypatch
):
    user, _ = super_admin
    item, chunk = await _make_video_chunk_fixture(db_session, user.id, codec="h264")
    db_session.add_all(
        [
            VideoFrameIndex(
                dataset_item_id=item.id,
                frame_index=0,
                pts_ms=0,
                is_keyframe=True,
                pict_type="I",
                byte_offset=100,
            ),
            VideoFrameIndex(
                dataset_item_id=item.id,
                frame_index=29,
                pts_ms=967,
                is_keyframe=False,
                pict_type="P",
                byte_offset=9000,
            ),
        ]
    )
    await db_session.flush()
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"fake")
    calls: list[str] = []

    def fake_smart_copy(input_path, output_path, start_ms, duration_ms):
        calls.append(f"smart:{start_ms}:{duration_ms}")
        output_path.write_bytes(b"smart")

    def fake_transcode(*args):
        calls.append("transcode")

    monkeypatch.setattr(
        "app.workers.media.extract_video_chunk_smart_copy", fake_smart_copy
    )
    monkeypatch.setattr("app.workers.media.extract_video_chunk", fake_transcode)

    storage = _FakeVideoChunkStorage()
    await _store_video_chunk(
        db_session,
        storage,
        item,
        VideoMetadata.model_validate(item.metadata_["video"]),
        source,
        tmp_path,
        chunk,
    )

    assert calls == ["smart:0:1000"]
    assert chunk.status == "ready"
    assert chunk.generation_mode == "smart_copy"
    assert chunk.diagnostics["source_codec"] == "h264"
    assert chunk.diagnostics["output_codec"] == "h264"
    assert chunk.diagnostics["keyframe_aligned"] is True
    assert chunk.diagnostics["start_byte_offset"] == 100
    assert chunk.diagnostics["end_byte_offset"] == 9000
    assert chunk.diagnostics["smart_copy_eligible"] is True
    assert chunk.diagnostics["fallback_reason"] is None
    assert storage.client.puts[0]["Body"] == b"smart"


async def test_store_video_chunk_transcodes_when_not_keyframe_aligned(
    db_session, tmp_path, super_admin, monkeypatch
):
    user, _ = super_admin
    item, chunk = await _make_video_chunk_fixture(db_session, user.id, codec="h264")
    db_session.add(
        VideoFrameIndex(
            dataset_item_id=item.id,
            frame_index=0,
            pts_ms=0,
            is_keyframe=False,
            pict_type="P",
            byte_offset=100,
        )
    )
    await db_session.flush()
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"fake")
    calls: list[str] = []

    def fake_smart_copy(*args):
        calls.append("smart")

    def fake_transcode(input_path, output_path, start_ms, frame_count):
        calls.append(f"transcode:{start_ms}:{frame_count}")
        output_path.write_bytes(b"transcoded")

    monkeypatch.setattr(
        "app.workers.media.extract_video_chunk_smart_copy", fake_smart_copy
    )
    monkeypatch.setattr("app.workers.media.extract_video_chunk", fake_transcode)

    storage = _FakeVideoChunkStorage()
    await _store_video_chunk(
        db_session,
        storage,
        item,
        VideoMetadata.model_validate(item.metadata_["video"]),
        source,
        tmp_path,
        chunk,
    )

    assert calls == ["transcode:0:30"]
    assert chunk.status == "ready"
    assert chunk.generation_mode == "transcode"
    assert chunk.diagnostics["output_codec"] == "h264"
    assert chunk.diagnostics["smart_copy_eligible"] is False
    assert chunk.diagnostics["fallback_reason"] == "start_frame_not_keyframe"
    assert storage.client.puts[0]["Body"] == b"transcoded"


async def test_store_video_chunk_falls_back_when_smart_copy_fails(
    db_session, tmp_path, super_admin, monkeypatch
):
    user, _ = super_admin
    item, chunk = await _make_video_chunk_fixture(db_session, user.id, codec="h264")
    db_session.add(
        VideoFrameIndex(
            dataset_item_id=item.id,
            frame_index=0,
            pts_ms=0,
            is_keyframe=True,
            pict_type="I",
            byte_offset=100,
        )
    )
    await db_session.flush()
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"fake")
    calls: list[str] = []

    def fake_smart_copy(*args):
        calls.append("smart")
        raise RuntimeError("copy failed")

    def fake_transcode(input_path, output_path, start_ms, frame_count):
        calls.append(f"transcode:{start_ms}:{frame_count}")
        output_path.write_bytes(b"transcoded")

    monkeypatch.setattr(
        "app.workers.media.extract_video_chunk_smart_copy", fake_smart_copy
    )
    monkeypatch.setattr("app.workers.media.extract_video_chunk", fake_transcode)

    storage = _FakeVideoChunkStorage()
    await _store_video_chunk(
        db_session,
        storage,
        item,
        VideoMetadata.model_validate(item.metadata_["video"]),
        source,
        tmp_path,
        chunk,
    )

    assert calls == ["smart", "transcode:0:30"]
    assert chunk.status == "ready"
    assert chunk.generation_mode == "transcode"
    assert chunk.diagnostics["output_codec"] == "h264"
    assert chunk.diagnostics["smart_copy_eligible"] is False
    assert chunk.diagnostics["fallback_reason"].startswith("smart_copy_failed:")
    assert storage.client.puts[0]["Body"] == b"transcoded"


def test_video_bbox_geometry_validates_and_bbox_stays_compatible():
    adapter = TypeAdapter(Geometry)

    parsed = adapter.validate_python(
        {
            "type": "video_bbox",
            "frame_index": 12,
            "x": 0.1,
            "y": 0.2,
            "w": 0.3,
            "h": 0.4,
        }
    )
    assert parsed.type == "video_bbox"
    assert parsed.frame_index == 12

    legacy_bbox = adapter.validate_python(
        {"type": "bbox", "x": 0, "y": 0, "w": 1, "h": 1}
    )
    assert legacy_bbox.type == "bbox"


def test_video_track_geometry_validates_and_video_bbox_stays_compatible():
    adapter = TypeAdapter(Geometry)

    parsed = adapter.validate_python(
        {
            "type": "video_track_bbox",
            "track_id": "trk_abc123",
            "keyframes": [
                {
                    "frame_index": 0,
                    "bbox": {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
                    "source": "manual",
                },
                {
                    "frame_index": 30,
                    "bbox": {"x": 0.2, "y": 0.2, "w": 0.3, "h": 0.4},
                    "source": "prediction",
                    "occluded": True,
                },
            ],
        }
    )
    assert parsed.type == "video_track_bbox"
    assert parsed.track_id == "trk_abc123"
    assert parsed.keyframes[1].occluded is True

    legacy_video_bbox = adapter.validate_python(
        {
            "type": "video_bbox",
            "frame_index": 12,
            "x": 0.1,
            "y": 0.2,
            "w": 0.3,
            "h": 0.4,
        }
    )
    assert legacy_video_bbox.type == "video_bbox"


@pytest.mark.parametrize(
    "geometry",
    [
        {"type": "video_track_bbox", "track_id": "trk_empty", "keyframes": []},
        {
            "type": "video_track_bbox",
            "track_id": "trk_negative",
            "keyframes": [
                {
                    "frame_index": -1,
                    "bbox": {"x": 0, "y": 0, "w": 1, "h": 1},
                    "source": "manual",
                }
            ],
        },
    ],
)
def test_video_track_geometry_rejects_invalid_keyframes(geometry):
    adapter = TypeAdapter(Geometry)

    with pytest.raises(ValueError):
        adapter.validate_python(geometry)


async def test_get_task_exposes_video_metadata(
    db_session,
    httpx_client,
    super_admin,
    monkeypatch,
):
    user, token = super_admin
    project = Project(
        display_id=f"P-VID-{uuid.uuid4().hex[:6]}",
        name="Video Project",
        type_key="video-track",
        type_label="视频 · 时序追踪",
        owner_id=user.id,
        tool_bindings=build_tool_bindings(["car"]),
    )
    dataset = Dataset(
        display_id=f"D-VID-{uuid.uuid4().hex[:6]}",
        name="videos",
        data_type="video",
        created_by=user.id,
    )
    db_session.add_all([project, dataset])
    await db_session.flush()
    item = DatasetItem(
        dataset_id=dataset.id,
        file_name="clip.mp4",
        file_path="videos/clip.mp4",
        file_type="video",
        width=640,
        height=360,
        thumbnail_path="thumbnails/poster.webp",
        metadata_={
            "video": {
                "duration_ms": 1000,
                "fps": 25,
                "frame_count": 25,
                "width": 640,
                "height": 360,
                "codec": "h264",
                "poster_frame_path": "thumbnails/poster.webp",
            }
        },
    )
    db_session.add(item)
    await db_session.flush()
    task = Task(
        project_id=project.id,
        dataset_item_id=item.id,
        display_id=f"T-VID-{uuid.uuid4().hex[:6]}",
        file_name="clip.mp4",
        file_path="videos/clip.mp4",
        file_type="video",
        status="pending",
    )
    db_session.add(task)
    await db_session.flush()

    monkeypatch.setattr(
        "app.api.v1.tasks.video.storage_service.generate_download_url",
        lambda key, expires_in=3600, bucket=None: f"http://storage.local/{key}",
    )

    resp = await httpx_client.get(
        f"/api/v1/tasks/{task.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["video_metadata"]["frame_count"] == 25
    assert body["thumbnail_url"] == "http://storage.local/thumbnails/poster.webp"


async def test_video_manifest_returns_signed_urls(
    db_session,
    httpx_client,
    super_admin,
    monkeypatch,
):
    user, token = super_admin
    project = Project(
        display_id=f"P-VID-{uuid.uuid4().hex[:6]}",
        name="Video Project",
        type_key="video-track",
        type_label="视频 · 时序追踪",
        owner_id=user.id,
        tool_bindings=build_tool_bindings(["car"]),
    )
    dataset = Dataset(
        display_id=f"D-VID-{uuid.uuid4().hex[:6]}",
        name="videos",
        data_type="video",
        created_by=user.id,
    )
    db_session.add_all([project, dataset])
    await db_session.flush()
    item = DatasetItem(
        dataset_id=dataset.id,
        file_name="clip.mp4",
        file_path="videos/clip.mp4",
        file_type="video",
        metadata_={
            "video": {
                "fps": 30,
                "frame_count": 90,
                "playback_path": "playback/clip.mp4",
                "playback_codec": "h264",
                "poster_frame_path": "posters/clip.webp",
            }
        },
    )
    db_session.add(item)
    await db_session.flush()
    task = Task(
        project_id=project.id,
        dataset_item_id=item.id,
        display_id=f"T-VID-{uuid.uuid4().hex[:6]}",
        file_name="clip.mp4",
        file_path="videos/clip.mp4",
        file_type="video",
        status="pending",
    )
    db_session.add(task)
    await db_session.flush()

    signed: list[tuple[str, int]] = []

    def fake_generate_download_url(key, expires_in=3600, bucket=None):
        signed.append((key, expires_in))
        return f"http://storage.local/{key}"

    monkeypatch.setattr(
        "app.api.v1.tasks.video.storage_service.generate_download_url",
        fake_generate_download_url,
    )

    resp = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/video/manifest",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["task_id"] == str(task.id)
    assert body["video_url"] == "http://storage.local/playback/clip.mp4"
    assert body["poster_url"] == "http://storage.local/posters/clip.webp"
    assert body["metadata"]["fps"] == 30
    assert body["metadata"]["playback_path"] == "playback/clip.mp4"
    assert body["expires_in"] == 3600
    assert signed == [("playback/clip.mp4", 3600), ("posters/clip.webp", 3600)]


async def test_video_frame_timetable_returns_ffprobe_rows(
    db_session,
    httpx_client,
    super_admin,
):
    user, token = super_admin
    project = Project(
        display_id=f"P-VID-{uuid.uuid4().hex[:6]}",
        name="Video Project",
        type_key="video-track",
        type_label="视频 · 时序追踪",
        owner_id=user.id,
        tool_bindings=build_tool_bindings(["car"]),
    )
    dataset = Dataset(
        display_id=f"D-VID-{uuid.uuid4().hex[:6]}",
        name="videos",
        data_type="video",
        created_by=user.id,
    )
    db_session.add_all([project, dataset])
    await db_session.flush()
    item = DatasetItem(
        dataset_id=dataset.id,
        file_name="clip.mp4",
        file_path="videos/clip.mp4",
        file_type="video",
        metadata_={"video": {"fps": 30, "frame_count": 4}},
    )
    db_session.add(item)
    await db_session.flush()
    task = Task(
        project_id=project.id,
        dataset_item_id=item.id,
        display_id=f"T-VID-{uuid.uuid4().hex[:6]}",
        file_name="clip.mp4",
        file_path="videos/clip.mp4",
        file_type="video",
        status="pending",
    )
    db_session.add(task)
    db_session.add_all(
        [
            VideoFrameIndex(
                dataset_item_id=item.id,
                frame_index=0,
                pts_ms=0,
                is_keyframe=True,
                pict_type="I",
                byte_offset=10,
            ),
            VideoFrameIndex(
                dataset_item_id=item.id,
                frame_index=1,
                pts_ms=33,
                is_keyframe=False,
                pict_type="P",
                byte_offset=20,
            ),
            VideoFrameIndex(
                dataset_item_id=item.id,
                frame_index=2,
                pts_ms=67,
                is_keyframe=False,
                pict_type="P",
                byte_offset=30,
            ),
        ]
    )
    await db_session.flush()

    resp = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/video/frame-timetable?from=1&to=2",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "ffprobe"
    assert body["fps"] == 30
    assert body["frame_count"] == 4
    assert body["frames"] == [
        {
            "frame_index": 1,
            "pts_ms": 33,
            "is_keyframe": False,
            "pict_type": "P",
            "byte_offset": 20,
        },
        {
            "frame_index": 2,
            "pts_ms": 67,
            "is_keyframe": False,
            "pict_type": "P",
            "byte_offset": 30,
        },
    ]


async def test_video_frame_timetable_falls_back_to_estimated(
    db_session,
    httpx_client,
    super_admin,
):
    user, token = super_admin
    project = Project(
        display_id=f"P-VID-{uuid.uuid4().hex[:6]}",
        name="Video Project",
        type_key="video-track",
        type_label="视频 · 时序追踪",
        owner_id=user.id,
        tool_bindings=build_tool_bindings(["car"]),
    )
    dataset = Dataset(
        display_id=f"D-VID-{uuid.uuid4().hex[:6]}",
        name="videos",
        data_type="video",
        created_by=user.id,
    )
    db_session.add_all([project, dataset])
    await db_session.flush()
    item = DatasetItem(
        dataset_id=dataset.id,
        file_name="clip.mp4",
        file_path="videos/clip.mp4",
        file_type="video",
        metadata_={"video": {"fps": 24, "frame_count": 12}},
    )
    db_session.add(item)
    await db_session.flush()
    task = Task(
        project_id=project.id,
        dataset_item_id=item.id,
        display_id=f"T-VID-{uuid.uuid4().hex[:6]}",
        file_name="clip.mp4",
        file_path="videos/clip.mp4",
        file_type="video",
        status="pending",
    )
    db_session.add(task)
    await db_session.flush()

    resp = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/video/frame-timetable",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "estimated"
    assert body["fps"] == 24
    assert body["frame_count"] == 12
    assert body["frames"] == []


async def test_video_manifest_returns_503_when_metadata_not_ready(
    db_session,
    httpx_client,
    super_admin,
    monkeypatch,
):
    user, token = super_admin
    project = Project(
        display_id=f"P-VID-{uuid.uuid4().hex[:6]}",
        name="Video Project",
        type_key="video-track",
        type_label="视频 · 时序追踪",
        owner_id=user.id,
        tool_bindings=build_tool_bindings(["car"]),
    )
    db_session.add(project)
    await db_session.flush()
    task = Task(
        project_id=project.id,
        display_id=f"T-VID-{uuid.uuid4().hex[:6]}",
        file_name="clip.mp4",
        file_path="videos/clip.mp4",
        file_type="video",
        status="pending",
    )
    db_session.add(task)
    await db_session.flush()

    monkeypatch.setattr(
        "app.api.v1.tasks.video.storage_service.generate_download_url",
        lambda key, expires_in=3600, bucket=None: f"http://storage.local/{key}",
    )

    resp = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/video/manifest",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 503
    assert resp.json()["detail"] == "Video metadata not ready"


async def test_video_manifest_rejects_non_video(
    db_session,
    httpx_client,
    super_admin,
):
    user, token = super_admin
    project = Project(
        display_id=f"P-IMG-{uuid.uuid4().hex[:6]}",
        name="Image Project",
        type_key="image-det",
        type_label="图像目标检测",
        owner_id=user.id,
        tool_bindings=build_tool_bindings(["car"]),
    )
    db_session.add(project)
    await db_session.flush()
    task = Task(
        project_id=project.id,
        display_id=f"T-IMG-{uuid.uuid4().hex[:6]}",
        file_name="image.jpg",
        file_path="images/image.jpg",
        file_type="image",
        status="pending",
    )
    db_session.add(task)
    await db_session.flush()

    resp = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/video/manifest",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 400


async def _create_video_export_fixture(db_session, user):
    suffix = uuid.uuid4().hex[:6]
    project = Project(
        display_id=f"P-VID-{suffix}",
        name="Video Project",
        type_key="video-track",
        type_label="视频 · 时序追踪",
        data_type="video",
        owner_id=user.id,
        tool_bindings=build_tool_bindings(
            ["car", "person"],
            attribute_schema={
                "fields": [
                    {"key": "speed", "label": "Speed", "type": "number"},
                ]
            },
        ),
    )
    dataset = Dataset(
        display_id=f"D-VID-{suffix}",
        name="videos",
        data_type="video",
        created_by=user.id,
    )
    db_session.add_all([project, dataset])
    await db_session.flush()

    batch_a = TaskBatch(
        project_id=project.id,
        dataset_id=dataset.id,
        display_id=f"B-A-{suffix}",
        name="Batch A",
        created_by=user.id,
    )
    batch_b = TaskBatch(
        project_id=project.id,
        dataset_id=dataset.id,
        display_id=f"B-B-{suffix}",
        name="Batch B",
        created_by=user.id,
    )
    db_session.add_all([batch_a, batch_b])
    await db_session.flush()

    item_a = DatasetItem(
        dataset_id=dataset.id,
        file_name="clip-a.mp4",
        file_path="videos/clip-a.mp4",
        file_type="video",
        metadata_={
            "video": {
                "fps": 10,
                "frame_count": 6,
                "width": 640,
                "height": 360,
                "codec": "h264",
            }
        },
    )
    item_b = DatasetItem(
        dataset_id=dataset.id,
        file_name="clip-b.mp4",
        file_path="videos/clip-b.mp4",
        file_type="video",
        metadata_={"video": {"fps": 10, "frame_count": 3}},
    )
    db_session.add_all([item_a, item_b])
    await db_session.flush()

    task_a = Task(
        project_id=project.id,
        dataset_item_id=item_a.id,
        batch_id=batch_a.id,
        display_id=f"T-A-{suffix}",
        file_name="clip-a.mp4",
        file_path="videos/clip-a.mp4",
        file_type="video",
        status="pending",
        sequence_order=1,
    )
    task_b = Task(
        project_id=project.id,
        dataset_item_id=item_b.id,
        batch_id=batch_b.id,
        display_id=f"T-B-{suffix}",
        file_name="clip-b.mp4",
        file_path="videos/clip-b.mp4",
        file_type="video",
        status="pending",
        sequence_order=2,
    )
    db_session.add_all([task_a, task_b])
    await db_session.flush()

    track_ann = Annotation(
        task_id=task_a.id,
        project_id=project.id,
        user_id=user.id,
        annotation_type="video_track_bbox",
        class_name="car",
        geometry={
            "type": "video_track_bbox",
            "track_id": "trk_car",
            "keyframes": [
                {
                    "frame_index": 0,
                    "bbox": {"x": 0.1, "y": 0.2, "w": 0.2, "h": 0.2},
                    "source": "manual",
                },
                {
                    "frame_index": 2,
                    "bbox": {"x": 0.3, "y": 0.4, "w": 0.2, "h": 0.2},
                    "source": "manual",
                },
                {
                    "frame_index": 4,
                    "bbox": {"x": 0.5, "y": 0.6, "w": 0.2, "h": 0.2},
                    "source": "manual",
                },
            ],
            "outside": [{"from": 4, "to": 4}],
        },
        attributes={"speed": 42},
    )
    bbox_ann = Annotation(
        task_id=task_a.id,
        project_id=project.id,
        user_id=user.id,
        annotation_type="video_bbox",
        class_name="person",
        geometry={
            "type": "video_bbox",
            "frame_index": 1,
            "x": 0.2,
            "y": 0.2,
            "w": 0.1,
            "h": 0.1,
        },
        attributes={"speed": 3},
    )
    other_batch_ann = Annotation(
        task_id=task_b.id,
        project_id=project.id,
        user_id=user.id,
        annotation_type="video_track_bbox",
        class_name="person",
        geometry={
            "type": "video_track_bbox",
            "track_id": "trk_person",
            "keyframes": [
                {
                    "frame_index": 0,
                    "bbox": {"x": 0.4, "y": 0.1, "w": 0.1, "h": 0.2},
                    "source": "manual",
                }
            ],
        },
    )
    db_session.add_all([track_ann, bbox_ann, other_batch_ann])
    await db_session.flush()
    return project, batch_a, batch_b


async def _video_fixture_task_and_track(db_session, project):
    task = (
        await db_session.execute(
            select(Task).where(
                Task.project_id == project.id, Task.file_name == "clip-a.mp4"
            )
        )
    ).scalar_one()
    track = (
        await db_session.execute(
            select(Annotation).where(
                Annotation.task_id == task.id,
                Annotation.annotation_type == "video_track_bbox",
                Annotation.class_name == "car",
                Annotation.is_active.is_(True),
            )
        )
    ).scalar_one()
    return task, track


async def test_video_project_export_returns_video_tracks_json(
    db_session,
    super_admin,
):
    # v0.10.27 导出已异步化 (POST→job); 内容正确性直接断言 ExportService。
    user, _ = super_admin
    project, _, _ = await _create_video_export_fixture(db_session, user)

    body = json.loads(await ExportService(db_session).export_coco(project.id))

    assert body["export_type"] == "video_tracks"
    assert body["frame_mode"] == "keyframes"
    assert body["project"]["attribute_schema"]["fields"][0]["key"] == "speed"
    assert [c["name"] for c in body["categories"]] == ["car", "person"]
    assert len(body["tasks"]) == 2
    assert body["tasks"][0]["video_metadata"]["frame_count"] == 6
    assert len(body["tracks"]) == 2
    assert "frames" not in body["tracks"][0]
    assert len(body["keyframes"]) == 4
    assert len(body["video_bbox"]) == 1


async def test_video_batch_export_filters_tasks_and_annotations(
    db_session,
    super_admin,
):
    user, _ = super_admin
    project, batch_a, _ = await _create_video_export_fixture(db_session, user)

    body = json.loads(
        await ExportService(db_session).export_coco(project.id, batch_id=batch_a.id)
    )
    assert [task["display_id"] for task in body["tasks"]] == [
        batch_a.display_id.replace("B-A", "T-A")
    ]
    assert [track["track_id"] for track in body["tracks"]] == ["trk_car"]
    assert len(body["video_bbox"]) == 1


async def test_video_frame_range_export_clips_task_tracks_and_single_frames(
    db_session,
    super_admin,
):
    from app.services.exporting.video_scope import VideoExportScope

    user, _ = super_admin
    project, _, _ = await _create_video_export_fixture(db_session, user)
    task, track_row = await _video_fixture_task_and_track(db_session, project)
    track_row.geometry = {**track_row.geometry, "outside": []}
    await db_session.flush()
    scope = VideoExportScope(
        task_id=task.id,
        dataset_item_id=task.dataset_item_id,
        selection_kind="frames",
        from_frame=1,
        to_frame=3,
    )

    body = json.loads(
        await ExportService(db_session).export_coco(
            project.id,
            video_frame_mode="all_frames",
            video_scope=scope,
        )
    )

    assert [row["id"] for row in body["tasks"]] == [str(task.id)]
    assert body["export_scope"] == scope.as_dict()
    track = body["tracks"][0]
    assert [row["frame_index"] for row in track["keyframes"]] == [1, 2, 3]
    assert [row["source"] for row in track["keyframes"]] == [
        "interpolated",
        "manual",
        "interpolated",
    ]
    assert [row["frame_index"] for row in track["frames"]] == [1, 2, 3]
    assert [row["frame_index"] for row in body["keyframes"]] == [1, 2, 3]
    assert [row["frame_index"] for row in body["video_bbox"]] == [1]


async def test_video_export_all_frames_interpolates_and_outside_blocks(
    db_session,
    super_admin,
):
    user, _ = super_admin
    project, _, _ = await _create_video_export_fixture(db_session, user)

    body = json.loads(
        await ExportService(db_session).export_coco(
            project.id, video_frame_mode="all_frames"
        )
    )
    car = next(track for track in body["tracks"] if track["track_id"] == "trk_car")
    assert body["frame_mode"] == "all_frames"
    assert [frame["frame_index"] for frame in car["frames"]] == [0, 1, 2]
    assert car["frames"][1]["source"] == "interpolated"
    assert car["frames"][1]["bbox"] == {"x": 0.2, "y": 0.3, "w": 0.2, "h": 0.2}


async def test_video_export_preserves_and_applies_outside_ranges(
    db_session,
    super_admin,
):
    user, _ = super_admin
    project, _, _ = await _create_video_export_fixture(db_session, user)
    _, track = await _video_fixture_task_and_track(db_session, project)
    # frame 4 关键帧仍消失 (沿用 fixture 的 {4,4} manual outside);
    # 再叠加 {1,1} manual 与 {5,6} prediction。不同来源的相邻区间不合并，
    # 避免恢复 manual outside 时意外删除 prediction 范围。
    track.geometry = {
        **track.geometry,
        "outside": [
            {"from": 1, "to": 1},
            {"from": 4, "to": 4},
            {"from": 5, "to": 6, "source": "prediction"},
        ],
    }
    await db_session.flush()

    body = json.loads(
        await ExportService(db_session).export_coco(
            project.id, video_frame_mode="all_frames"
        )
    )
    car = next(track for track in body["tracks"] if track["track_id"] == "trk_car")
    assert car["outside"] == [
        {"from": 1, "to": 1, "source": "manual"},
        {"from": 4, "to": 4, "source": "manual"},
        {"from": 5, "to": 6, "source": "prediction"},
    ]
    assert [frame["frame_index"] for frame in car["frames"]] == [0, 2]


async def test_video_track_convert_frame_copy_preserves_source(
    db_session,
    httpx_client,
    super_admin,
):
    user, token = super_admin
    project, _, _ = await _create_video_export_fixture(db_session, user)
    task, track = await _video_fixture_task_and_track(db_session, project)

    resp = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/{track.id}/video/convert-to-bboxes",
        json={"operation": "copy", "scope": "frame", "frame_index": 1},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted_source"] is False
    assert body["source_annotation"]["id"] == str(track.id)
    assert len(body["created_annotations"]) == 1
    created = body["created_annotations"][0]
    assert created["annotation_type"] == "video_bbox"
    assert created["geometry"] == {
        "type": "video_bbox",
        "frame_index": 1,
        "x": 0.2,
        "y": 0.3,
        "w": 0.2,
        "h": 0.2,
    }


async def test_video_track_convert_frame_copy_rejects_outside_frame(
    db_session,
    httpx_client,
    super_admin,
):
    user, token = super_admin
    project, _, _ = await _create_video_export_fixture(db_session, user)
    task, track = await _video_fixture_task_and_track(db_session, project)
    track.geometry = {**track.geometry, "outside": [{"from": 1, "to": 1}]}
    await db_session.flush()

    resp = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/{track.id}/video/convert-to-bboxes",
        json={"operation": "copy", "scope": "frame", "frame_index": 1},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "track has no bbox at the requested frame"


async def test_video_track_convert_frame_split_removes_exact_keyframe(
    db_session,
    httpx_client,
    super_admin,
):
    user, token = super_admin
    project, _, _ = await _create_video_export_fixture(db_session, user)
    task, track = await _video_fixture_task_and_track(db_session, project)

    resp = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/{track.id}/video/convert-to-bboxes",
        json={"operation": "split", "scope": "frame", "frame_index": 2},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted_source"] is False
    assert body["removed_frame_indexes"] == [2]
    assert [
        kf["frame_index"] for kf in body["source_annotation"]["geometry"]["keyframes"]
    ] == [0, 4]
    assert body["created_annotations"][0]["geometry"]["frame_index"] == 2


async def test_video_track_convert_track_split_all_frames_deletes_source(
    db_session,
    httpx_client,
    super_admin,
):
    user, token = super_admin
    project, _, _ = await _create_video_export_fixture(db_session, user)
    task, track = await _video_fixture_task_and_track(db_session, project)

    resp = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/{track.id}/video/convert-to-bboxes",
        json={
            "operation": "split",
            "scope": "track",
            "frame_mode": "all_frames",
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted_source"] is True
    assert body["removed_frame_indexes"] == [0, 1, 2]
    assert body["source_annotation"] is None
    assert [ann["geometry"]["frame_index"] for ann in body["created_annotations"]] == [
        0,
        1,
        2,
    ]
    assert body["created_annotations"][1]["geometry"]["x"] == 0.2


async def test_video_track_convert_track_copy_keeps_removed_frames_empty(
    db_session,
    httpx_client,
    super_admin,
):
    user, token = super_admin
    project, _, _ = await _create_video_export_fixture(db_session, user)
    task, track = await _video_fixture_task_and_track(db_session, project)

    resp = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/{track.id}/video/convert-to-bboxes",
        json={
            "operation": "copy",
            "scope": "track",
            "frame_mode": "all_frames",
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted_source"] is False
    assert body["removed_frame_indexes"] == []
    assert body["source_annotation"]["id"] == str(track.id)
    assert [ann["geometry"]["frame_index"] for ann in body["created_annotations"]] == [
        0,
        1,
        2,
    ]


async def test_video_track_convert_rejects_non_track_annotation(
    db_session,
    httpx_client,
    super_admin,
):
    user, token = super_admin
    project, _, _ = await _create_video_export_fixture(db_session, user)
    task = (
        await db_session.execute(
            select(Task).where(
                Task.project_id == project.id, Task.file_name == "clip-a.mp4"
            )
        )
    ).scalar_one()
    bbox = (
        await db_session.execute(
            select(Annotation).where(
                Annotation.task_id == task.id,
                Annotation.annotation_type == "video_bbox",
            )
        )
    ).scalar_one()

    resp = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/{bbox.id}/video/convert-to-bboxes",
        json={"operation": "copy", "scope": "frame", "frame_index": 1},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Annotation is not a video_track_bbox"


async def test_video_track_convert_requires_task_visibility(
    db_session,
    httpx_client,
    super_admin,
    annotator,
):
    user, _ = super_admin
    annotator_user, annotator_token = annotator
    project, _, _ = await _create_video_export_fixture(db_session, user)
    task, track = await _video_fixture_task_and_track(db_session, project)
    # Valid membership so the account passes project access, while the
    # unassigned task must still be hidden from this annotator.
    await create_membership(
        db_session,
        project_id=project.id,
        user_id=annotator_user.id,
        role="annotator",
        assigned_by=user.id,
    )
    await db_session.commit()

    resp = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/{track.id}/video/convert-to-bboxes",
        json={"operation": "copy", "scope": "frame", "frame_index": 1},
        headers={"Authorization": f"Bearer {annotator_token}"},
    )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Task not found"


async def test_video_track_composition_aggregate_bboxes_deletes_sources(
    db_session,
    httpx_client,
    super_admin,
):
    user, token = super_admin
    project, _, _ = await _create_video_export_fixture(db_session, user)
    task = (
        await db_session.execute(
            select(Task).where(
                Task.project_id == project.id, Task.file_name == "clip-a.mp4"
            )
        )
    ).scalar_one()
    first = (
        await db_session.execute(
            select(Annotation).where(
                Annotation.task_id == task.id,
                Annotation.annotation_type == "video_bbox",
            )
        )
    ).scalar_one()
    second = Annotation(
        task_id=task.id,
        project_id=project.id,
        user_id=user.id,
        annotation_type="video_bbox",
        class_name="person",
        geometry={
            "type": "video_bbox",
            "frame_index": 3,
            "x": 0.3,
            "y": 0.2,
            "w": 0.1,
            "h": 0.1,
        },
    )
    db_session.add(second)
    await db_session.flush()

    resp = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/video/track-compositions",
        json={
            "operation": "aggregate_bboxes",
            "annotation_ids": [str(first.id), str(second.id)],
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["operation"] == "aggregate_bboxes"
    assert set(body["deleted_annotation_ids"]) == {str(first.id), str(second.id)}
    created = body["created_annotations"][0]
    assert created["annotation_type"] == "video_track_bbox"
    assert created["class_name"] == "person"
    assert [kf["frame_index"] for kf in created["geometry"]["keyframes"]] == [1, 3]
    await db_session.refresh(first)
    await db_session.refresh(second)
    assert first.is_active is False
    assert second.is_active is False


async def test_video_track_composition_aggregate_rejects_mixed_classes(
    db_session,
    httpx_client,
    super_admin,
):
    user, token = super_admin
    project, _, _ = await _create_video_export_fixture(db_session, user)
    task = (
        await db_session.execute(
            select(Task).where(
                Task.project_id == project.id, Task.file_name == "clip-a.mp4"
            )
        )
    ).scalar_one()
    person = (
        await db_session.execute(
            select(Annotation).where(
                Annotation.task_id == task.id,
                Annotation.annotation_type == "video_bbox",
            )
        )
    ).scalar_one()
    car = Annotation(
        task_id=task.id,
        project_id=project.id,
        user_id=user.id,
        annotation_type="video_bbox",
        class_name="car",
        geometry={
            "type": "video_bbox",
            "frame_index": 3,
            "x": 0.3,
            "y": 0.2,
            "w": 0.1,
            "h": 0.1,
        },
    )
    db_session.add(car)
    await db_session.flush()

    resp = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/video/track-compositions",
        json={
            "operation": "aggregate_bboxes",
            "annotation_ids": [str(person.id), str(car.id)],
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "video_bbox annotations must share one class"


async def test_video_track_composition_aggregate_rejects_duplicate_frames(
    db_session,
    httpx_client,
    super_admin,
):
    user, token = super_admin
    project, _, _ = await _create_video_export_fixture(db_session, user)
    task = (
        await db_session.execute(
            select(Task).where(
                Task.project_id == project.id, Task.file_name == "clip-a.mp4"
            )
        )
    ).scalar_one()
    first = (
        await db_session.execute(
            select(Annotation).where(
                Annotation.task_id == task.id,
                Annotation.annotation_type == "video_bbox",
            )
        )
    ).scalar_one()
    second = Annotation(
        task_id=task.id,
        project_id=project.id,
        user_id=user.id,
        annotation_type="video_bbox",
        class_name="person",
        geometry={
            "type": "video_bbox",
            "frame_index": 1,
            "x": 0.3,
            "y": 0.2,
            "w": 0.1,
            "h": 0.1,
        },
    )
    db_session.add(second)
    await db_session.flush()

    resp = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/video/track-compositions",
        json={
            "operation": "aggregate_bboxes",
            "annotation_ids": [str(first.id), str(second.id)],
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 400
    assert (
        resp.json()["detail"] == "video_bbox annotations must not share a frame_index"
    )


async def test_video_track_composition_split_visible_frame_creates_tail_track(
    db_session,
    httpx_client,
    super_admin,
):
    user, token = super_admin
    project, _, _ = await _create_video_export_fixture(db_session, user)
    task, track = await _video_fixture_task_and_track(db_session, project)

    resp = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/video/track-compositions",
        json={
            "operation": "split_track",
            "annotation_ids": [str(track.id)],
            "frame_index": 1,
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200
    body = resp.json()
    source = body["updated_annotations"][0]
    tail = body["created_annotations"][0]
    assert source["id"] == str(track.id)
    assert [kf["frame_index"] for kf in source["geometry"]["keyframes"]] == [0, 1]
    assert [kf["frame_index"] for kf in tail["geometry"]["keyframes"]] == [2, 4]
    assert tail["parent_annotation_id"] == str(track.id)


async def test_video_track_composition_split_rejects_absent_frame(
    db_session,
    httpx_client,
    super_admin,
):
    user, token = super_admin
    project, _, _ = await _create_video_export_fixture(db_session, user)
    task, track = await _video_fixture_task_and_track(db_session, project)

    resp = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/video/track-compositions",
        json={
            "operation": "split_track",
            "annotation_ids": [str(track.id)],
            "frame_index": 4,
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "split_track requires a visible frame"


async def test_video_track_composition_merge_tracks_adds_outside_gap(
    db_session,
    httpx_client,
    super_admin,
):
    user, token = super_admin
    project, _, _ = await _create_video_export_fixture(db_session, user)
    task, first = await _video_fixture_task_and_track(db_session, project)
    second = Annotation(
        task_id=task.id,
        project_id=project.id,
        user_id=user.id,
        annotation_type="video_track_bbox",
        class_name="car",
        geometry={
            "type": "video_track_bbox",
            "track_id": "trk_car_tail",
            "keyframes": [
                {
                    "frame_index": 5,
                    "bbox": {"x": 0.6, "y": 0.2, "w": 0.2, "h": 0.2},
                    "source": "manual",
                },
                {
                    "frame_index": 6,
                    "bbox": {"x": 0.7, "y": 0.2, "w": 0.2, "h": 0.2},
                    "source": "manual",
                },
            ],
        },
    )
    db_session.add(second)
    await db_session.flush()

    resp = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/video/track-compositions",
        json={
            "operation": "merge_tracks",
            "annotation_ids": [str(first.id), str(second.id)],
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted_annotation_ids"] == [str(second.id)]
    merged = body["updated_annotations"][0]
    assert merged["id"] == str(first.id)
    assert [kf["frame_index"] for kf in merged["geometry"]["keyframes"]] == [
        0,
        2,
        4,
        5,
        6,
    ]
    assert {"from": 3, "to": 4, "source": "manual"} in merged["geometry"]["outside"]
    await db_session.refresh(second)
    assert second.is_active is False


async def test_video_track_composition_merge_rejects_overlap_and_mixed_classes(
    db_session,
    httpx_client,
    super_admin,
):
    user, token = super_admin
    project, _, _ = await _create_video_export_fixture(db_session, user)
    task, first = await _video_fixture_task_and_track(db_session, project)
    overlap = Annotation(
        task_id=task.id,
        project_id=project.id,
        user_id=user.id,
        annotation_type="video_track_bbox",
        class_name="car",
        geometry={
            "type": "video_track_bbox",
            "track_id": "trk_overlap",
            "keyframes": [
                {
                    "frame_index": 1,
                    "bbox": {"x": 0.6, "y": 0.2, "w": 0.2, "h": 0.2},
                    "source": "manual",
                },
            ],
        },
    )
    mixed = Annotation(
        task_id=task.id,
        project_id=project.id,
        user_id=user.id,
        annotation_type="video_track_bbox",
        class_name="person",
        geometry={
            "type": "video_track_bbox",
            "track_id": "trk_person_tail",
            "keyframes": [
                {
                    "frame_index": 5,
                    "bbox": {"x": 0.6, "y": 0.2, "w": 0.2, "h": 0.2},
                    "source": "manual",
                },
            ],
        },
    )
    db_session.add_all([overlap, mixed])
    await db_session.flush()

    overlap_resp = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/video/track-compositions",
        json={
            "operation": "merge_tracks",
            "annotation_ids": [str(first.id), str(overlap.id)],
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    mixed_resp = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/video/track-compositions",
        json={
            "operation": "merge_tracks",
            "annotation_ids": [str(first.id), str(mixed.id)],
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert overlap_resp.status_code == 400
    assert (
        overlap_resp.json()["detail"] == "merge_tracks requires non-overlapping tracks"
    )
    assert mixed_resp.status_code == 400
    assert (
        mixed_resp.json()["detail"]
        == "merge_tracks requires tracks with the same class"
    )


async def _add_car_tail_track(db_session, task, project, user, *, frame_index=6):
    # trk_car 已有 keyframes [0, 2, 4]; 这里造一条帧号在后、与之不重叠的同 class track,
    # 形成 gap [5..frame_index-1] 供 join 测试.
    tail = Annotation(
        task_id=task.id,
        project_id=project.id,
        user_id=user.id,
        annotation_type="video_track_bbox",
        class_name="car",
        geometry={
            "type": "video_track_bbox",
            "track_id": "trk_car_tail",
            "keyframes": [
                {
                    "frame_index": frame_index,
                    "bbox": {"x": 0.8, "y": 0.2, "w": 0.2, "h": 0.2},
                    "source": "manual",
                },
            ],
        },
    )
    db_session.add(tail)
    await db_session.flush()
    return tail


async def test_video_track_composition_join_interpolate_no_gap_outside(
    db_session,
    httpx_client,
    super_admin,
):
    user, token = super_admin
    project, _, _ = await _create_video_export_fixture(db_session, user)
    task, first = await _video_fixture_task_and_track(db_session, project)
    # fixture 的 car track 自带 {4,4} outside (frame4 消失); join 测试聚焦 gap 行为,
    # 先清空使 [0,2,4] 全可见, 与 "gap [5..5]" 断言对齐。
    first.geometry = {**first.geometry, "outside": []}
    await db_session.flush()
    tail = await _add_car_tail_track(db_session, task, project, user)

    resp = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/video/track-compositions",
        json={
            "operation": "join_tracks",
            "annotation_ids": [str(first.id), str(tail.id)],
            "gap_mode": "interpolate",
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["operation"] == "join_tracks"
    assert body["deleted_annotation_ids"] == [str(tail.id)]
    merged = body["updated_annotations"][0]
    assert merged["id"] == str(first.id)
    assert [kf["frame_index"] for kf in merged["geometry"]["keyframes"]] == [0, 2, 4, 6]
    # interpolate 模式: gap [5..5] 不写 outside, 靠现有线性插值连接.
    assert merged["geometry"]["outside"] == []
    await db_session.refresh(tail)
    assert tail.is_active is False


async def test_video_track_composition_join_outside_marks_gap(
    db_session,
    httpx_client,
    super_admin,
):
    user, token = super_admin
    project, _, _ = await _create_video_export_fixture(db_session, user)
    task, first = await _video_fixture_task_and_track(db_session, project)
    first.geometry = {**first.geometry, "outside": []}
    await db_session.flush()
    tail = await _add_car_tail_track(db_session, task, project, user)

    resp = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/video/track-compositions",
        json={
            "operation": "join_tracks",
            "annotation_ids": [str(first.id), str(tail.id)],
            "gap_mode": "outside",
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200
    body = resp.json()
    merged = body["updated_annotations"][0]
    assert [kf["frame_index"] for kf in merged["geometry"]["keyframes"]] == [0, 2, 4, 6]
    # outside 模式: gap [5..5] 标 outside 后合并 (与 merge_tracks 一致).
    assert {"from": 5, "to": 5, "source": "manual"} in merged["geometry"]["outside"]
    await db_session.refresh(tail)
    assert tail.is_active is False


async def test_video_track_composition_join_default_gap_mode_is_interpolate(
    db_session,
    httpx_client,
    super_admin,
):
    user, token = super_admin
    project, _, _ = await _create_video_export_fixture(db_session, user)
    task, first = await _video_fixture_task_and_track(db_session, project)
    first.geometry = {**first.geometry, "outside": []}
    await db_session.flush()
    tail = await _add_car_tail_track(db_session, task, project, user)

    resp = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/video/track-compositions",
        json={
            "operation": "join_tracks",
            "annotation_ids": [str(first.id), str(tail.id)],
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200
    merged = resp.json()["updated_annotations"][0]
    assert merged["geometry"]["outside"] == []


async def test_video_track_composition_join_rejects_overlap_and_mixed_classes(
    db_session,
    httpx_client,
    super_admin,
):
    user, token = super_admin
    project, _, _ = await _create_video_export_fixture(db_session, user)
    task, first = await _video_fixture_task_and_track(db_session, project)
    overlap = Annotation(
        task_id=task.id,
        project_id=project.id,
        user_id=user.id,
        annotation_type="video_track_bbox",
        class_name="car",
        geometry={
            "type": "video_track_bbox",
            "track_id": "trk_join_overlap",
            "keyframes": [
                {
                    "frame_index": 2,
                    "bbox": {"x": 0.6, "y": 0.2, "w": 0.2, "h": 0.2},
                    "source": "manual",
                },
            ],
        },
    )
    mixed = Annotation(
        task_id=task.id,
        project_id=project.id,
        user_id=user.id,
        annotation_type="video_track_bbox",
        class_name="person",
        geometry={
            "type": "video_track_bbox",
            "track_id": "trk_join_person_tail",
            "keyframes": [
                {
                    "frame_index": 6,
                    "bbox": {"x": 0.6, "y": 0.2, "w": 0.2, "h": 0.2},
                    "source": "manual",
                },
            ],
        },
    )
    db_session.add_all([overlap, mixed])
    await db_session.flush()

    overlap_resp = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/video/track-compositions",
        json={
            "operation": "join_tracks",
            "annotation_ids": [str(first.id), str(overlap.id)],
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    mixed_resp = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/video/track-compositions",
        json={
            "operation": "join_tracks",
            "annotation_ids": [str(first.id), str(mixed.id)],
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert overlap_resp.status_code == 400
    assert (
        overlap_resp.json()["detail"] == "join_tracks requires non-overlapping tracks"
    )
    assert mixed_resp.status_code == 400
    assert (
        mixed_resp.json()["detail"] == "join_tracks requires tracks with the same class"
    )


async def test_video_track_composition_rejects_annotation_from_other_task(
    db_session,
    httpx_client,
    super_admin,
):
    user, token = super_admin
    project, _, _ = await _create_video_export_fixture(db_session, user)
    task, track = await _video_fixture_task_and_track(db_session, project)
    other = (
        await db_session.execute(
            select(Annotation).where(
                Annotation.project_id == project.id,
                Annotation.task_id != task.id,
                Annotation.annotation_type == "video_track_bbox",
            )
        )
    ).scalar_one()

    resp = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/video/track-compositions",
        json={
            "operation": "merge_tracks",
            "annotation_ids": [str(track.id), str(other.id)],
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Annotation does not belong to this task"


async def test_video_track_composition_requires_task_visibility(
    db_session,
    httpx_client,
    super_admin,
    annotator,
):
    user, _ = super_admin
    annotator_user, annotator_token = annotator
    project, _, _ = await _create_video_export_fixture(db_session, user)
    task, track = await _video_fixture_task_and_track(db_session, project)
    # Valid membership so the account passes project access, while the
    # unassigned task must still be hidden from this annotator.
    await create_membership(
        db_session,
        project_id=project.id,
        user_id=annotator_user.id,
        role="annotator",
        assigned_by=user.id,
    )
    await db_session.commit()

    resp = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/video/track-compositions",
        json={
            "operation": "split_track",
            "annotation_ids": [str(track.id)],
            "frame_index": 1,
        },
        headers={"Authorization": f"Bearer {annotator_token}"},
    )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Task not found"


async def test_video_export_include_attributes_false_removes_schema_and_attrs(
    db_session,
    super_admin,
):
    user, _ = super_admin
    project, _, _ = await _create_video_export_fixture(db_session, user)

    body = json.loads(
        await ExportService(db_session).export_coco(
            project.id, include_attributes=False
        )
    )
    assert "attribute_schema" not in body["project"]
    assert "attributes" not in body["tracks"][0]
    assert "attributes" not in body["video_bbox"][0]


@pytest.mark.parametrize("format", ["yolo", "voc"])
async def test_video_project_yolo_voc_export_raises_unsupported(
    db_session,
    super_admin,
    format,
):
    user, _ = super_admin
    project, _, _ = await _create_video_export_fixture(db_session, user)

    svc = ExportService(db_session)
    method = svc.export_yolo if format == "yolo" else svc.export_voc
    with pytest.raises(UnsupportedExportError) as exc:
        await method(project.id)
    assert str(exc.value) == (
        f"video-track projects do not support {format.upper()} export yet"
    )


async def test_video_mm_coco_export_raises_unsupported(
    db_session,
    super_admin,
):
    user, _ = super_admin
    project, _, _ = await _create_video_export_fixture(db_session, user)
    project.type_key = "video-mm"
    project.type_label = "视频 · 多模态"
    await db_session.flush()

    with pytest.raises(UnsupportedExportError) as exc:
        await ExportService(db_session).export_coco(project.id)
    assert str(exc.value) == (
        "Video annotation export is not supported for video-mm projects"
    )
