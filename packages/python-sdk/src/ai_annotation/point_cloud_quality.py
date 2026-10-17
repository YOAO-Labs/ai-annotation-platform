"""Public REST resource for point-cloud quality scans, review, and governance."""

from __future__ import annotations

from uuid import UUID

from ai_annotation._http import HttpTransport, _drop_none
from ai_annotation.point_cloud_quality_models import (
    PointCloudQualityConfig,
    PointCloudQualityEvaluation,
    PointCloudQualityEvaluationPage,
    PointCloudQualityIssue,
    PointCloudQualityIssuePage,
    PointCloudQualityIssuePatch,
    PointCloudQualityRun,
    PointCloudQualityRunRequest,
)

IdLike = str | UUID


class PointCloudQuality:
    """Project-scoped quality scanning and reviewed-rule governance."""

    def __init__(self, http: HttpTransport):
        self._http = http

    def list_issues(
        self,
        project_id: IdLike,
        *,
        status: str | None = None,
        severity: str | None = None,
        code: str | None = None,
        scene_id: IdLike | None = None,
        task_id: IdLike | None = None,
        annotation_id: IdLike | None = None,
        scene_track_id: IdLike | None = None,
        frame: int | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> PointCloudQualityIssuePage:
        """List visible project issues by scene, task, annotation, track, or frame.

        The endpoint refreshes source evidence and may persist issues as ``stale``
        while serving a read. The server limits visibility to project reviewers
        and managers and returns one offset-paginated page.
        """
        if status is not None and status not in {
            "open",
            "resolved",
            "wont_fix",
            "stale",
        }:
            raise ValueError("status must be open, resolved, wont_fix, or stale")
        if severity is not None and severity not in {"blocker", "warning", "info"}:
            raise ValueError("severity must be blocker, warning, or info")
        if frame is not None and (type(frame) is not int or frame < 0):
            raise ValueError("frame must be a non-negative integer")
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit must be an integer between 1 and 200")
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        params = _drop_none(
            {
                "status": status,
                "severity": severity,
                "code": code,
                "scene_id": str(scene_id) if scene_id is not None else None,
                "task_id": str(task_id) if task_id is not None else None,
                "annotation_id": str(annotation_id)
                if annotation_id is not None
                else None,
                "scene_track_id": str(scene_track_id)
                if scene_track_id is not None
                else None,
                "frame": frame,
                "limit": limit,
                "offset": offset,
            }
        )
        resp = self._http.request(
            "GET",
            f"/projects/{project_id}/point-cloud-quality/issues",
            params=params,
        )
        return PointCloudQualityIssuePage.model_validate(resp.json())

    def create_run(
        self, project_id: IdLike, body: PointCloudQualityRunRequest
    ) -> PointCloudQualityRun:
        """Queue one scan and return its HTTP 202 receipt without polling.

        Project and scene scopes require project management authority. Task and
        annotation scopes require the caller's visible review scope. Optional
        ``expected_versions`` checks annotation IDs against their current versions;
        409 conflicts are returned unchanged and must be resolved by the caller.
        The API has no caller-supplied request ID; identical active or completed
        source/config snapshots reuse the existing run. A newly created run
        schedules worker reads of referenced point-cloud assets and uses captured
        camera calibration records; it may persist issues. No write retry is safe.
        """
        resp = self._http.request(
            "POST",
            f"/projects/{project_id}/point-cloud-quality/runs",
            json=body.model_dump(mode="json"),
        )
        return PointCloudQualityRun.model_validate(resp.json())

    def get_run(self, project_id: IdLike, run_id: IdLike) -> PointCloudQualityRun:
        """Read one run belonging to the project; task visibility is rechecked."""
        resp = self._http.request(
            "GET", f"/projects/{project_id}/point-cloud-quality/runs/{run_id}"
        )
        return PointCloudQualityRun.model_validate(resp.json())

    def get_issue(self, issue_id: IdLike) -> PointCloudQualityIssue:
        """Read a visible issue and refresh its source-staleness status.

        This GET can persist a transition to ``stale``. It takes no project ID;
        the API resolves the issue's project and task before applying review access.
        """
        resp = self._http.request("GET", f"/point-cloud-quality/issues/{issue_id}")
        return PointCloudQualityIssue.model_validate(resp.json())

    def patch_issue(
        self, issue_id: IdLike, body: PointCloudQualityIssuePatch
    ) -> PointCloudQualityIssue:
        """Review or reopen a current issue once; stale/busy rows return HTTP 409.

        The API records the reviewing user's identity and an audit event. There is
        no client-side source refresh or retry after a conflict.
        """
        resp = self._http.request(
            "PATCH",
            f"/point-cloud-quality/issues/{issue_id}",
            json=body.model_dump(mode="json"),
        )
        return PointCloudQualityIssue.model_validate(resp.json())

    def list_evaluations(
        self, project_id: IdLike, *, limit: int = 20, offset: int = 0
    ) -> PointCloudQualityEvaluationPage:
        """List frozen evaluations; reviewers may read, owners and super-admins may create/promote."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer between 1 and 100")
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        resp = self._http.request(
            "GET",
            f"/projects/{project_id}/point-cloud-quality/evaluations",
            params={"limit": limit, "offset": offset},
        )
        return PointCloudQualityEvaluationPage.model_validate(resp.json())

    def create_evaluation(
        self, project_id: IdLike, candidate_config: PointCloudQualityConfig
    ) -> PointCloudQualityEvaluation:
        """Freeze reviewed, non-stale samples against a candidate configuration.

        Requires project management authority and the candidate's config_revision
        to match the current project config. ``gate_status='promote'`` means the
        candidate passed evaluation only; it does not change project settings.
        This call creates a durable, audited evaluation; repeating it creates a
        separate evaluation. The response records baseline/candidate digests for
        the later server-side promotion compare-and-swap.
        """
        resp = self._http.request(
            "POST",
            f"/projects/{project_id}/point-cloud-quality/evaluations",
            json={"candidate_config": candidate_config.model_dump(mode="json")},
        )
        return PointCloudQualityEvaluation.model_validate(resp.json())

    def get_evaluation(
        self, project_id: IdLike, evaluation_id: IdLike
    ) -> PointCloudQualityEvaluation:
        """Get a frozen evaluation belonging to the project."""
        resp = self._http.request(
            "GET",
            f"/projects/{project_id}/point-cloud-quality/evaluations/{evaluation_id}",
        )
        return PointCloudQualityEvaluation.model_validate(resp.json())

    def promote_evaluation(
        self, project_id: IdLike, evaluation_id: IdLike
    ) -> PointCloudQualityEvaluation:
        """Promote an eligible evaluation once; project baseline revision/digest are CAS-checked.

        Only a project manager may promote. The API separately verifies the gate,
        project/evaluation relationship, and that the baseline config still matches;
        success changes project config and returns promotion identity/revision.
        A repeat or stale baseline returns 409; this method never retries it.
        """
        resp = self._http.request(
            "POST",
            f"/projects/{project_id}/point-cloud-quality/evaluations/{evaluation_id}/promote",
        )
        return PointCloudQualityEvaluation.model_validate(resp.json())
