"""API regression coverage for SDK-facing authorization and async failure contracts."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

pytestmark = pytest.mark.asyncio


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _annotation(
    db,
    *,
    task_id: uuid.UUID,
    user_id: uuid.UUID,
    project_id: uuid.UUID | None,
    active: bool = True,
):
    from app.db.models.annotation import Annotation

    annotation = Annotation(
        task_id=task_id,
        project_id=project_id,
        user_id=user_id,
        source="manual",
        annotation_type="bbox",
        tool_unit_id="bbox",
        class_name="car",
        geometry={"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
        is_active=active,
        was_cancelled=False,
    )
    db.add(annotation)
    await db.flush()
    return annotation


async def test_history_uses_actual_task_visibility_and_allows_soft_deleted_annotations(
    httpx_client, db_session, super_admin, annotator
):
    """Denormalized annotation.project_id cannot grant or remove task visibility."""
    from tests.factory import create_membership, create_project, create_task

    admin, _ = super_admin
    member, token = annotator
    actual_project = await create_project(
        db_session, owner_id=admin.id, name="History actual task project"
    )
    unrelated_project = await create_project(
        db_session, owner_id=admin.id, name="History denormalized project"
    )
    await create_membership(
        db_session,
        project_id=actual_project.id,
        user_id=member.id,
        role="annotator",
        assigned_by=admin.id,
    )
    visible_task = await create_task(
        db_session,
        project_id=actual_project.id,
        display_id=f"T-HISTORY-VISIBLE-{uuid.uuid4().hex[:8]}",
    )
    visible_task.assignee_id = member.id
    hidden_task = await create_task(
        db_session,
        project_id=actual_project.id,
        display_id=f"T-HISTORY-HIDDEN-{uuid.uuid4().hex[:8]}",
    )

    # A mismatched denormalized project used to authorize against the wrong
    # project. A NULL value must still resolve visibility from task.project_id.
    spoofed = await _annotation(
        db_session,
        task_id=visible_task.id,
        user_id=member.id,
        project_id=unrelated_project.id,
    )
    soft_deleted = await _annotation(
        db_session,
        task_id=visible_task.id,
        user_id=member.id,
        project_id=None,
        active=False,
    )
    hidden_annotation = await _annotation(
        db_session,
        task_id=hidden_task.id,
        user_id=member.id,
        project_id=actual_project.id,
    )
    await db_session.flush()

    headers = _bearer(token)
    for annotation in (spoofed, soft_deleted):
        response = await httpx_client.get(
            f"/api/v1/annotations/{annotation.id}/history", headers=headers
        )
        assert response.status_code == 200, response.text
        assert response.json()["task_id"] == str(visible_task.id)

    visible_task_history = await httpx_client.get(
        f"/api/v1/tasks/{visible_task.id}/audit-history", headers=headers
    )
    assert visible_task_history.status_code == 200, visible_task_history.text

    hidden_annotation_history = await httpx_client.get(
        f"/api/v1/annotations/{hidden_annotation.id}/history", headers=headers
    )
    assert hidden_annotation_history.status_code == 404
    hidden_task_history = await httpx_client.get(
        f"/api/v1/tasks/{hidden_task.id}/audit-history", headers=headers
    )
    assert hidden_task_history.status_code == 404


async def test_bug_report_patch_field_authority_and_nullable_admin_fields(
    httpx_client, db_session, annotator, project_admin
):
    """Reporters edit content only; global project admins retain manager fields."""
    from tests.test_bug_reports import _seed_bug

    reporter, reporter_token = annotator
    manager, manager_token = project_admin
    report = await _seed_bug(db_session, reporter.id, status="new")
    await db_session.flush()

    reporter_edit = await httpx_client.patch(
        f"/api/v1/bug_reports/{report.id}",
        headers=_bearer(reporter_token),
        json={
            "title": "Updated title",
            "description": "Updated details",
            "severity": "high",
        },
    )
    assert reporter_edit.status_code == 200, reporter_edit.text
    assert reporter_edit.json()["title"] == "Updated title"
    assert reporter_edit.json()["description"] == "Updated details"
    assert reporter_edit.json()["severity"] == "high"

    # Even an explicit null on an administrator-only field is an attempted
    # administrative write. Reject the whole patch before applying its title.
    report.resolution = "retain this admin field"
    await db_session.flush()
    mixed_reporter_patch = await httpx_client.patch(
        f"/api/v1/bug_reports/{report.id}",
        headers=_bearer(reporter_token),
        json={
            "title": "Must not persist",
            "status": "fixed",
            "resolution": None,
        },
    )
    assert mixed_reporter_patch.status_code == 403
    await db_session.refresh(report)
    assert report.title == "Updated title"
    assert report.resolution == "retain this admin field"

    # Bug management is global for project_admin. This caller is neither the
    # report author nor a project owner; status null is accepted as a no-op.
    duplicate = await _seed_bug(db_session, reporter.id, status="new")
    report.status = "fixed"
    report.resolution = "confirmed"
    report.fixed_in_version = "1.0.0"
    report.assigned_to_id = manager.id
    report.duplicate_of_id = duplicate.id
    await db_session.flush()

    cleared = await httpx_client.patch(
        f"/api/v1/bug_reports/{report.id}",
        headers=_bearer(manager_token),
        json={
            "status": None,
            "resolution": None,
            "assigned_to_id": None,
            "duplicate_of_id": None,
            "fixed_in_version": None,
        },
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["status"] == "fixed"
    for field in (
        "resolution",
        "assigned_to_id",
        "duplicate_of_id",
        "fixed_in_version",
    ):
        assert cleared.json()[field] is None


@pytest.mark.parametrize("field", ["title", "description", "severity"])
async def test_bug_report_patch_rejects_null_for_nonnullable_reporter_fields(
    field, httpx_client, db_session, annotator
):
    from tests.test_bug_reports import _seed_bug

    reporter, token = annotator
    report = await _seed_bug(db_session, reporter.id, status="new")
    original = {
        "title": report.title,
        "description": report.description,
        "severity": report.severity,
    }
    await db_session.flush()

    response = await httpx_client.patch(
        f"/api/v1/bug_reports/{report.id}",
        headers=_bearer(token),
        json={field: None},
    )
    assert response.status_code == 422, response.text
    await db_session.refresh(report)
    assert report.title == original["title"]
    assert report.description == original["description"]
    assert report.severity == original["severity"]


async def test_mask_format_export_preflight_requires_export_capability_before_adapter(
    httpx_client,
    db_session,
    super_admin,
    project_admin,
    annotator,
    reviewer,
    monkeypatch,
):
    """Only reviewer/owner/super have export.annotations; no denied adapter work runs."""
    from tests.factory import create_membership, create_project, create_user

    from app.core.security import create_access_token
    from app.schemas.mask_format import MaskFormatPlan

    admin, admin_token = super_admin
    nonowner_project_admin, project_admin_token = project_admin
    annotator_user, annotator_token = annotator
    reviewer_user, reviewer_token = reviewer
    owner = await create_user(
        db_session,
        "project_admin",
        f"sdk-mask-owner-{uuid.uuid4().hex[:8]}@test.local",
        "Mask format owner",
    )
    owner_token = create_access_token(subject=str(owner.id), role=owner.role)
    viewer = await create_user(
        db_session,
        "viewer",
        f"sdk-mask-viewer-{uuid.uuid4().hex[:8]}@test.local",
        "Mask format viewer",
    )
    viewer_token = create_access_token(subject=str(viewer.id), role=viewer.role)
    project = await create_project(
        db_session, owner_id=owner.id, name="Mask export capability scope"
    )
    for user, role in (
        (annotator_user, "annotator"),
        (reviewer_user, "reviewer"),
        (viewer, "viewer"),
        # Simulate a legacy row so global project_admin role cannot act as a
        # project manager when it only has viewer-level project capability.
        (nonowner_project_admin, "viewer"),
    ):
        await create_membership(
            db_session,
            project_id=project.id,
            user_id=user.id,
            role=role,
            assigned_by=owner.id,
        )

    plan = MaskFormatPlan(
        format_id="sdk-test-format",
        direction="export",
        adapter_version="1",
        manifest_version="1",
        media_type="image",
        loss_class="lossless",
        mapping_digest="a" * 64,
        options_digest="b" * 64,
        estimated_objects=0,
        estimated_files=0,
        estimated_bytes=0,
        plan_digest="c" * 64,
    )
    adapter = SimpleNamespace(
        descriptor=SimpleNamespace(
            export_capability=SimpleNamespace(supported=True),
            media_types=["image"],
        ),
        preflight_export=AsyncMock(return_value=plan),
    )
    registry_get = Mock(return_value=adapter)
    from app.api.v1 import mask_formats as mask_formats_api

    monkeypatch.setattr(
        mask_formats_api,
        "registry",
        SimpleNamespace(get=registry_get),
    )
    path = f"/api/v1/projects/{project.id}/mask-formats/exports:preflight"
    request = {"targets": ["sdk-test-format"]}

    denied = (
        (annotator_token, "annotator"),
        (viewer_token, "viewer"),
        (project_admin_token, "nonowner project_admin viewer"),
    )
    for token, label in denied:
        response = await httpx_client.post(path, headers=_bearer(token), json=request)
        assert response.status_code == 403, f"{label}: {response.text}"
        registry_get.assert_not_called()
    assert adapter.preflight_export.await_count == 0

    allowed = (
        (reviewer_token, "reviewer"),
        (owner_token, "owner"),
        (admin_token, "super admin"),
    )
    for token, label in allowed:
        response = await httpx_client.post(path, headers=_bearer(token), json=request)
        assert response.status_code == 200, f"{label}: {response.text}"
        assert response.json()["plans"][0]["format_id"] == "sdk-test-format"
    assert registry_get.call_count == 3
    assert adapter.preflight_export.await_count == 3


async def test_quality_dispatch_failure_persists_failed_run_and_job(
    db_session, httpx_client, super_admin, monkeypatch
):
    """A broker exception returns IDs for the persisted failed run and job."""
    from tests.test_point_cloud_quality_api import _seed_quality_scene

    from app.db.models.async_job import AsyncJob
    from app.db.models.point_cloud_quality import PointCloudQualityRun

    user, token = super_admin
    project, scene, _tasks, _track, _annotations = await _seed_quality_scene(
        db_session, owner_id=user.id
    )

    def fail_dispatch(*, args, task_id):
        raise RuntimeError("queue unavailable for regression test")

    monkeypatch.setattr(
        "app.workers.point_cloud_quality.run_point_cloud_quality.apply_async",
        fail_dispatch,
    )
    await db_session.commit()
    headers = _bearer(token)
    response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/point-cloud-quality/runs",
        headers=headers,
        json={"scope": "scene_ids", "scene_ids": [str(scene.id)]},
    )
    assert response.status_code == 503, response.text
    detail = response.json()["detail"]
    assert detail["reason"] == "point_cloud_quality_dispatch_failed"
    assert detail["message"] == "queue unavailable for regression test"
    run_id = uuid.UUID(detail["run_id"])
    job_id = uuid.UUID(detail["async_job_id"])

    run = await db_session.get(PointCloudQualityRun, run_id)
    job = await db_session.get(AsyncJob, job_id)
    assert run is not None and job is not None
    await db_session.refresh(run)
    await db_session.refresh(job)
    assert run.status == "failed"
    assert run.async_job_id == job_id
    assert "point_cloud_quality_dispatch_failed" in (run.error_message or "")
    assert job.status == "failed"
    assert "queue unavailable for regression test" in (job.error_message or "")

    readback = await httpx_client.get(
        f"/api/v1/projects/{project.id}/point-cloud-quality/runs/{run_id}",
        headers=headers,
    )
    assert readback.status_code == 200, readback.text
    assert readback.json()["id"] == str(run_id)
    assert readback.json()["async_job_id"] == str(job_id)
    assert readback.json()["status"] == "failed"
