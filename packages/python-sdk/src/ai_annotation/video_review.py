"""Synchronous video mask editing, track-quality review and track conversions."""

from __future__ import annotations

from uuid import UUID

from ai_annotation._http import HttpTransport
from ai_annotation.models import Annotation
from ai_annotation.video_review_models import (
    CocoRleContent,
    CocoRleMaskRef,
    VideoMaskKeyframeOperationRequest,
    VideoMaskKeyframeSaveRequest,
    VideoMaskContentUploadRequest,
    VideoTrackCompositionRequest,
    VideoTrackCompositionResponse,
    VideoTrackConvertToBboxesRequest,
    VideoTrackConvertToBboxesResponse,
    VideoTrackQualityAcceptRequest,
    VideoTrackQualityRun,
    VideoTrackQualityRunRequest,
)

IdLike = str | UUID


def _body(request) -> dict:
    return request.model_dump(mode="json", by_alias=True, exclude_unset=True)


class VideoReview:
    """Task-scoped video review operations with version and digest preconditions."""

    def __init__(self, http: HttpTransport):
        self._http = http

    def upload_mask_content(
        self, task_id: IdLike, body: VideoMaskContentUploadRequest
    ) -> CocoRleMaskRef:
        """Upload validated RLE JSON and return its immutable task-owned reference."""
        response = self._http.request(
            "POST",
            f"/tasks/{task_id}/mask-content",
            json=body.model_dump(mode="json", by_alias=True, exclude_none=True),
        )
        return CocoRleMaskRef.model_validate(response.json())

    def get_annotation_mask_content(self, annotation_id: IdLike) -> CocoRleContent:
        """Fetch a static mask as JSON COCO RLE without conditional-cache headers."""
        response = self._http.request(
            "GET", f"/annotations/{annotation_id}/mask-content"
        )
        return CocoRleContent.model_validate(response.json())

    def get_annotation_frame_mask_content(
        self, annotation_id: IdLike, frame_index: int
    ) -> CocoRleContent:
        """Fetch the annotation mask or resolved video-track mask for one frame."""
        if frame_index < 0:
            raise ValueError("frame_index must be non-negative")
        response = self._http.request(
            "GET", f"/annotations/{annotation_id}/mask-content/{frame_index}"
        )
        return CocoRleContent.model_validate(response.json())

    def get_prediction_mask_content(
        self,
        task_id: IdLike,
        prediction_id: IdLike,
        shape_index: int,
        frame_index: int,
    ) -> CocoRleContent:
        """Fetch a prediction track's resolved mask for one frame as JSON RLE."""
        if shape_index < 0 or frame_index < 0:
            raise ValueError("shape_index and frame_index must be non-negative")
        response = self._http.request(
            "GET",
            f"/tasks/{task_id}/predictions/{prediction_id}/mask-content/{shape_index}/{frame_index}",
        )
        return CocoRleContent.model_validate(response.json())

    def save_mask_keyframe(
        self,
        task_id: IdLike,
        annotation_id: IdLike,
        frame_index: int,
        expected_version: int,
        body: VideoMaskKeyframeSaveRequest,
    ) -> Annotation:
        """Write one mask keyframe using the annotation version as If-Match."""
        if frame_index < 0:
            raise ValueError("frame_index must be non-negative")
        if expected_version < 1:
            raise ValueError("expected_version must be positive")
        response = self._http.request(
            "PUT",
            f"/tasks/{task_id}/video/tracks/{annotation_id}/mask-keyframes/{frame_index}",
            json=_body(body),
            headers={"If-Match": f'W/"{expected_version}"'},
        )
        return Annotation.model_validate(response.json())

    def operate_mask_keyframe(
        self,
        task_id: IdLike,
        annotation_id: IdLike,
        frame_index: int,
        expected_version: int,
        body: VideoMaskKeyframeOperationRequest,
    ) -> Annotation:
        """Apply a keyframe operation against the exact annotation version."""
        if frame_index < 0:
            raise ValueError("frame_index must be non-negative")
        if expected_version < 1:
            raise ValueError("expected_version must be positive")
        response = self._http.request(
            "PATCH",
            f"/tasks/{task_id}/video/tracks/{annotation_id}/mask-keyframes/{frame_index}",
            json=_body(body),
            headers={"If-Match": f'W/"{expected_version}"'},
        )
        return Annotation.model_validate(response.json())

    def list_track_quality_runs(self, task_id: IdLike) -> list[VideoTrackQualityRun]:
        response = self._http.request("GET", f"/tasks/{task_id}/video/track-quality")
        return [VideoTrackQualityRun.model_validate(item) for item in response.json()]

    def run_track_quality(
        self, task_id: IdLike, body: VideoTrackQualityRunRequest
    ) -> VideoTrackQualityRun:
        response = self._http.request(
            "POST",
            f"/tasks/{task_id}/video/track-quality/run",
            json=_body(body),
        )
        return VideoTrackQualityRun.model_validate(response.json())

    def get_track_quality_run(
        self, task_id: IdLike, run_id: IdLike
    ) -> VideoTrackQualityRun:
        response = self._http.request(
            "GET", f"/tasks/{task_id}/video/track-quality/{run_id}"
        )
        return VideoTrackQualityRun.model_validate(response.json())

    def accept_track_quality_run(
        self,
        task_id: IdLike,
        run_id: IdLike,
        body: VideoTrackQualityAcceptRequest,
    ) -> VideoTrackQualityRun:
        """Accept decisions only for the returned digest; stale runs stay conflicts."""
        response = self._http.request(
            "POST",
            f"/tasks/{task_id}/video/track-quality/{run_id}:accept",
            json=_body(body),
        )
        return VideoTrackQualityRun.model_validate(response.json())

    def compose_tracks(
        self, task_id: IdLike, body: VideoTrackCompositionRequest
    ) -> VideoTrackCompositionResponse:
        """Compose task annotations; delete_sources defaults to true in the API."""
        response = self._http.request(
            "POST",
            f"/tasks/{task_id}/annotations/video/track-compositions",
            json=_body(body),
        )
        return VideoTrackCompositionResponse.model_validate(response.json())

    def convert_track_to_bboxes(
        self,
        task_id: IdLike,
        annotation_id: IdLike,
        body: VideoTrackConvertToBboxesRequest,
    ) -> VideoTrackConvertToBboxesResponse:
        """Convert a bbox track to frame boxes; split can deactivate its source."""
        response = self._http.request(
            "POST",
            f"/tasks/{task_id}/annotations/{annotation_id}/video/convert-to-bboxes",
            json=_body(body),
        )
        return VideoTrackConvertToBboxesResponse.model_validate(response.json())
