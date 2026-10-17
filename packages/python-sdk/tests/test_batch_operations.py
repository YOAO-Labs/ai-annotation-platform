"""Contracts for batch preview, administration, audit, and submission routes."""

import json
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from ai_annotation.batch_operation_models import (
    AdminBatchLockRequest,
    BatchAssignmentPreviewRequest,
    BatchAuditLogEntry,
    BatchDistributionPreview,
    BatchDistributionRequest,
    BatchSplitRequest,
    BatchSubmitReviewResult,
    LidarExportPreflightRequest,
    LidarExportPreflightResult,
    ReviewedBatchAssignment,
    ReviewedBatchDistribution,
    UnclassifiedTaskCount,
)
from ai_annotation.errors import ConflictError, NotFoundError
from ai_annotation.models import Batch, BatchDistributeResult, BulkBatchActionResult

from .conftest import API

PROJECT_ID, BATCH_ID, ANNOTATOR_ID, REVIEWER_ID = [str(uuid4()) for _ in range(4)]
PREVIEW_VERSION = "a" * 64


def _batch() -> dict:
    return {
        "id": BATCH_ID,
        "project_id": PROJECT_ID,
        "display_id": "B-17",
        "name": "Part 17",
        "status": "active",
        "total_tasks": 12,
        "completed_tasks": 4,
        "review_tasks": 1,
        "approved_tasks": 3,
        "rejected_tasks": 0,
        "progress_pct": 33.3,
        "created_at": "2026-10-17T00:00:00Z",
    }


def _preview() -> dict:
    return {
        "project_id": PROJECT_ID,
        "only_unassigned": True,
        "total_batches": 1,
        "candidate_batches": 1,
        "changed_batches": 1,
        "skipped_batches": 0,
        "items": [
            {
                "batch_id": BATCH_ID,
                "display_id": "B-17",
                "name": "Part 17",
                "status": "active",
                "task_count": 12,
                "before_annotator_id": None,
                "after_annotator_id": ANNOTATOR_ID,
                "before_reviewer_id": None,
                "after_reviewer_id": REVIEWER_ID,
                "will_change": True,
                "skipped_reason": None,
            }
        ],
        "recipient_summary": [
            {
                "user_id": ANNOTATOR_ID,
                "role": "annotator",
                "new_task_count": 12,
                "existing_backlog_count": 2,
            }
        ],
        "preview_version": PREVIEW_VERSION,
    }


def _partial_result() -> dict:
    return {
        "succeeded": [BATCH_ID],
        "skipped": [{"batch_id": str(uuid4()), "reason": "already archived"}],
        "failed": [
            {
                "batch_id": str(uuid4()),
                "reason": "requires_force",
                "affected_tasks": 3,
            }
        ],
    }


def test_count_and_audit_use_exact_project_scoped_routes_and_limit(client, respx_mock):
    count_route = respx_mock.get(
        f"{API}/projects/{PROJECT_ID}/batches/unclassified-count"
    ).mock(return_value=httpx.Response(200, json={"count": 9}))
    audit_route = respx_mock.get(
        f"{API}/projects/{PROJECT_ID}/batches/{BATCH_ID}/audit-logs"
    ).mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 81,
                    "created_at": "2026-10-17T00:00:00Z",
                    "actor_id": str(uuid4()),
                    "actor_email": "owner@example.test",
                    "actor_role": "project_admin",
                    "action": "batch.admin_lock",
                    "target_type": "batch",
                    "target_id": BATCH_ID,
                    "detail": {"reason": "review requested", "nested": {"v": 1}},
                }
            ],
        )
    )

    count = client.batches.unclassified_count(PROJECT_ID)
    entries = client.batches.audit_logs(PROJECT_ID, BATCH_ID, limit=17)

    assert isinstance(count, UnclassifiedTaskCount) and count.count == 9
    assert isinstance(entries[0], BatchAuditLogEntry)
    assert entries[0].detail["nested"] == {"v": 1}
    assert count_route.called
    assert audit_route.calls.last.request.url.params["limit"] == "17"


def test_batch_audit_foreign_or_missing_pair_404_is_not_hidden(client, respx_mock):
    route = respx_mock.get(
        f"{API}/projects/{PROJECT_ID}/batches/{BATCH_ID}/audit-logs"
    ).mock(return_value=httpx.Response(404, json={"detail": "Batch not found"}))

    with pytest.raises(NotFoundError) as caught:
        client.batches.audit_logs(PROJECT_ID, BATCH_ID)

    assert caught.value.status_code == 404
    assert route.call_count == 1


def test_bulk_archive_and_delete_send_typed_ids_and_keep_partial_receipt(
    client, respx_mock
):
    archive_route = respx_mock.post(
        f"{API}/projects/{PROJECT_ID}/batches/bulk-archive"
    ).mock(return_value=httpx.Response(200, json=_partial_result()))
    delete_route = respx_mock.post(
        f"{API}/projects/{PROJECT_ID}/batches/bulk-delete"
    ).mock(return_value=httpx.Response(200, json=_partial_result()))
    ids = [BATCH_ID, str(uuid4())]

    archived = client.batches.bulk_archive(PROJECT_ID, ids)
    deleted = client.batches.bulk_delete(PROJECT_ID, ids, force=True)

    assert isinstance(archived, BulkBatchActionResult)
    assert str(archived.succeeded[0]) == BATCH_ID
    assert archived.failed[0].reason == "requires_force"
    assert archived.failed[0].affected_tasks == 3
    assert json.loads(archive_route.calls.last.request.content) == {"batch_ids": ids}
    assert json.loads(delete_route.calls.last.request.content) == {"batch_ids": ids}
    assert delete_route.calls.last.request.url.params["force"] == "true"
    assert deleted.failed[0].reason == "requires_force"

    with pytest.raises(ValidationError):
        client.batches.bulk_archive(PROJECT_ID, [])
    assert archive_route.call_count == 1


def test_distribution_preview_apply_preserve_selected_inputs_and_surface_stale_conflict(
    client, respx_mock
):
    preview_route = respx_mock.post(
        f"{API}/projects/{PROJECT_ID}/batches/distribution-preview"
    ).mock(return_value=httpx.Response(200, json=_preview()))
    apply_route = respx_mock.post(
        f"{API}/projects/{PROJECT_ID}/batches/distribution-apply"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "distributed_batches": 1,
                "annotator_per_batch": {BATCH_ID: ANNOTATOR_ID},
                "reviewer_per_batch": {BATCH_ID: REVIEWER_ID},
            },
        )
    )
    body = BatchDistributionRequest(
        batch_ids=[BATCH_ID],
        annotator_ids=[ANNOTATOR_ID],
        reviewer_ids=[],
        only_unassigned=False,
    )

    preview = client.batches.distribution_preview(PROJECT_ID, body)
    applied = client.batches.distribution_apply(
        PROJECT_ID,
        ReviewedBatchDistribution(
            batch_ids=[BATCH_ID],
            annotator_ids=[ANNOTATOR_ID],
            reviewer_ids=[],
            only_unassigned=False,
            preview_version=preview.preview_version,
        ),
    )

    assert isinstance(preview, BatchDistributionPreview)
    assert str(preview.items[0].after_annotator_id) == ANNOTATOR_ID
    assert isinstance(applied, BatchDistributeResult)
    assert applied.distributed_batches == 1
    assert json.loads(preview_route.calls.last.request.content) == {
        "batch_ids": [BATCH_ID],
        "annotator_ids": [ANNOTATOR_ID],
        "reviewer_ids": [],
        "only_unassigned": False,
    }
    assert json.loads(apply_route.calls.last.request.content) == {
        "batch_ids": [BATCH_ID],
        "annotator_ids": [ANNOTATOR_ID],
        "reviewer_ids": [],
        "only_unassigned": False,
        "preview_version": PREVIEW_VERSION,
    }


def test_distribution_apply_does_not_refresh_or_retry_conflict(client, respx_mock):
    route = respx_mock.post(
        f"{API}/projects/{PROJECT_ID}/batches/distribution-apply"
    ).mock(return_value=httpx.Response(409, json={"detail": "stale preview"}))
    body = ReviewedBatchDistribution(
        annotator_ids=[ANNOTATOR_ID],
        reviewer_ids=[],
        preview_version=PREVIEW_VERSION,
    )

    with pytest.raises(ConflictError):
        client.batches.distribution_apply(PROJECT_ID, body)

    assert route.call_count == 1


def test_split_admin_lock_and_unlock_use_typed_requests(client, respx_mock):
    split_route = respx_mock.post(f"{API}/projects/{PROJECT_ID}/batches/split").mock(
        return_value=httpx.Response(200, json=[_batch()])
    )
    lock_batch = _batch()
    lock_batch["admin_locked"] = True
    lock_route = respx_mock.post(
        f"{API}/projects/{PROJECT_ID}/batches/{BATCH_ID}/admin-lock"
    ).mock(return_value=httpx.Response(200, json=lock_batch))
    unlock_batch = _batch()
    unlock_batch["admin_locked"] = False
    unlock_route = respx_mock.post(
        f"{API}/projects/{PROJECT_ID}/batches/{BATCH_ID}/admin-unlock"
    ).mock(return_value=httpx.Response(200, json=unlock_batch))

    body = BatchSplitRequest(strategy="random", n_batches=3, shuffle=False)
    batches = client.batches.split(PROJECT_ID, body)
    locked = client.batches.admin_lock(PROJECT_ID, BATCH_ID, "pause while disputed")
    unlocked = client.batches.admin_unlock(PROJECT_ID, BATCH_ID)

    assert isinstance(batches[0], Batch)
    assert json.loads(split_route.calls.last.request.content) == {
        "strategy": "random",
        "n_batches": 3,
        "shuffle": False,
    }
    assert json.loads(lock_route.calls.last.request.content) == {
        "reason": "pause while disputed"
    }
    assert json.loads(unlock_route.calls.last.request.content) == {}
    assert locked.admin_locked is True
    assert unlocked.admin_locked is False

    with pytest.raises(ValidationError):
        AdminBatchLockRequest(reason="")


def test_assignment_preview_apply_preserve_explicit_null_targets(client, respx_mock):
    preview_route = respx_mock.post(
        f"{API}/projects/{PROJECT_ID}/batches/{BATCH_ID}/assignment-preview"
    ).mock(return_value=httpx.Response(200, json=_preview()))
    updated_batch = _batch()
    updated_batch["annotator_id"] = None
    apply_route = respx_mock.post(
        f"{API}/projects/{PROJECT_ID}/batches/{BATCH_ID}/assignment-apply"
    ).mock(return_value=httpx.Response(200, json=updated_batch))
    preview_body = BatchAssignmentPreviewRequest(
        annotator_id=None, reviewer_id=REVIEWER_ID
    )
    apply_body = ReviewedBatchAssignment(
        annotator_id=None,
        reviewer_id=REVIEWER_ID,
        preview_version=PREVIEW_VERSION,
    )

    preview = client.batches.assignment_preview(PROJECT_ID, BATCH_ID, preview_body)
    batch = client.batches.assignment_apply(PROJECT_ID, BATCH_ID, apply_body)

    assert isinstance(preview, BatchDistributionPreview)
    assert isinstance(batch, Batch)
    assert json.loads(preview_route.calls.last.request.content) == {
        "annotator_id": None,
        "reviewer_id": REVIEWER_ID,
    }
    assert json.loads(apply_route.calls.last.request.content) == {
        "annotator_id": None,
        "reviewer_id": REVIEWER_ID,
        "preview_version": PREVIEW_VERSION,
    }


def test_lidar_preflight_returns_not_ready_issues_as_normal_response(
    client, respx_mock
):
    route = respx_mock.post(
        f"{API}/projects/{PROJECT_ID}/batches/{BATCH_ID}/exports/lidar:preflight"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "ready": False,
                "camera_roles": ["camera_front"],
                "selected_camera_role": "camera_front",
                "checked_tasks": 1,
                "issue_count": 1,
                "issues_truncated": False,
                "issues": [
                    {
                        "code": "missing_camera_frame",
                        "message": "camera frame is missing",
                        "task_id": str(uuid4()),
                        "task_display_id": "T-1",
                        "frame_key": "000001",
                        "camera_role": "camera_front",
                    }
                ],
            },
        )
    )

    result = client.batches.lidar_preflight(
        PROJECT_ID,
        BATCH_ID,
        LidarExportPreflightRequest(
            targets=["kitti"], lidar={"kitti_camera_role": "camera_front"}
        ),
    )

    assert isinstance(result, LidarExportPreflightResult)
    assert result.ready is False
    assert result.issues[0].code == "missing_camera_frame"
    assert json.loads(route.calls.last.request.content) == {
        "targets": ["kitti"],
        "lidar": {"kitti_camera_role": "camera_front"},
    }


def test_submit_review_posts_empty_object_and_parses_counts(client, respx_mock):
    route = respx_mock.post(
        f"{API}/projects/{PROJECT_ID}/batches/{BATCH_ID}/submit-review"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "batch_id": BATCH_ID,
                "status": "reviewing",
                "submitted_tasks": 8,
                "skipped_tasks": 1,
                "remaining_tasks": 2,
            },
        )
    )

    result = client.batches.submit_review(PROJECT_ID, BATCH_ID)

    assert isinstance(result, BatchSubmitReviewResult)
    assert result.submitted_tasks == 8 and result.remaining_tasks == 2
    assert json.loads(route.calls.last.request.content) == {}
