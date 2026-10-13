"""Project-role authorization for the B2-entry API surface.

Covers the routes reserved to this work package: platform catalog admin
(``groups``), dataset-to-project linking, annotation history, task views,
mask QC, point-cloud quality, video chapters, video tracker jobs,
failed-prediction administration, and ML backend project reads.

Every account is a literal platform ``employee`` / ``project_admin`` /
``viewer`` with an *explicit* :class:`ProjectMember` row; no fixture translates
a legacy global annotator/reviewer into authority.  These are real PostgreSQL
HTTP tests, run by the coordinator in the isolated test environment.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.mask_qc import _resolve_task_review_access
from app.api.v1.ml_backends import _reacquire_write_boundary
from app.api.v1.tasks._shared import assert_annotation_write_allowed
from app.api.v1.video_tracker_jobs import _lock_visible_job_task_row
from app.core.security import create_access_token
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.prediction import FailedPrediction
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.db.models.user import User
from app.db.models.video_tracker_job import VideoTrackerJob
from app.services.project_access import ProjectAccess, ProjectCapability
from app.services.data_management.views import TaskViewService
from tests.factory import (
    create_batch,
    create_membership,
    create_project,
    create_task,
    create_user,
)

pytestmark = pytest.mark.asyncio


def _headers(user: User) -> dict[str, str]:
    token = create_access_token(subject=str(user.id), role=user.role)
    return {"Authorization": f"Bearer {token}"}


async def _employee(
    db: AsyncSession, role: str = "employee", prefix: str = "entry"
) -> User:
    suffix = uuid.uuid4().hex[:8]
    return await create_user(
        db, role, f"{prefix}-{suffix}@test.local", f"{prefix}-{suffix}"
    )


async def _add_member(
    db: AsyncSession, *, project_id: uuid.UUID, user: User, role: str, by: uuid.UUID
) -> ProjectMember:
    return await create_membership(
        db,
        project_id=project_id,
        user_id=user.id,
        role=role,
        assigned_by=by,
    )


async def _seed_dataset(
    db: AsyncSession, *, created_by: uuid.UUID, data_type: str = "image"
) -> Dataset:
    suffix = uuid.uuid4().hex[:6]
    dataset = Dataset(
        display_id=f"D-ENT-{suffix}",
        name=f"entry dataset {suffix}",
        data_type=data_type,
        created_by=created_by,
    )
    db.add(dataset)
    await db.flush()
    return dataset


async def _seed_video_item_task(
    db: AsyncSession, *, owner_id: uuid.UUID
) -> tuple[DatasetItem, Task]:
    suffix = uuid.uuid4().hex[:6]
    project = Project(
        display_id=f"P-ENT-{suffix}",
        name=f"video entry {suffix}",
        type_key="video-track",
        type_label="视频 · 时序追踪",
        owner_id=owner_id,
        tool_bindings={
            "region": {
                "enabled": True,
                "classes": [{"name": "car", "order": 0}],
                "attribute_schema": {"fields": []},
            }
        },
    )
    dataset = Dataset(
        display_id=f"D-VID-{suffix}",
        name=f"videos {suffix}",
        data_type="video",
        created_by=owner_id,
    )
    db.add_all([project, dataset])
    await db.flush()
    item = DatasetItem(
        dataset_id=dataset.id,
        file_name="clip.mp4",
        file_path=f"videos/{suffix}.mp4",
        file_type="video",
        width=640,
        height=360,
        metadata_={
            "video": {
                "duration_ms": 1000,
                "fps": 25,
                "frame_count": 40,
                "width": 640,
                "height": 360,
                "codec": "h264",
            }
        },
    )
    db.add(item)
    await db.flush()
    task = Task(
        project_id=project.id,
        dataset_item_id=item.id,
        display_id=f"T-VID-{suffix}",
        file_name="clip.mp4",
        file_path=f"videos/{suffix}.mp4",
        file_type="video",
        status="pending",
    )
    db.add(task)
    await db.flush()
    return item, task


# ── Platform catalog: groups (platform-only admin operation) ─────────────────


async def test_groups_are_platform_admin_only(
    httpx_client, db_session: AsyncSession, super_admin
):
    admin, _ = super_admin
    employee = await _employee(db_session, prefix="grp")

    denied = await httpx_client.get("/api/v1/groups", headers=_headers(employee))
    assert denied.status_code == 403

    allowed = await httpx_client.get("/api/v1/groups", headers=_headers(admin))
    assert allowed.status_code == 200


# ── datasets: project linking is target-project management ───────────────────


async def test_dataset_link_requires_target_project_management(
    httpx_client, db_session: AsyncSession, super_admin
):
    admin, _ = super_admin
    project = await create_project(db_session, owner_id=admin.id, name="Link target")
    dataset = await _seed_dataset(db_session, created_by=admin.id)

    annotator = await _employee(db_session, prefix="link-anno")
    await _add_member(
        db_session,
        project_id=project.id,
        user=annotator,
        role="annotator",
        by=admin.id,
    )
    foreign_admin = await _employee(db_session, role="project_admin", prefix="link-pa")
    await _add_member(
        db_session,
        project_id=project.id,
        user=foreign_admin,
        role="reviewer",
        by=admin.id,
    )
    body = {"project_id": str(project.id)}

    denied_annotator = await httpx_client.post(
        f"/api/v1/datasets/{dataset.id}/link", json=body, headers=_headers(annotator)
    )
    assert denied_annotator.status_code == 403

    # A platform project administrator who only holds a membership in the target
    # project receives that membership's capabilities, never management.
    denied_foreign = await httpx_client.post(
        f"/api/v1/datasets/{dataset.id}/link",
        json=body,
        headers=_headers(foreign_admin),
    )
    assert denied_foreign.status_code == 403

    allowed = await httpx_client.post(
        f"/api/v1/datasets/{dataset.id}/link", json=body, headers=_headers(admin)
    )
    assert allowed.status_code == 200
    assert allowed.json()["status"] in {"linked", "linking"}


# ── annotation history: canonical project visibility ─────────────────────────


async def test_annotation_history_uses_project_visibility(
    httpx_client, db_session: AsyncSession, super_admin
):
    admin, _ = super_admin
    project = await create_project(db_session, owner_id=admin.id, name="History")
    task = await create_task(db_session, project_id=project.id)

    member = await _employee(db_session, prefix="hist-in")
    await _add_member(
        db_session, project_id=project.id, user=member, role="annotator", by=admin.id
    )
    outsider = await _employee(db_session, prefix="hist-out")

    visible = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/audit-history", headers=_headers(member)
    )
    assert visible.status_code == 200

    hidden = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/audit-history", headers=_headers(outsider)
    )
    assert hidden.status_code == 404


# ── task views: read for members, shared writes for managers ─────────────────


async def test_task_view_shared_write_requires_management(
    httpx_client, db_session: AsyncSession, super_admin
):
    admin, _ = super_admin
    project = await create_project(db_session, owner_id=admin.id, name="Views")
    member = await _employee(db_session, prefix="view-emp")
    await _add_member(
        db_session, project_id=project.id, user=member, role="annotator", by=admin.id
    )

    listed = await httpx_client.get(
        f"/api/v1/projects/{project.id}/task-views", headers=_headers(member)
    )
    assert listed.status_code == 200

    shared = await httpx_client.post(
        f"/api/v1/projects/{project.id}/task-views",
        json={"name": "shared", "visibility": "project"},
        headers=_headers(member),
    )
    assert shared.status_code == 403

    private = await httpx_client.post(
        f"/api/v1/projects/{project.id}/task-views",
        json={"name": "private", "visibility": "private"},
        headers=_headers(member),
    )
    assert private.status_code == 201

    manager_shared = await httpx_client.post(
        f"/api/v1/projects/{project.id}/task-views",
        json={"name": "manager shared", "visibility": "project"},
        headers=_headers(admin),
    )
    assert manager_shared.status_code == 201


# ── mask QC: reviewer/manager authority, annotator denied ────────────────────


async def test_mask_qc_summary_requires_review_authority(
    httpx_client, db_session: AsyncSession, super_admin
):
    admin, _ = super_admin
    project = await create_project(db_session, owner_id=admin.id, name="Mask QC")
    task = await create_task(db_session, project_id=project.id)

    annotator = await _employee(db_session, prefix="qc-anno")
    await _add_member(
        db_session, project_id=project.id, user=annotator, role="annotator", by=admin.id
    )
    task.assignee_id = annotator.id
    reviewer = await _employee(db_session, prefix="qc-rev")
    await _add_member(
        db_session, project_id=project.id, user=reviewer, role="reviewer", by=admin.id
    )
    task.reviewer_id = reviewer.id
    await db_session.flush()

    denied = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/mask-qc/summary", headers=_headers(annotator)
    )
    assert denied.status_code == 403

    allowed = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/mask-qc/summary", headers=_headers(reviewer)
    )
    assert allowed.status_code == 200


# ── point-cloud quality: reviewer/manager authority ──────────────────────────


async def test_point_cloud_quality_issues_require_review_authority(
    httpx_client, db_session: AsyncSession, super_admin
):
    admin, _ = super_admin
    project = await create_project(db_session, owner_id=admin.id, name="PCQ")

    annotator = await _employee(db_session, prefix="pcq-anno")
    await _add_member(
        db_session, project_id=project.id, user=annotator, role="annotator", by=admin.id
    )
    reviewer = await _employee(db_session, prefix="pcq-rev")
    await _add_member(
        db_session, project_id=project.id, user=reviewer, role="reviewer", by=admin.id
    )

    denied = await httpx_client.get(
        f"/api/v1/projects/{project.id}/point-cloud-quality/issues",
        headers=_headers(annotator),
    )
    assert denied.status_code == 403

    allowed = await httpx_client.get(
        f"/api/v1/projects/{project.id}/point-cloud-quality/issues",
        headers=_headers(reviewer),
    )
    assert allowed.status_code == 200


# ── video chapters: manager-only mutation ────────────────────────────────────


async def test_video_chapter_requires_project_manager(
    httpx_client, db_session: AsyncSession, super_admin
):
    admin, _ = super_admin
    item, task = await _seed_video_item_task(db_session, owner_id=admin.id)

    annotator = await _employee(db_session, prefix="vid-anno")
    await _add_member(
        db_session,
        project_id=task.project_id,
        user=annotator,
        role="annotator",
        by=admin.id,
    )
    task.assignee_id = annotator.id
    reviewer = await _employee(db_session, prefix="vid-rev")
    await _add_member(
        db_session,
        project_id=task.project_id,
        user=reviewer,
        role="reviewer",
        by=admin.id,
    )
    task.reviewer_id = reviewer.id
    await db_session.flush()

    payload = {"start_frame": 0, "end_frame": 5, "title": "seg"}
    denied_annotator = await httpx_client.post(
        f"/api/v1/videos/{item.id}/chapters",
        json=payload,
        headers=_headers(annotator),
    )
    assert denied_annotator.status_code == 403

    denied_reviewer = await httpx_client.post(
        f"/api/v1/videos/{item.id}/chapters",
        json=payload,
        headers=_headers(reviewer),
    )
    assert denied_reviewer.status_code == 403

    allowed = await httpx_client.post(
        f"/api/v1/videos/{item.id}/chapters", json=payload, headers=_headers(admin)
    )
    assert allowed.status_code == 201


# ── video tracker jobs: annotation-phase write denies reviewer ───────────────


async def test_video_tracker_job_write_denies_reviewer(
    httpx_client, db_session: AsyncSession, super_admin
):
    admin, _ = super_admin
    item, task = await _seed_video_item_task(db_session, owner_id=admin.id)

    annotator = await _employee(db_session, prefix="trk-anno")
    await _add_member(
        db_session,
        project_id=task.project_id,
        user=annotator,
        role="annotator",
        by=admin.id,
    )
    task.assignee_id = annotator.id
    reviewer = await _employee(db_session, prefix="trk-rev")
    await _add_member(
        db_session,
        project_id=task.project_id,
        user=reviewer,
        role="reviewer",
        by=admin.id,
    )
    task.reviewer_id = reviewer.id

    job = VideoTrackerJob(
        task_id=task.id,
        dataset_item_id=item.id,
        created_by=annotator.id,
        status="queued",
        job_kind="tracking",
        model_key="sam2",
        direction="forward",
        from_frame=0,
        to_frame=5,
        event_channel="project",
    )
    db_session.add(job)
    await db_session.flush()

    # The creator (assigned annotator) may cancel.
    allowed = await httpx_client.delete(
        f"/api/v1/video-tracker-jobs/{job.id}", headers=_headers(annotator)
    )
    assert allowed.status_code == 200

    # A reviewer is denied annotation-phase tracker work outside a review round.
    second = VideoTrackerJob(
        task_id=task.id,
        dataset_item_id=item.id,
        created_by=annotator.id,
        status="queued",
        job_kind="tracking",
        model_key="sam2",
        direction="forward",
        from_frame=0,
        to_frame=5,
        event_channel="project",
    )
    db_session.add(second)
    await db_session.flush()
    denied_reviewer = await httpx_client.post(
        f"/api/v1/video-tracker-jobs/{second.id}/accept",
        headers=_headers(reviewer),
    )
    assert denied_reviewer.status_code == 403

    outsider = await _employee(db_session, prefix="trk-out")
    hidden = await httpx_client.get(
        f"/api/v1/video-tracker-jobs/{second.id}", headers=_headers(outsider)
    )
    assert hidden.status_code == 404


# ── failed predictions: project-admin scope is owned projects only ───────────


async def test_failed_prediction_management_is_project_scoped(
    httpx_client, db_session: AsyncSession, super_admin
):
    admin, _ = super_admin
    project_admin = await _employee(db_session, role="project_admin", prefix="fp-pa")
    owned = await create_project(db_session, owner_id=project_admin.id, name="PA owned")
    foreign = await create_project(db_session, owner_id=admin.id, name="PA foreign")

    owned_failure = FailedPrediction(
        project_id=owned.id, error_type="timeout", message="owned"
    )
    foreign_failure = FailedPrediction(
        project_id=foreign.id, error_type="timeout", message="foreign"
    )
    db_session.add_all([owned_failure, foreign_failure])
    await db_session.flush()

    # The global list is scoped to the administrator's owned projects.
    listed = await httpx_client.get(
        "/api/v1/admin/failed-predictions", headers=_headers(project_admin)
    )
    assert listed.status_code == 200
    listed_ids = {item["id"] for item in listed.json()["items"]}
    assert str(owned_failure.id) in listed_ids
    assert str(foreign_failure.id) not in listed_ids

    denied = await httpx_client.post(
        f"/api/v1/admin/failed-predictions/{foreign_failure.id}/dismiss",
        headers=_headers(project_admin),
    )
    assert denied.status_code in {403, 404}

    allowed = await httpx_client.post(
        f"/api/v1/admin/failed-predictions/{owned_failure.id}/dismiss",
        headers=_headers(project_admin),
    )
    assert allowed.status_code == 200


# ── ML backends: project read for work roles, not platform viewers ───────────


async def test_ml_backend_read_scope(
    httpx_client, db_session: AsyncSession, super_admin
):
    admin, _ = super_admin
    project = await create_project(db_session, owner_id=admin.id, name="ML reads")

    annotator = await _employee(db_session, prefix="ml-anno")
    await _add_member(
        db_session, project_id=project.id, user=annotator, role="annotator", by=admin.id
    )
    viewer = await _employee(db_session, role="viewer", prefix="ml-view")
    await _add_member(
        db_session, project_id=project.id, user=viewer, role="viewer", by=admin.id
    )
    outsider = await _employee(db_session, prefix="ml-out")

    allowed = await httpx_client.get(
        f"/api/v1/projects/{project.id}/ml-backends", headers=_headers(annotator)
    )
    assert allowed.status_code == 200

    denied_viewer = await httpx_client.get(
        f"/api/v1/projects/{project.id}/ml-backends", headers=_headers(viewer)
    )
    assert denied_viewer.status_code == 403

    hidden = await httpx_client.get(
        f"/api/v1/projects/{project.id}/ml-backends", headers=_headers(outsider)
    )
    assert hidden.status_code == 404


# ── final-write freshness: revocation after the preflight is denied ──────────


async def test_canonical_annotation_write_predicate_denies_reviewer_annotation_phase():
    """The canonical predicate denies a reviewer outside a review adjustment."""

    task = SimpleNamespace(status="in_progress")
    access = ProjectAccess(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        platform_role="employee",
        project_role="reviewer",
        membership_id=uuid.uuid4(),
        membership_version=1,
        access_kind="member",
        capabilities=frozenset({ProjectCapability.REVIEW_WRITE.value}),
    )

    with pytest.raises(HTTPException) as exc:
        assert_annotation_write_allowed(task, access)
    assert exc.value.status_code == 403


async def test_reacquire_write_boundary_denies_revoked_membership(
    db_session: AsyncSession, super_admin
):
    """Re-locking the task at the write boundary sees a revocation immediately."""

    admin, _ = super_admin
    project = await create_project(db_session, owner_id=admin.id, name="Fresh ML")
    task = await create_task(db_session, project_id=project.id, status="in_progress")
    annotator = await _employee(db_session, prefix="fresh-ml")
    await _add_member(
        db_session, project_id=project.id, user=annotator, role="annotator", by=admin.id
    )
    task.assignee_id = annotator.id
    await db_session.flush()

    _task, access = await _reacquire_write_boundary(db_session, task.id, annotator)
    assert access.project_role == "annotator"

    await db_session.execute(
        delete(ProjectMember).where(
            ProjectMember.project_id == project.id,
            ProjectMember.user_id == annotator.id,
        )
    )
    await db_session.flush()

    with pytest.raises(HTTPException) as exc:
        await _reacquire_write_boundary(db_session, task.id, annotator)
    assert exc.value.status_code in {403, 404}


async def test_lock_tracker_task_denies_reviewer_and_revoked_member(
    db_session: AsyncSession, super_admin
):
    """Tracker mutations lock the task and reject reviewer/revoked authority."""

    admin, _ = super_admin
    item, task = await _seed_video_item_task(db_session, owner_id=admin.id)
    annotator = await _employee(db_session, prefix="fresh-trk")
    await _add_member(
        db_session,
        project_id=task.project_id,
        user=annotator,
        role="annotator",
        by=admin.id,
    )
    task.assignee_id = annotator.id
    reviewer = await _employee(db_session, prefix="fresh-trk-rev")
    await _add_member(
        db_session,
        project_id=task.project_id,
        user=reviewer,
        role="reviewer",
        by=admin.id,
    )
    task.reviewer_id = reviewer.id
    job = VideoTrackerJob(
        task_id=task.id,
        dataset_item_id=item.id,
        created_by=annotator.id,
        status="queued",
        job_kind="tracking",
        model_key="sam2",
        direction="forward",
        from_frame=0,
        to_frame=5,
        event_channel="project",
    )
    db_session.add(job)
    await db_session.flush()

    _task, _row, access = await _lock_visible_job_task_row(
        db_session, job.id, annotator
    )
    assert access.project_role == "annotator"

    # A project reviewer cannot perform annotation-phase tracker work.
    with pytest.raises(HTTPException) as reviewer_exc:
        await _lock_visible_job_task_row(db_session, job.id, reviewer)
    assert reviewer_exc.value.status_code == 403

    await db_session.execute(
        delete(ProjectMember).where(
            ProjectMember.project_id == task.project_id,
            ProjectMember.user_id == annotator.id,
        )
    )
    await db_session.flush()
    with pytest.raises(HTTPException) as revoked_exc:
        await _lock_visible_job_task_row(db_session, job.id, annotator)
    assert revoked_exc.value.status_code in {403, 404}


async def test_mask_qc_locked_review_access_denies_revoked_reviewer(
    db_session: AsyncSession, super_admin
):
    """The locked QC review boundary re-reads the membership before mutating."""

    admin, _ = super_admin
    project = await create_project(db_session, owner_id=admin.id, name="Fresh QC")
    task = await create_task(db_session, project_id=project.id, status="review")
    reviewer = await _employee(db_session, prefix="fresh-qc")
    await _add_member(
        db_session, project_id=project.id, user=reviewer, role="reviewer", by=admin.id
    )
    task.reviewer_id = reviewer.id
    await db_session.flush()

    _project, access = await _resolve_task_review_access(
        db_session, task=task, user=reviewer, lock=True
    )
    assert access.project_role == "reviewer"

    await db_session.execute(
        delete(ProjectMember).where(
            ProjectMember.project_id == project.id,
            ProjectMember.user_id == reviewer.id,
        )
    )
    await db_session.flush()
    with pytest.raises(HTTPException) as exc:
        await _resolve_task_review_access(
            db_session, task=task, user=reviewer, lock=True
        )
    assert exc.value.status_code in {403, 404}


async def test_task_view_query_and_count_share_project_role_scope(
    db_session: AsyncSession, super_admin
):
    """Both TaskViewService count and query apply the caller's project_role."""

    admin, _ = super_admin
    project = await create_project(db_session, owner_id=admin.id, name="Parity")
    annotator = await _employee(db_session, prefix="parity-a")
    await _add_member(
        db_session, project_id=project.id, user=annotator, role="annotator", by=admin.id
    )
    other = await _employee(db_session, prefix="parity-b")
    await _add_member(
        db_session, project_id=project.id, user=other, role="annotator", by=admin.id
    )

    batch_a = await create_batch(db_session, project_id=project.id, status="active")
    batch_a.annotator_id = annotator.id
    task_a = await create_task(db_session, project_id=project.id, status="pending")
    task_a.batch_id = batch_a.id
    batch_b = await create_batch(db_session, project_id=project.id, status="active")
    batch_b.annotator_id = other.id
    task_b = await create_task(db_session, project_id=project.id, status="pending")
    task_b.batch_id = batch_b.id
    await db_session.flush()

    svc = TaskViewService(db_session)
    rows, total = await svc.query_tasks(
        project_id=project.id,
        filter_json={},
        sort_json=[],
        columns_json=[],
        limit=50,
        offset=0,
        user=annotator,
        project=project,
        project_role="annotator",
    )
    count = await svc.count_for_filter(
        project.id,
        {},
        user=annotator,
        project=project,
        project_role="annotator",
    )
    assert {row[0].id for row in rows} == {task_a.id}
    assert total == count == 1

    # Missing project_role stays fail-closed for a non-privileged caller.
    rows_none, total_none = await svc.query_tasks(
        project_id=project.id,
        filter_json={},
        sort_json=[],
        columns_json=[],
        limit=50,
        offset=0,
        user=annotator,
        project=project,
        project_role=None,
    )
    assert rows_none == []
    assert total_none == 0

    # A manager sees the whole project regardless of membership role.
    _manager_rows, manager_total = await svc.query_tasks(
        project_id=project.id,
        filter_json={},
        sort_json=[],
        columns_json=[],
        limit=50,
        offset=0,
        user=admin,
        project=project,
        project_role=None,
    )
    assert manager_total == 2
