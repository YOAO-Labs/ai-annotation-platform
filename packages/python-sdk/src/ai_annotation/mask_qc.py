"""Mask QC runs, issue review, comparisons, and repair lifecycle."""

from __future__ import annotations

from uuid import UUID

from ai_annotation._http import HttpTransport, _drop_none
from ai_annotation.mask_qc_models import (
    CocoRleMaskContent,
    MaskCompareBaseline,
    MaskCompareResult,
    MaskQCSeverity,
    MaskQCIssue,
    MaskQCIssuePage,
    MaskQCIssuePatch,
    MaskQCIssuePatchStatus,
    MaskQCIssueStatus,
    MaskQCRun,
    MaskQCRunRequest,
    MaskRepairBatch,
    MaskRepairDryRunRequest,
    MaskRepairDryRunResult,
    MaskRepairExecuteRequest,
    MaskRepairRollbackRequest,
    TaskMaskQCSummary,
)

IdLike = str | UUID


class MaskQC:
    """Mask QC reads, review writes, and version-safe repair controls.

    Project-wide runs and repairs require project review authority; selected task
    scopes are checked against task visibility. Compare and content routes also
    require the server's ``annotations:read`` API-key scope, while repair writes
    require ``annotations:write``. The server remains authoritative for all of
    these checks.
    """

    def __init__(self, http: HttpTransport):
        self._http = http

    def create_run(self, project_id: IdLike, body: MaskQCRunRequest) -> MaskQCRun:
        """Create one asynchronous run and preserve its returned run/job IDs.

        A 202 response contains the Mask QC domain run. Project scope requires a
        project owner; selected task/annotation scopes require visible review
        authority. ``async_job_id`` is a separate handle for ``client.jobs``;
        this method does not wait or retry.
        """
        response = self._http.request(
            "POST",
            f"/projects/{project_id}/mask-qc/runs",
            json=body.model_dump(mode="json", by_alias=True, exclude_unset=True),
        )
        return MaskQCRun.model_validate(response.json())

    def list_issues(
        self,
        project_id: IdLike,
        *,
        task_id: IdLike | None = None,
        annotation_id: IdLike | None = None,
        status: MaskQCIssueStatus | None = None,
        severity: MaskQCSeverity | None = None,
        code: str | None = None,
        frame: int | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> MaskQCIssuePage:
        """List only visible review issues; ``cursor`` is opaque and reused verbatim."""
        response = self._http.request(
            "GET",
            f"/projects/{project_id}/mask-qc/issues",
            params=_drop_none(
                {
                    "task_id": str(task_id) if task_id is not None else None,
                    "annotation_id": (
                        str(annotation_id) if annotation_id is not None else None
                    ),
                    "status": status,
                    "severity": severity,
                    "code": code,
                    "frame": frame,
                    "limit": limit,
                    "cursor": cursor,
                }
            ),
        )
        return MaskQCIssuePage.model_validate(response.json())

    def task_summary(self, task_id: IdLike) -> TaskMaskQCSummary:
        """Read a visible task's QC state with review authority."""
        response = self._http.request("GET", f"/tasks/{task_id}/mask-qc/summary")
        return TaskMaskQCSummary.model_validate(response.json())

    def compare(
        self,
        annotation_id: IdLike,
        *,
        annotation_version: int,
        baseline: MaskCompareBaseline,
        frame_index: int | None = None,
        candidate_job_id: IdLike | None = None,
        candidate_job_revision: int | None = None,
        candidate_digest: str | None = None,
        candidate_instance_id: str | None = None,
    ) -> MaskCompareResult:
        """Compare an exact annotation version with a selected baseline.

        Requires ``annotations:read`` plus visible review access. Preserve the response's
        ``content_path`` and digest. For a side whose
        ``source`` is ``tracker_candidate``, use
        ``client.video_tracker.get_mask_content(side.candidate_job_id, side.digest)``.
        """
        response = self._http.request(
            "GET",
            f"/annotations/{annotation_id}/mask-compare",
            params=_drop_none(
                {
                    "annotation_version": annotation_version,
                    "baseline": baseline,
                    "frame_index": frame_index,
                    "candidate_job_id": (
                        str(candidate_job_id) if candidate_job_id is not None else None
                    ),
                    "candidate_job_revision": candidate_job_revision,
                    "candidate_digest": candidate_digest,
                    "candidate_instance_id": candidate_instance_id,
                }
            ),
        )
        return MaskCompareResult.model_validate(response.json())

    def compare_content(
        self,
        annotation_id: IdLike,
        *,
        annotation_version: int,
        digest: str,
        frame_index: int | None = None,
    ) -> CocoRleMaskContent:
        """Fetch COCO RLE for the exact annotation version/digest (annotations:read)."""
        response = self._http.request(
            "GET",
            f"/annotations/{annotation_id}/mask-compare/content",
            params=_drop_none(
                {
                    "annotation_version": annotation_version,
                    "digest": digest,
                    "frame_index": frame_index,
                }
            ),
        )
        return CocoRleMaskContent.model_validate(response.json())

    def issue_region_content(
        self, issue_id: IdLike, *, digest: str
    ) -> CocoRleMaskContent:
        """Fetch visible issue-region RLE only for its exact digest (annotations:read)."""
        response = self._http.request(
            "GET",
            f"/mask-qc/issues/{issue_id}/region-content",
            params={"digest": digest},
        )
        return CocoRleMaskContent.model_validate(response.json())

    def get_issue(self, issue_id: IdLike) -> MaskQCIssue:
        response = self._http.request("GET", f"/mask-qc/issues/{issue_id}")
        return MaskQCIssue.model_validate(response.json())

    def patch_issue(
        self, issue_id: IdLike, status: MaskQCIssuePatchStatus
    ) -> MaskQCIssue:
        """Set a visible, non-stale issue status with review authority; conflicts pass through."""
        body = MaskQCIssuePatch(status=status)
        response = self._http.request(
            "PATCH",
            f"/mask-qc/issues/{issue_id}",
            json=body.model_dump(mode="json", by_alias=True),
        )
        return MaskQCIssue.model_validate(response.json())

    def dry_run_repairs(
        self, project_id: IdLike, body: MaskRepairDryRunRequest
    ) -> MaskRepairDryRunResult:
        """Create a receipt-backed plan with annotations:write and review.write.

        This does not mutate annotations. Skipped actions stay in the returned plan.
        """
        response = self._http.request(
            "POST",
            f"/projects/{project_id}/mask-qc/repairs:dry-run",
            json=body.model_dump(mode="json", by_alias=True, exclude_unset=True),
        )
        return MaskRepairDryRunResult.model_validate(response.json())

    def execute_repairs(
        self, project_id: IdLike, body: MaskRepairExecuteRequest
    ) -> MaskRepairBatch:
        """Execute an actor-owned dry-run receipt/digest once; no refresh or retry.

        This is a 202 repair batch with its own domain status and async job ID. A stale,
        expired, reused, or invalid receipt remains a server error/conflict.
        """
        response = self._http.request(
            "POST",
            f"/projects/{project_id}/mask-qc/repairs",
            json=body.model_dump(mode="json", by_alias=True, exclude_unset=True),
        )
        return MaskRepairBatch.model_validate(response.json())

    def get_repair_batch(self, repair_id: IdLike) -> MaskRepairBatch:
        """Read a repair batch only when its affected task scope is visible."""
        response = self._http.request("GET", f"/mask-qc/repairs/{repair_id}")
        return MaskRepairBatch.model_validate(response.json())

    def resume_repairs(self, repair_id: IdLike) -> MaskRepairBatch:
        """Resume only a visible failed/partial repair batch with write scope."""
        response = self._http.request(
            "POST", f"/mask-qc/repairs/{repair_id}/resume", json={}
        )
        return MaskRepairBatch.model_validate(response.json())

    def rollback_repairs(
        self,
        repair_id: IdLike,
        body: MaskRepairRollbackRequest,
    ) -> MaskRepairBatch:
        """Request rollback for the exact result digest; eligibility is server-owned.

        The API accepts completed/partial batches only during the rollback window. It
        returns a 202 batch/job response; the SDK does not refresh or replay conflicts.
        """
        response = self._http.request(
            "POST",
            f"/mask-qc/repairs/{repair_id}/rollback",
            json=body.model_dump(mode="json", by_alias=True, exclude_unset=True),
        )
        return MaskRepairBatch.model_validate(response.json())
