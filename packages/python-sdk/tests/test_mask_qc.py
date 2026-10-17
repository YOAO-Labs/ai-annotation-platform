"""Mask QC SDK route and stale-input contracts (HTTP mocks only)."""

import json
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from ai_annotation.errors import ConflictError
from ai_annotation.mask_qc import MaskQC
from ai_annotation.mask_qc_models import (
    CocoRleMaskContent,
    MaskQCIssue,
    MaskQCIssuePage,
    MaskQCRun,
    MaskQCRunRequest,
    MaskRepairAction,
    MaskRepairBatch,
    MaskRepairDryRunRequest,
    MaskRepairDryRunResult,
    MaskRepairExecuteRequest,
    MaskRepairRollbackRequest,
    MaskCompareResult,
    TaskMaskQCSummary,
)

from .conftest import API

PROJECT_ID, TASK_ID, ANNOTATION_ID, ISSUE_ID, REPAIR_ID, RUN_ID, JOB_ID = [
    str(uuid4()) for _ in range(7)
]
ANNOTATION_2_ID = str(uuid4())
DIGEST = "a" * 64
PLAN_DIGEST = "b" * 64
RESULT_DIGEST = "c" * 64
RECEIPT = "mrp_" + "r" * 48


def _issue(**overrides) -> dict:
    body = {
        "id": ISSUE_ID,
        "run_id": RUN_ID,
        "last_seen_run_id": RUN_ID,
        "project_id": PROJECT_ID,
        "task_id": TASK_ID,
        "annotation_id": ANNOTATION_ID,
        "annotation_version": 4,
        "related_annotation_ids": [ANNOTATION_2_ID],
        "source_versions": {ANNOTATION_ID: 4, ANNOTATION_2_ID: 2},
        "code": "small_island",
        "severity": "warning",
        "status": "open",
        "effective_status": "open",
        "frame_start": 3,
        "frame_end": 5,
        "metric": {"component_pixels": 8},
        "threshold": {"component_pixels": 32},
        "region_bbox": {"x0": 0.1, "y0": 0.2, "x1": 0.4, "y1": 0.5},
        "region_mask_ref": {"sha256": DIGEST},
        "region_digest": DIGEST,
        "source": {"rule": "small_island"},
        "suggestion": "remove small island",
        "resolved_by_id": None,
        "resolved_at": None,
        "created_at": "2026-10-17T00:00:00Z",
        "updated_at": "2026-10-17T00:00:00Z",
    }
    return body | overrides


def _run(**overrides) -> dict:
    return {
        "id": RUN_ID,
        "project_id": PROJECT_ID,
        "async_job_id": JOB_ID,
        "status": "pending",
        "progress_pct": 0,
        "config_revision": 3,
        "config_digest": DIGEST,
        "source_snapshot_digest": PLAN_DIGEST,
        "source_versions": {ANNOTATION_ID: 4},
        "summary": {"selected_annotations": 1},
        "created_at": "2026-10-17T00:00:00Z",
        "completed_at": None,
        "reused": False,
    } | overrides


def _side(**overrides) -> dict:
    return {
        "annotation_id": ANNOTATION_ID,
        "annotation_version": 4,
        "frame_index": 3,
        "source": "previous_version",
        "state": "manual",
        "digest": DIGEST,
        "size": [2, 2],
        "content_path": f"/annotations/{ANNOTATION_ID}/mask-compare/content?annotation_version=4&digest={DIGEST}&frame_index=3",
        "candidate_job_id": None,
        "candidate_digest": None,
        "candidate_instance_id": None,
    } | overrides


def _compare() -> dict:
    return {
        "baseline_kind": "previous_version",
        "current": _side(source="manual"),
        "baseline": _side(annotation_version=3, source="previous_version"),
        "metrics": {
            "current_area_pixels": 4,
            "baseline_area_pixels": 3,
            "intersection_pixels": 3,
            "union_pixels": 4,
            "changed_pixels": 1,
            "added_pixels": 1,
            "removed_pixels": 0,
            "iou_numerator": 3,
            "iou_denominator": 4,
            "dice_numerator": 6,
            "dice_denominator": 7,
        },
        "loss": [],
    }


def _repair_dry_run() -> dict:
    return {
        "receipt": RECEIPT,
        "plan_digest": PLAN_DIGEST,
        "expires_at": "2026-10-17T00:15:00Z",
        "items": [
            {
                "issue_id": ISSUE_ID,
                "task_id": TASK_ID,
                "annotation_ids": [ANNOTATION_ID],
                "kind": "delete_small_islands",
                "frame_index": 3,
                "source_versions": {ANNOTATION_ID: 4},
                "changed_pixels": 8,
                "mutation_count": 1,
                "candidate_count": 1,
                "scope_fingerprint": DIGEST,
                "skip_code": None,
                "skip_detail": None,
            },
            {
                "issue_id": str(uuid4()),
                "task_id": TASK_ID,
                "annotation_ids": [],
                "kind": "fill_small_holes",
                "frame_index": None,
                "source_versions": {},
                "changed_pixels": 0,
                "mutation_count": 0,
                "candidate_count": 0,
                "scope_fingerprint": None,
                "skip_code": "stale_issue",
                "skip_detail": "issue source changed",
            },
        ],
        "summary": {
            "action_count": 2,
            "executable_count": 1,
            "skipped_count": 1,
            "mutation_count": 1,
            "candidate_count": 1,
            "changed_pixels": 8,
            "shard_count": 1,
        },
    }


def _repair_batch(**overrides) -> dict:
    return {
        "id": REPAIR_ID,
        "project_id": PROJECT_ID,
        "async_job_id": JOB_ID,
        "rollback_async_job_id": None,
        "status": "pending",
        "plan_digest": PLAN_DIGEST,
        "plan": {"items": []},
        "result": {"completed": 0},
        "result_digest": RESULT_DIGEST,
        "receipt_expires_at": "2026-10-17T00:15:00Z",
        "rollback_expires_at": None,
        "created_at": "2026-10-17T00:00:00Z",
        "completed_at": None,
        "rolled_back_at": None,
    } | overrides


def test_run_scope_normalization_and_202_response_preserve_versions(client, respx_mock):
    route = respx_mock.post(f"{API}/projects/{PROJECT_ID}/mask-qc/runs").mock(
        return_value=httpx.Response(202, json=_run(reused=True))
    )
    qc = MaskQC(client._http)
    body = MaskQCRunRequest(
        scope="annotation_ids",
        annotation_ids=[ANNOTATION_2_ID, ANNOTATION_ID, ANNOTATION_ID],
        expected_versions={ANNOTATION_ID: 4, ANNOTATION_2_ID: 2},
    )

    result = qc.create_run(PROJECT_ID, body)

    assert isinstance(result, MaskQCRun)
    assert result.reused is True
    assert str(result.async_job_id) == JOB_ID
    assert json.loads(route.calls.last.request.content) == {
        "scope": "annotation_ids",
        "annotation_ids": sorted([ANNOTATION_ID, ANNOTATION_2_ID]),
        "expected_versions": {ANNOTATION_ID: 4, ANNOTATION_2_ID: 2},
    }
    assert "annotation_ids" not in route.calls.last.request.url.params

    with pytest.raises(ValidationError):
        MaskQCRunRequest(scope="project", task_ids=[TASK_ID])
    with pytest.raises(ValidationError, match="duplicate expected annotation version"):
        MaskQCRunRequest(
            scope="task_ids",
            task_ids=[TASK_ID],
            expected_versions={ANNOTATION_ID: 1, ANNOTATION_ID.upper(): 2},
        )


def test_issue_filters_page_and_visible_issue_read_patch(client, respx_mock):
    issues_route = respx_mock.get(f"{API}/projects/{PROJECT_ID}/mask-qc/issues").mock(
        return_value=httpx.Response(
            200, json={"items": [_issue()], "next_cursor": "opaque-cursor=="}
        )
    )
    get_route = respx_mock.get(f"{API}/mask-qc/issues/{ISSUE_ID}").mock(
        return_value=httpx.Response(200, json=_issue())
    )
    patch_route = respx_mock.patch(f"{API}/mask-qc/issues/{ISSUE_ID}").mock(
        return_value=httpx.Response(
            200,
            json=_issue(
                status="resolved",
                effective_status="resolved",
                resolved_by_id=str(uuid4()),
            ),
        )
    )
    qc = MaskQC(client._http)

    page = qc.list_issues(
        PROJECT_ID,
        task_id=TASK_ID,
        annotation_id=ANNOTATION_ID,
        status="stale",
        severity="warning",
        code="small_island",
        frame=4,
        limit=17,
        cursor="opaque-cursor==",
    )
    issue = qc.get_issue(ISSUE_ID)
    patched = qc.patch_issue(ISSUE_ID, "resolved")

    assert isinstance(page, MaskQCIssuePage)
    assert page.next_cursor == "opaque-cursor=="
    assert isinstance(page.items[0], MaskQCIssue)
    assert page.items[0].source_versions[ANNOTATION_ID] == 4
    assert dict(issues_route.calls.last.request.url.params) == {
        "task_id": TASK_ID,
        "annotation_id": ANNOTATION_ID,
        "status": "stale",
        "severity": "warning",
        "code": "small_island",
        "frame": "4",
        "limit": "17",
        "cursor": "opaque-cursor==",
    }
    assert isinstance(issue, MaskQCIssue) and issue.effective_status == "open"
    assert json.loads(patch_route.calls.last.request.content) == {"status": "resolved"}
    assert patched.effective_status == "resolved"
    assert get_route.called


def test_task_summary_and_compare_query_content_paths_are_typed(client, respx_mock):
    summary_route = respx_mock.get(f"{API}/tasks/{TASK_ID}/mask-qc/summary").mock(
        return_value=httpx.Response(
            200,
            json={
                "task_id": TASK_ID,
                "run_id": RUN_ID,
                "qc_digest": DIGEST,
                "source_snapshot_digest": PLAN_DIGEST,
                "status": "completed",
                "progress_pct": 100,
                "counts": {"warning": 2},
                "blocking": False,
            },
        )
    )
    compare_route = respx_mock.get(
        f"{API}/annotations/{ANNOTATION_ID}/mask-compare"
    ).mock(return_value=httpx.Response(200, json=_compare()))
    content_route = respx_mock.get(
        f"{API}/annotations/{ANNOTATION_ID}/mask-compare/content"
    ).mock(
        return_value=httpx.Response(
            200, json={"encoding": "coco_rle", "size": [2, 2], "counts": [4]}
        )
    )
    region_route = respx_mock.get(
        f"{API}/mask-qc/issues/{ISSUE_ID}/region-content"
    ).mock(
        return_value=httpx.Response(
            200, json={"encoding": "coco_rle", "size": [2, 2], "counts": [0, 4]}
        )
    )
    qc = MaskQC(client._http)

    summary = qc.task_summary(TASK_ID)
    compare = qc.compare(
        ANNOTATION_ID,
        annotation_version=4,
        baseline="tracker_candidate",
        frame_index=3,
        candidate_job_id=JOB_ID,
        candidate_job_revision=8,
        candidate_digest="sha256:" + DIGEST,
        candidate_instance_id="instance-7",
    )
    content = qc.compare_content(
        ANNOTATION_ID,
        annotation_version=4,
        digest=DIGEST,
        frame_index=3,
    )
    region = qc.issue_region_content(ISSUE_ID, digest=DIGEST)

    assert isinstance(summary, TaskMaskQCSummary) and summary.status == "completed"
    assert summary_route.called
    assert isinstance(compare, MaskCompareResult)
    assert compare.baseline.content_path.endswith(f"digest={DIGEST}&frame_index=3")
    assert compare.current.digest == DIGEST
    assert dict(compare_route.calls.last.request.url.params) == {
        "annotation_version": "4",
        "baseline": "tracker_candidate",
        "frame_index": "3",
        "candidate_job_id": JOB_ID,
        "candidate_job_revision": "8",
        "candidate_digest": "sha256:" + DIGEST,
        "candidate_instance_id": "instance-7",
    }
    assert isinstance(content, CocoRleMaskContent) and content.counts == [4]
    assert content_route.calls.last.request.url.params["digest"] == DIGEST
    assert isinstance(region, CocoRleMaskContent) and region.counts == [0, 4]
    assert region_route.calls.last.request.url.params["digest"] == DIGEST


def test_stale_issue_conflict_is_not_refreshed_or_retried(client, respx_mock):
    route = respx_mock.patch(f"{API}/mask-qc/issues/{ISSUE_ID}").mock(
        return_value=httpx.Response(
            409, json={"detail": {"reason": "mask_qc_issue_stale"}}
        )
    )

    with pytest.raises(ConflictError) as caught:
        MaskQC(client._http).patch_issue(ISSUE_ID, "resolved")

    assert caught.value.detail == {"reason": "mask_qc_issue_stale"}
    assert route.call_count == 1


def test_repair_plan_execute_get_resume_rollback_keep_receipts_and_domain_states(
    client, respx_mock
):
    dry_run_route = respx_mock.post(
        f"{API}/projects/{PROJECT_ID}/mask-qc/repairs:dry-run"
    ).mock(return_value=httpx.Response(200, json=_repair_dry_run()))
    execute_route = respx_mock.post(
        f"{API}/projects/{PROJECT_ID}/mask-qc/repairs"
    ).mock(return_value=httpx.Response(202, json=_repair_batch()))
    get_route = respx_mock.get(f"{API}/mask-qc/repairs/{REPAIR_ID}").mock(
        return_value=httpx.Response(
            200,
            json=_repair_batch(
                status="partial", rollback_expires_at="2026-10-18T00:00:00Z"
            ),
        )
    )
    resume_route = respx_mock.post(f"{API}/mask-qc/repairs/{REPAIR_ID}/resume").mock(
        return_value=httpx.Response(202, json=_repair_batch())
    )
    rollback_route = respx_mock.post(
        f"{API}/mask-qc/repairs/{REPAIR_ID}/rollback"
    ).mock(
        return_value=httpx.Response(
            202,
            json=_repair_batch(
                status="rolling_back", rollback_async_job_id=str(uuid4())
            ),
        )
    )
    qc = MaskQC(client._http)
    request = MaskRepairDryRunRequest(
        actions=[MaskRepairAction(issue_id=ISSUE_ID, kind="delete_small_islands")]
    )

    plan = qc.dry_run_repairs(PROJECT_ID, request)
    executed = qc.execute_repairs(
        PROJECT_ID,
        MaskRepairExecuteRequest(receipt=plan.receipt, plan_digest=plan.plan_digest),
    )
    partial = qc.get_repair_batch(REPAIR_ID)
    resumed = qc.resume_repairs(REPAIR_ID)
    rolled_back = qc.rollback_repairs(
        REPAIR_ID, MaskRepairRollbackRequest(expected_result_digest=RESULT_DIGEST)
    )

    assert isinstance(plan, MaskRepairDryRunResult)
    assert plan.summary.skipped_count == 1
    assert "receipt" not in repr(plan)
    assert isinstance(executed, MaskRepairBatch) and executed.status == "pending"
    assert json.loads(dry_run_route.calls.last.request.content) == {
        "actions": [{"issue_id": ISSUE_ID, "kind": "delete_small_islands"}]
    }
    assert json.loads(execute_route.calls.last.request.content) == {
        "receipt": RECEIPT,
        "plan_digest": PLAN_DIGEST,
    }
    assert isinstance(partial, MaskRepairBatch) and partial.status == "partial"
    assert get_route.called
    assert isinstance(resumed, MaskRepairBatch)
    assert json.loads(resume_route.calls.last.request.content) == {}
    assert isinstance(rolled_back, MaskRepairBatch)
    assert rolled_back.status == "rolling_back"
    assert json.loads(rollback_route.calls.last.request.content) == {
        "expected_result_digest": RESULT_DIGEST
    }


def test_repair_inputs_enforce_unique_actions_and_exact_digest_shapes():
    with pytest.raises(ValidationError):
        MaskRepairDryRunRequest(
            actions=[
                MaskRepairAction(issue_id=ISSUE_ID, kind="delete_small_islands"),
                MaskRepairAction(issue_id=ISSUE_ID, kind="fill_small_holes"),
            ]
        )
    with pytest.raises(ValidationError):
        MaskRepairAction(issue_id=ISSUE_ID, kind="rerun_tracker")
    with pytest.raises(ValidationError):
        MaskRepairExecuteRequest(receipt="not-a-receipt", plan_digest="bad")
    request = MaskRepairExecuteRequest(receipt=RECEIPT, plan_digest=PLAN_DIGEST)
    assert RECEIPT not in repr(request)
    with pytest.raises(ValidationError):
        MaskRepairRollbackRequest(expected_result_digest="bad")


def test_expired_or_stale_repair_receipt_conflict_is_not_replanned(client, respx_mock):
    route = respx_mock.post(f"{API}/projects/{PROJECT_ID}/mask-qc/repairs").mock(
        return_value=httpx.Response(
            409, json={"detail": {"reason": "repair_plan_digest_conflict"}}
        )
    )

    with pytest.raises(ConflictError):
        MaskQC(client._http).execute_repairs(
            PROJECT_ID,
            MaskRepairExecuteRequest(receipt=RECEIPT, plan_digest=PLAN_DIGEST),
        )

    assert route.call_count == 1
