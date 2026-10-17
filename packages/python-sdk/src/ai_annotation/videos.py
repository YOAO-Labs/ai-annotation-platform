"""Video asset, chapter, and task-scoped segment resources."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from ai_annotation._http import HttpTransport, _drop_none
from ai_annotation.errors import AAPError
from ai_annotation.video_models import (
    TaskVideoFrameTimetableResponse,
    TaskVideoManifestResponse,
    VideoChapterCreate,
    VideoChapterList,
    VideoChapterOut,
    VideoChapterUpdate,
    VideoChunkOut,
    VideoChunkSamplesResponse,
    VideoChunksResponse,
    VideoFrameOut,
    VideoFramePrefetchRequest,
    VideoFramePrefetchResponse,
    VideoFrameRetryRequest,
    VideoImageFormat,
    VideoManifestV2Response,
    VideoSegmentsResponse,
    VideoSegmentOut,
)

IdLike = str | UUID


def _require_signed_url(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        raise AAPError(
            "video asset URL must be absolute to avoid sending API credentials"
        )
    return url


class Videos:
    """Dataset-item video endpoints, including semantic chapter metadata."""

    def __init__(self, http: HttpTransport):
        self._http = http

    def manifest(self, dataset_item_id: IdLike) -> VideoManifestV2Response:
        resp = self._http.request("GET", f"/videos/{dataset_item_id}/manifest")
        return VideoManifestV2Response.model_validate(resp.json())

    def segments(self, dataset_item_id: IdLike) -> VideoSegmentsResponse:
        resp = self._http.request("GET", f"/videos/{dataset_item_id}/segments")
        return VideoSegmentsResponse.model_validate(resp.json())

    def frame_timetable(
        self,
        dataset_item_id: IdLike,
        *,
        from_frame: int | None = None,
        to_frame: int | None = None,
    ) -> TaskVideoFrameTimetableResponse:
        resp = self._http.request(
            "GET",
            f"/videos/{dataset_item_id}/frame-timetable",
            params=_drop_none({"from": from_frame, "to": to_frame}),
        )
        return TaskVideoFrameTimetableResponse.model_validate(resp.json())

    def chunks(
        self,
        dataset_item_id: IdLike,
        *,
        from_frame: int | None = None,
        to_frame: int | None = None,
    ) -> VideoChunksResponse:
        resp = self._http.request(
            "GET",
            f"/videos/{dataset_item_id}/chunks",
            params=_drop_none({"from_frame": from_frame, "to_frame": to_frame}),
        )
        return VideoChunksResponse.model_validate(resp.json())

    def chunk(self, dataset_item_id: IdLike, chunk_id: int) -> VideoChunkOut:
        resp = self._http.request("GET", f"/videos/{dataset_item_id}/chunks/{chunk_id}")
        return VideoChunkOut.model_validate(resp.json())

    def chunk_samples(
        self, dataset_item_id: IdLike, chunk_id: int
    ) -> VideoChunkSamplesResponse:
        resp = self._http.request(
            "GET", f"/videos/{dataset_item_id}/chunks/{chunk_id}/samples"
        )
        return VideoChunkSamplesResponse.model_validate(resp.json())

    def frame(
        self,
        dataset_item_id: IdLike,
        frame_index: int,
        *,
        format: VideoImageFormat = "webp",
        width: int = 512,
    ) -> VideoFrameOut:
        resp = self._http.request(
            "GET",
            f"/videos/{dataset_item_id}/frames/{frame_index}",
            params={"format": format, "w": width},
        )
        return VideoFrameOut.model_validate(resp.json())

    def prefetch_frames(
        self,
        dataset_item_id: IdLike,
        frame_indices: list[int],
        *,
        width: int = 512,
        format: VideoImageFormat = "webp",
    ) -> VideoFramePrefetchResponse:
        body = VideoFramePrefetchRequest(
            frame_indices=frame_indices, width=width, format=format
        )
        resp = self._http.request(
            "POST",
            f"/videos/{dataset_item_id}/frames:prefetch",
            json=body.model_dump(mode="json"),
        )
        return VideoFramePrefetchResponse.model_validate(resp.json())

    def retry_frames(
        self,
        dataset_item_id: IdLike,
        frame_indices: list[int] | None = None,
        *,
        width: int = 512,
        format: VideoImageFormat = "webp",
        force: bool = False,
    ) -> VideoFramePrefetchResponse:
        body = VideoFrameRetryRequest(
            frame_indices=frame_indices or [],
            width=width,
            format=format,
            force=force,
        )
        resp = self._http.request(
            "POST",
            f"/videos/{dataset_item_id}/frames:retry",
            json=body.model_dump(mode="json"),
        )
        return VideoFramePrefetchResponse.model_validate(resp.json())

    def download_frame(self, frame: VideoFrameOut, dest_path: str | Path) -> Path:
        """Download one ready frame; pending work is returned to the caller."""
        if frame.status != "ready" or not frame.url:
            raise AAPError(
                f"video frame {frame.frame_index} is not ready (status={frame.status})"
            )
        url = _require_signed_url(frame.url)
        return self._http.stream_download(url, Path(dest_path))

    def download_chunk(self, chunk: VideoChunkOut, dest_path: str | Path) -> Path:
        """Download one ready chunk; pending work is returned to the caller."""
        if chunk.status != "ready" or not chunk.url:
            raise AAPError(
                f"video chunk {chunk.chunk_id} is not ready (status={chunk.status})"
            )
        url = _require_signed_url(chunk.url)
        return self._http.stream_download(
            url,
            Path(dest_path),
            expected_size=chunk.byte_size,
        )

    def chapters(self, dataset_item_id: IdLike) -> VideoChapterList:
        resp = self._http.request("GET", f"/videos/{dataset_item_id}/chapters")
        return VideoChapterList.model_validate(resp.json())

    def create_chapter(
        self, dataset_item_id: IdLike, body: VideoChapterCreate
    ) -> VideoChapterOut:
        resp = self._http.request(
            "POST",
            f"/videos/{dataset_item_id}/chapters",
            json=body.model_dump(mode="json", exclude_unset=True),
        )
        return VideoChapterOut.model_validate(resp.json())

    def update_chapter(
        self,
        dataset_item_id: IdLike,
        chapter_id: IdLike,
        body: VideoChapterUpdate,
    ) -> VideoChapterOut:
        resp = self._http.request(
            "PATCH",
            f"/videos/{dataset_item_id}/chapters/{chapter_id}",
            json=body.model_dump(mode="json", exclude_unset=True),
        )
        return VideoChapterOut.model_validate(resp.json())

    def delete_chapter(self, dataset_item_id: IdLike, chapter_id: IdLike) -> None:
        self._http.request("DELETE", f"/videos/{dataset_item_id}/chapters/{chapter_id}")


class TaskVideos:
    """Task-scoped video endpoints; task IDs are never treated as item IDs."""

    def __init__(self, http: HttpTransport):
        self._http = http

    def manifest(self, task_id: IdLike) -> TaskVideoManifestResponse:
        resp = self._http.request("GET", f"/tasks/{task_id}/video/manifest")
        return TaskVideoManifestResponse.model_validate(resp.json())

    def manifest_v2(self, task_id: IdLike) -> VideoManifestV2Response:
        resp = self._http.request("GET", f"/tasks/{task_id}/video/manifest-v2")
        return VideoManifestV2Response.model_validate(resp.json())

    def segments(self, task_id: IdLike) -> VideoSegmentsResponse:
        resp = self._http.request("GET", f"/tasks/{task_id}/video/segments")
        return VideoSegmentsResponse.model_validate(resp.json())

    def frame_timetable(
        self,
        task_id: IdLike,
        *,
        from_frame: int | None = None,
        to_frame: int | None = None,
    ) -> TaskVideoFrameTimetableResponse:
        resp = self._http.request(
            "GET",
            f"/tasks/{task_id}/video/frame-timetable",
            params=_drop_none({"from": from_frame, "to": to_frame}),
        )
        return TaskVideoFrameTimetableResponse.model_validate(resp.json())

    def chunks(
        self,
        task_id: IdLike,
        *,
        from_frame: int | None = None,
        to_frame: int | None = None,
    ) -> VideoChunksResponse:
        resp = self._http.request(
            "GET",
            f"/tasks/{task_id}/video/chunks",
            params=_drop_none({"from_frame": from_frame, "to_frame": to_frame}),
        )
        return VideoChunksResponse.model_validate(resp.json())

    def chunk(self, task_id: IdLike, chunk_id: int) -> VideoChunkOut:
        resp = self._http.request("GET", f"/tasks/{task_id}/video/chunks/{chunk_id}")
        return VideoChunkOut.model_validate(resp.json())

    def frame(
        self,
        task_id: IdLike,
        frame_index: int,
        *,
        format: VideoImageFormat = "webp",
        width: int = 512,
    ) -> VideoFrameOut:
        resp = self._http.request(
            "GET",
            f"/tasks/{task_id}/video/frames/{frame_index}",
            params={"format": format, "w": width},
        )
        return VideoFrameOut.model_validate(resp.json())

    def prefetch_frames(
        self,
        task_id: IdLike,
        frame_indices: list[int],
        *,
        width: int = 512,
        format: VideoImageFormat = "webp",
    ) -> VideoFramePrefetchResponse:
        body = VideoFramePrefetchRequest(
            frame_indices=frame_indices, width=width, format=format
        )
        resp = self._http.request(
            "POST",
            f"/tasks/{task_id}/video/frames:prefetch",
            json=body.model_dump(mode="json"),
        )
        return VideoFramePrefetchResponse.model_validate(resp.json())

    def retry_frames(
        self,
        task_id: IdLike,
        frame_indices: list[int] | None = None,
        *,
        width: int = 512,
        format: VideoImageFormat = "webp",
        force: bool = False,
    ) -> VideoFramePrefetchResponse:
        body = VideoFrameRetryRequest(
            frame_indices=frame_indices or [],
            width=width,
            format=format,
            force=force,
        )
        resp = self._http.request(
            "POST",
            f"/tasks/{task_id}/video/frames:retry",
            json=body.model_dump(mode="json"),
        )
        return VideoFramePrefetchResponse.model_validate(resp.json())

    def download_frame(self, frame: VideoFrameOut, dest_path: str | Path) -> Path:
        """Download one ready frame; pending work is returned to the caller."""
        if frame.status != "ready" or not frame.url:
            raise AAPError(
                f"video frame {frame.frame_index} is not ready (status={frame.status})"
            )
        url = _require_signed_url(frame.url)
        return self._http.stream_download(url, Path(dest_path))

    def download_chunk(self, chunk: VideoChunkOut, dest_path: str | Path) -> Path:
        """Download one ready chunk; pending work is returned to the caller."""
        if chunk.status != "ready" or not chunk.url:
            raise AAPError(
                f"video chunk {chunk.chunk_id} is not ready (status={chunk.status})"
            )
        url = _require_signed_url(chunk.url)
        return self._http.stream_download(
            url,
            Path(dest_path),
            expected_size=chunk.byte_size,
        )

    def claim_segment(self, task_id: IdLike, segment_id: IdLike) -> VideoSegmentOut:
        resp = self._http.request(
            "POST",
            f"/tasks/{task_id}/video/segments/{segment_id}:claim",
        )
        return VideoSegmentOut.model_validate(resp.json())

    def heartbeat_segment(self, task_id: IdLike, segment_id: IdLike) -> VideoSegmentOut:
        resp = self._http.request(
            "POST",
            f"/tasks/{task_id}/video/segments/{segment_id}:heartbeat",
        )
        return VideoSegmentOut.model_validate(resp.json())

    def release_segment(self, task_id: IdLike, segment_id: IdLike) -> VideoSegmentOut:
        resp = self._http.request(
            "POST",
            f"/tasks/{task_id}/video/segments/{segment_id}:release",
        )
        return VideoSegmentOut.model_validate(resp.json())

    def submit_segment(self, task_id: IdLike, segment_id: IdLike) -> VideoSegmentOut:
        resp = self._http.request(
            "POST",
            f"/tasks/{task_id}/video/segments/{segment_id}:submit",
        )
        return VideoSegmentOut.model_validate(resp.json())

    def reopen_segment(self, task_id: IdLike, segment_id: IdLike) -> VideoSegmentOut:
        resp = self._http.request(
            "POST",
            f"/tasks/{task_id}/video/segments/{segment_id}:reopen",
        )
        return VideoSegmentOut.model_validate(resp.json())

    def unassign_segment(self, task_id: IdLike, segment_id: IdLike) -> VideoSegmentOut:
        resp = self._http.request(
            "POST",
            f"/tasks/{task_id}/video/segments/{segment_id}:unassign",
        )
        return VideoSegmentOut.model_validate(resp.json())
