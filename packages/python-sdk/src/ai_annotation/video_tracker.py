"""Client resource for video tracking jobs and staged candidate review."""

from __future__ import annotations

import re
from uuid import UUID

from ai_annotation._http import HttpTransport, _drop_none
from ai_annotation.video_tracker_models import (
    CocoRleMaskContent,
    VideoMaskCorrectionRequest,
    VideoTrackerCreateRequest,
    VideoTrackerDecisionRequest,
    VideoTrackerJob,
    VideoTrackerJobPreview,
    VideoTrackerJobsResponse,
    VideoTrackerStatus,
)

IdLike = str | UUID


class VideoTracker:
    """Video tracker jobs, candidate decisions, and task recovery endpoints.

    The job's ``completed`` status means compute finished; candidate review may
    still be pending. Job creation is a single POST and is never retried by the
    SDK because the API does not accept an idempotency key. Recover an uncertain
    result with ``list_active`` or ``list_reviewable`` for the task.
    """

    def __init__(self, http: HttpTransport):
        self._http = http

    def list_jobs(
        self,
        *,
        project_id: IdLike | None = None,
        status: VideoTrackerStatus | None = None,
        model_key: str | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> VideoTrackerJobsResponse:
        """List tracker jobs for project/super admins, with counts and cursor."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer between 1 and 100")
        if model_key is not None and len(model_key) > 80:
            raise ValueError("model_key must contain at most 80 characters")
        response = self._http.request(
            "GET",
            "/video-tracker-jobs",
            params=_drop_none(
                {
                    "project_id": str(project_id) if project_id is not None else None,
                    "status": status,
                    "model_key": model_key,
                    "cursor": cursor,
                    "limit": limit,
                }
            ),
        )
        return VideoTrackerJobsResponse.model_validate(response.json())

    def get_job(self, job_id: IdLike) -> VideoTrackerJob:
        response = self._http.request("GET", f"/video-tracker-jobs/{job_id}")
        return VideoTrackerJob.model_validate(response.json())

    def cancel_job(self, job_id: IdLike) -> VideoTrackerJob:
        """Cancel a tracker job; the API returns the updated job record."""
        response = self._http.request("DELETE", f"/video-tracker-jobs/{job_id}")
        return VideoTrackerJob.model_validate(response.json())

    def accept_job(self, job_id: IdLike) -> VideoTrackerJob:
        """Accept all staged candidates for an eligible tracking job."""
        response = self._http.request("POST", f"/video-tracker-jobs/{job_id}/accept")
        return VideoTrackerJob.model_validate(response.json())

    def discard_job(self, job_id: IdLike) -> VideoTrackerJob:
        """Discard all staged candidates without changing annotations."""
        response = self._http.request("POST", f"/video-tracker-jobs/{job_id}/discard")
        return VideoTrackerJob.model_validate(response.json())

    def decide_candidates(
        self, job_id: IdLike, body: VideoTrackerDecisionRequest
    ) -> VideoTrackerJob:
        """Apply a revision and source-version guarded candidate decision.

        A 409 remains a conflict; the SDK does not refresh versions or replay the
        decision because doing so could apply it to a different candidate set.
        """
        response = self._http.request(
            "POST",
            f"/video-tracker-jobs/{job_id}/decisions",
            json=body.model_dump(mode="json", by_alias=True, exclude_unset=True),
        )
        return VideoTrackerJob.model_validate(response.json())

    def get_preview(self, job_id: IdLike) -> VideoTrackerJobPreview:
        response = self._http.request("GET", f"/video-tracker-jobs/{job_id}/preview")
        return VideoTrackerJobPreview.model_validate(response.json())

    def get_mask_content(self, job_id: IdLike, sha256: str) -> CocoRleMaskContent:
        """Fetch materialized COCO RLE JSON, not a binary image or mask file."""
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError("sha256 must contain 64 lowercase hexadecimal characters")
        response = self._http.request(
            "GET", f"/video-tracker-jobs/{job_id}/mask-content/{sha256}"
        )
        return CocoRleMaskContent.model_validate(response.json())

    def list_active(self, task_id: IdLike) -> list[VideoTrackerJob]:
        """Recover queued or running jobs for a visible video task."""
        response = self._http.request(
            "GET", f"/tasks/{task_id}/video/tracker-jobs/active"
        )
        return [VideoTrackerJob.model_validate(item) for item in response.json()]

    def list_reviewable(self, task_id: IdLike) -> list[VideoTrackerJob]:
        """Recover jobs with staged candidates awaiting review for a task."""
        response = self._http.request(
            "GET", f"/tasks/{task_id}/video/tracker-jobs/reviewable"
        )
        return [VideoTrackerJob.model_validate(item) for item in response.json()]

    def track(
        self, task_id: IdLike, body: VideoTrackerCreateRequest
    ) -> VideoTrackerJob:
        """Create a task-level 202 job; preserve the response ID for recovery."""
        response = self._http.request(
            "POST",
            f"/tasks/{task_id}/video:track",
            json=body.model_dump(mode="json", by_alias=True, exclude_unset=True),
        )
        return VideoTrackerJob.model_validate(response.json())

    def propagate(
        self,
        task_id: IdLike,
        annotation_id: IdLike,
        body: VideoTrackerCreateRequest,
    ) -> VideoTrackerJob:
        """Create a job through the legacy single-track propagation route."""
        response = self._http.request(
            "POST",
            f"/tasks/{task_id}/video/tracks/{annotation_id}:propagate",
            json=body.model_dump(mode="json", by_alias=True, exclude_unset=True),
        )
        return VideoTrackerJob.model_validate(response.json())

    def create_correction(
        self,
        task_id: IdLike,
        annotation_id: IdLike,
        body: VideoMaskCorrectionRequest,
    ) -> VideoTrackerJob:
        """Create a versioned mask-correction job (HTTP 202)."""
        response = self._http.request(
            "POST",
            f"/tasks/{task_id}/video/tracks/{annotation_id}/correction-jobs",
            json=body.model_dump(mode="json", by_alias=True, exclude_unset=True),
        )
        return VideoTrackerJob.model_validate(response.json())
