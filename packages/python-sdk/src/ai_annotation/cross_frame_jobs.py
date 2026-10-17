"""User-owned async jobs for point-cloud cross-frame annotation propagation."""

from __future__ import annotations

from uuid import UUID

from ai_annotation._http import HttpTransport
from ai_annotation.models import Job, JobPage
from ai_annotation.task_workflow_models import CrossFrameJobCreateRequest

IdLike = str | UUID


class CrossFrameJobs:
    def __init__(self, http: HttpTransport):
        self._http = http

    def list(self, task_id: IdLike, *, limit: int = 20) -> JobPage:
        resp = self._http.request(
            "GET", f"/tasks/{task_id}/cross-frame-jobs", params={"limit": limit}
        )
        return JobPage.model_validate(resp.json())

    def create(self, task_id: IdLike, body: CrossFrameJobCreateRequest) -> Job:
        resp = self._http.request(
            "POST",
            f"/tasks/{task_id}/cross-frame-jobs",
            json=body.model_dump(mode="json", by_alias=True, exclude_unset=True),
        )
        return Job.model_validate(resp.json())

    def retry_failed(self, task_id: IdLike, job_id: IdLike) -> Job:
        resp = self._http.request(
            "POST",
            f"/tasks/{task_id}/cross-frame-jobs/{job_id}/retry-failed",
        )
        return Job.model_validate(resp.json())
