import json
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from ai_annotation.errors import AAPError, APIStatusError
from ai_annotation.video_models import (
    VideoChapterCreate,
    VideoChapterOut,
    VideoChapterUpdate,
    VideoChunkOut,
    VideoFrameOut,
    VideoFramePrefetchRequest,
    VideoFrameRetryRequest,
)
from ai_annotation.videos import TaskVideos, Videos

from .conftest import API

ITEM_ID = str(uuid4())
TASK_ID = str(uuid4())
SEGMENT_ID = str(uuid4())
CHAPTER_ID = str(uuid4())

SEGMENT = {
    "id": SEGMENT_ID,
    "segment_index": 0,
    "start_frame": 0,
    "end_frame": 9,
    "work_start_frame": 0,
    "work_end_frame": 9,
    "status": "open",
}
VIDEO_FRAME = {
    "frame_index": 3,
    "width": 512,
    "format": "webp",
    "status": "ready",
    "url": "https://media.example/frame.webp?signature=private",
}
VIDEO_CHUNK = {
    "chunk_id": 0,
    "start_frame": 0,
    "end_frame": 9,
    "status": "ready",
    "url": "https://media.example/chunk.mp4?signature=private",
    "byte_size": 4,
}
CHAPTER = {
    "id": CHAPTER_ID,
    "dataset_item_id": ITEM_ID,
    "start_frame": 0,
    "end_frame": 8,
    "title": "opening",
    "metadata": {},
    "source": "manual",
    "created_at": "2026-10-17T00:00:00Z",
}


class UnconsumedStream(httpx.SyncByteStream):
    def __iter__(self):
        raise AssertionError("unexpected status body must not be buffered")

    def close(self) -> None:
        pass


def test_dataset_item_video_reads_use_item_paths_and_preserve_pending(
    client, respx_mock
):
    videos = Videos(client._http)
    respx_mock.get(f"{API}/videos/{ITEM_ID}/manifest").mock(
        return_value=httpx.Response(
            200,
            json={
                "dataset_item_id": ITEM_ID,
                "video_url": "https://media.example/video.mp4",
                "chunks_manifest_url": "/chunks",
                "frame_timetable_url": "/timetable",
                "frame_service_base": "/frames",
                "chunk_size_frames": 10,
            },
        )
    )
    respx_mock.get(f"{API}/videos/{ITEM_ID}/segments").mock(
        return_value=httpx.Response(
            200,
            json={
                "dataset_item_id": ITEM_ID,
                "segment_size_frames": 10,
                "segments": [SEGMENT],
            },
        )
    )
    timetable = respx_mock.get(f"{API}/videos/{ITEM_ID}/frame-timetable").mock(
        return_value=httpx.Response(
            200,
            json={
                "task_id": TASK_ID,
                "source": "ffprobe",
                "frames": [{"frame_index": 0, "pts_ms": 0, "is_keyframe": True}],
            },
        )
    )
    chunks = respx_mock.get(f"{API}/videos/{ITEM_ID}/chunks").mock(
        return_value=httpx.Response(
            200,
            json={
                "dataset_item_id": ITEM_ID,
                "chunk_size_frames": 10,
                "chunks": [VIDEO_CHUNK],
            },
        )
    )
    pending = respx_mock.get(f"{API}/videos/{ITEM_ID}/chunks/2").mock(
        return_value=httpx.Response(
            202,
            headers={"Retry-After": "3"},
            json={
                "chunk_id": 2,
                "start_frame": 20,
                "end_frame": 29,
                "status": "pending",
                "retry_after": 3,
            },
        )
    )
    frame = respx_mock.get(f"{API}/videos/{ITEM_ID}/frames/3").mock(
        return_value=httpx.Response(
            202,
            json={**VIDEO_FRAME, "status": "pending", "url": None, "retry_after": 3},
        )
    )

    assert str(videos.manifest(ITEM_ID).dataset_item_id) == ITEM_ID
    assert str(videos.segments(ITEM_ID).segments[0].id) == SEGMENT_ID
    assert (
        videos.frame_timetable(ITEM_ID, from_frame=0, to_frame=10).frames[0].frame_index
        == 0
    )
    assert dict(timetable.calls.last.request.url.params) == {"from": "0", "to": "10"}
    assert videos.chunks(ITEM_ID, from_frame=10, to_frame=19).chunks[0].chunk_id == 0
    assert dict(chunks.calls.last.request.url.params) == {
        "from_frame": "10",
        "to_frame": "19",
    }
    assert videos.chunk(ITEM_ID, 2).status == "pending"
    assert pending.call_count == 1
    assert videos.frame(ITEM_ID, 3).status == "pending"
    assert frame.call_count == 1


def test_dataset_item_video_requests_and_chapter_parent_path(client, respx_mock):
    videos = Videos(client._http)
    samples = respx_mock.get(f"{API}/videos/{ITEM_ID}/chunks/0/samples").mock(
        return_value=httpx.Response(
            200,
            json={
                "dataset_item_id": ITEM_ID,
                "chunk_id": 0,
                "codec_string": "avc1.4d001e",
                "width": 640,
                "height": 360,
                "samples": [],
            },
        )
    )
    prefetch = respx_mock.post(f"{API}/videos/{ITEM_ID}/frames:prefetch").mock(
        return_value=httpx.Response(
            200, json={"dataset_item_id": ITEM_ID, "frames": []}
        )
    )
    retry = respx_mock.post(f"{API}/videos/{ITEM_ID}/frames:retry").mock(
        return_value=httpx.Response(
            200, json={"dataset_item_id": ITEM_ID, "frames": []}
        )
    )
    chapter_list = respx_mock.get(f"{API}/videos/{ITEM_ID}/chapters").mock(
        return_value=httpx.Response(200, json={"chapters": [CHAPTER]})
    )
    create = respx_mock.post(f"{API}/videos/{ITEM_ID}/chapters").mock(
        return_value=httpx.Response(201, json=CHAPTER)
    )
    update = respx_mock.patch(f"{API}/videos/{ITEM_ID}/chapters/{CHAPTER_ID}").mock(
        return_value=httpx.Response(200, json={**CHAPTER, "title": "updated"})
    )
    delete = respx_mock.delete(f"{API}/videos/{ITEM_ID}/chapters/{CHAPTER_ID}").mock(
        return_value=httpx.Response(204)
    )

    assert videos.chunk_samples(ITEM_ID, 0).codec_string == "avc1.4d001e"
    assert samples.called
    videos.prefetch_frames(ITEM_ID, [1, 2], width=320, format="jpeg")
    assert json.loads(prefetch.calls.last.request.content) == {
        "frame_indices": [1, 2],
        "width": 320,
        "format": "jpeg",
    }
    videos.retry_frames(ITEM_ID, [3], force=True)
    assert json.loads(retry.calls.last.request.content) == {
        "frame_indices": [3],
        "width": 512,
        "format": "webp",
        "force": True,
    }
    assert str(videos.chapters(ITEM_ID).chapters[0].id) == CHAPTER_ID
    chapter = videos.create_chapter(
        ITEM_ID, VideoChapterCreate(start_frame=0, end_frame=8, title=" opening ")
    )
    assert isinstance(chapter, VideoChapterOut)
    assert json.loads(create.calls.last.request.content) == {
        "start_frame": 0,
        "end_frame": 8,
        "title": "opening",
    }
    videos.update_chapter(ITEM_ID, CHAPTER_ID, VideoChapterUpdate(start_frame=1))
    assert json.loads(update.calls.last.request.content) == {"start_frame": 1}
    videos.delete_chapter(ITEM_ID, CHAPTER_ID)
    assert delete.called and chapter_list.called


def test_task_video_and_item_video_keep_distinct_parent_ids(client, respx_mock):
    task_videos = TaskVideos(client._http)
    task_manifest = respx_mock.get(f"{API}/tasks/{TASK_ID}/video/manifest").mock(
        return_value=httpx.Response(
            200,
            json={
                "task_id": TASK_ID,
                "video_url": "https://media.example/video.mp4",
                "metadata": {"fps": 25, "frame_count": 10},
            },
        )
    )
    task_manifest_v2 = respx_mock.get(f"{API}/tasks/{TASK_ID}/video/manifest-v2").mock(
        return_value=httpx.Response(
            200,
            json={
                "task_id": TASK_ID,
                "dataset_item_id": ITEM_ID,
                "video_url": "https://media.example/video.mp4",
                "chunks_manifest_url": "/chunks",
                "frame_timetable_url": "/timetable",
                "frame_service_base": "/frames",
                "chunk_size_frames": 10,
            },
        )
    )
    item_manifest = respx_mock.get(f"{API}/videos/{ITEM_ID}/manifest").mock(
        return_value=httpx.Response(
            200,
            json={
                "dataset_item_id": ITEM_ID,
                "video_url": "https://media.example/video.mp4",
                "chunks_manifest_url": "/chunks",
                "frame_timetable_url": "/timetable",
                "frame_service_base": "/frames",
                "chunk_size_frames": 10,
            },
        )
    )

    assert str(task_videos.manifest(TASK_ID).task_id) == TASK_ID
    assert str(task_videos.manifest_v2(TASK_ID).task_id) == TASK_ID
    assert str(Videos(client._http).manifest(ITEM_ID).dataset_item_id) == ITEM_ID
    assert task_manifest.called and task_manifest_v2.called and item_manifest.called


def test_task_video_media_routes_use_task_scoped_paths(client, respx_mock):
    task_videos = TaskVideos(client._http)
    timetable = respx_mock.get(f"{API}/tasks/{TASK_ID}/video/frame-timetable").mock(
        return_value=httpx.Response(
            200,
            json={
                "task_id": TASK_ID,
                "source": "estimated",
                "frames": [],
            },
        )
    )
    chunks = respx_mock.get(f"{API}/tasks/{TASK_ID}/video/chunks").mock(
        return_value=httpx.Response(
            200,
            json={"dataset_item_id": ITEM_ID, "chunk_size_frames": 10, "chunks": []},
        )
    )
    chunk = respx_mock.get(f"{API}/tasks/{TASK_ID}/video/chunks/0").mock(
        return_value=httpx.Response(
            202,
            json={
                "chunk_id": 0,
                "start_frame": 0,
                "end_frame": 9,
                "status": "pending",
                "retry_after": 3,
            },
        )
    )
    frame = respx_mock.get(f"{API}/tasks/{TASK_ID}/video/frames/2").mock(
        return_value=httpx.Response(
            202,
            json={
                "frame_index": 2,
                "width": 320,
                "format": "jpeg",
                "status": "pending",
                "retry_after": 3,
            },
        )
    )
    prefetch = respx_mock.post(f"{API}/tasks/{TASK_ID}/video/frames:prefetch").mock(
        return_value=httpx.Response(
            200, json={"dataset_item_id": ITEM_ID, "frames": []}
        )
    )
    retry = respx_mock.post(f"{API}/tasks/{TASK_ID}/video/frames:retry").mock(
        return_value=httpx.Response(
            200, json={"dataset_item_id": ITEM_ID, "frames": []}
        )
    )

    task_videos.frame_timetable(TASK_ID)
    task_videos.chunks(TASK_ID, from_frame=0, to_frame=9)
    assert task_videos.chunk(TASK_ID, 0).status == "pending"
    assert task_videos.frame(TASK_ID, 2, format="jpeg", width=320).status == "pending"
    task_videos.prefetch_frames(TASK_ID, [2], format="jpeg", width=320)
    task_videos.retry_frames(TASK_ID, [2], format="jpeg", width=320, force=True)

    assert all(
        route.called for route in (timetable, chunks, chunk, frame, prefetch, retry)
    )
    assert dict(chunks.calls.last.request.url.params) == {
        "from_frame": "0",
        "to_frame": "9",
    }
    assert json.loads(retry.calls.last.request.content) == {
        "frame_indices": [2],
        "width": 320,
        "format": "jpeg",
        "force": True,
    }


@pytest.mark.parametrize(
    ("action", "path"),
    [
        ("claim_segment", "claim"),
        ("heartbeat_segment", "heartbeat"),
        ("release_segment", "release"),
        ("submit_segment", "submit"),
        ("reopen_segment", "reopen"),
        ("unassign_segment", "unassign"),
    ],
)
def test_task_segment_actions_use_task_and_segment_ids(
    client, respx_mock, action, path
):
    route = respx_mock.post(
        f"{API}/tasks/{TASK_ID}/video/segments/{SEGMENT_ID}:{path}"
    ).mock(return_value=httpx.Response(200, json=SEGMENT))
    result = getattr(TaskVideos(client._http), action)(TASK_ID, SEGMENT_ID)
    assert str(result.id) == SEGMENT_ID
    assert route.called


def test_frame_and_chunk_download_require_ready_descriptor(
    client, respx_mock, tmp_path
):
    videos = Videos(client._http)
    with pytest.raises(AAPError, match="not ready"):
        videos.download_frame(
            VideoFrameOut(
                frame_index=3,
                width=512,
                format="webp",
                status="pending",
                retry_after=3,
            ),
            tmp_path / "frame.webp",
        )
    with pytest.raises(AAPError, match="not ready"):
        videos.download_chunk(
            VideoChunkOut(chunk_id=0, start_frame=0, end_frame=9, status="failed"),
            tmp_path / "chunk.mp4",
        )
    assert not respx_mock.calls


def test_download_rejects_relative_url_without_sending_api_key(
    client, respx_mock, tmp_path
):
    with pytest.raises(AAPError, match="absolute"):
        Videos(client._http).download_frame(
            VideoFrameOut(
                frame_index=3,
                width=512,
                format="webp",
                status="ready",
                url="/signed/frame.webp",
            ),
            tmp_path / "frame.webp",
        )
    assert not respx_mock.calls


def test_download_uses_bare_client_and_verifies_chunk_size(
    client, respx_mock, tmp_path
):
    def check_auth(request):
        assert "authorization" not in request.headers
        return httpx.Response(200, content=b"data")

    respx_mock.get("https://media.example/chunk.mp4?signature=private").mock(
        side_effect=check_auth
    )
    path = Videos(client._http).download_chunk(
        VideoChunkOut.model_validate(VIDEO_CHUNK), tmp_path / "chunk.mp4"
    )
    assert path.read_bytes() == b"data"


@pytest.mark.parametrize("status", [206, 302])
def test_full_download_rejects_partial_or_redirect_and_preserves_destination(
    client, respx_mock, tmp_path, status
):
    dest = tmp_path / "chunk.mp4"
    dest.write_bytes(b"old-complete-file")
    respx_mock.get("https://media.example/chunk.mp4?signature=private").mock(
        return_value=httpx.Response(status, stream=UnconsumedStream())
    )

    with pytest.raises(APIStatusError) as exc_info:
        Videos(client._http).download_chunk(
            VideoChunkOut.model_validate(VIDEO_CHUNK), dest
        )

    assert exc_info.value.status_code == status
    assert dest.read_bytes() == b"old-complete-file"


def test_request_models_keep_service_bounds():
    with pytest.raises(ValidationError):
        VideoFramePrefetchRequest(frame_indices=[])
    with pytest.raises(ValidationError):
        VideoFramePrefetchRequest(frame_indices=list(range(501)))
    with pytest.raises(ValidationError):
        VideoFrameRetryRequest(frame_indices=list(range(501)))
    with pytest.raises(ValidationError):
        VideoChapterCreate(start_frame=9, end_frame=1, title="bad")
    with pytest.raises(ValidationError):
        VideoChapterCreate(start_frame=0, end_frame=1, title="  ")


def test_chapter_response_keeps_server_fields_authoritative():
    chapter = VideoChapterOut.model_validate(
        {
            **CHAPTER,
            "frame_step": 2,
            "source": "manual",
            "metadata": {"frame_step": 4, "source": "sampled"},
        }
    )
    assert chapter.frame_step == 2
    assert chapter.source == "manual"
