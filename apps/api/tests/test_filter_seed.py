"""Guarded fixture contract for the unified-filtering acceptance matrix."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select


pytestmark = pytest.mark.asyncio


async def _access_token(httpx_client, email: str) -> str:
    response = await httpx_client.post(
        "/api/v1/__test/seed/login", json={"email": email}
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


async def test_filtering_seed_real_queries_match_literal_memberships(httpx_client):
    response = await httpx_client.post("/api/v1/__test/seed/filtering")
    assert response.status_code == 200, response.text
    manifest = response.json()
    headers = {
        "Authorization": f"Bearer {await _access_token(httpx_client, 'anno@e2e.test')}"
    }

    def task_query(project_id: str, filter_json: dict):
        return httpx_client.post(
            f"/api/v1/projects/{project_id}/tasks/query",
            headers=headers,
            json={
                "filter_json": filter_json,
                "sort_json": [{"field": "task.display_id", "direction": "asc"}],
                "columns_json": [],
                "limit": 200,
                "offset": 0,
            },
        )

    image = manifest["image"]
    same_object_filter = {
        "op": "and",
        "rules": [
            {"field": "annotation.class_name", "op": "eq", "value": "car"},
            {
                "field": "annotation.attribute.bbox.color",
                "op": "eq",
                "value": "blue",
            },
        ],
    }
    same_object = await task_query(image["project_id"], same_object_filter)
    assert same_object.status_code == 200, same_object.text
    assert {item["id"] for item in same_object.json()["items"]} == set(
        image["expected"]["same_object_task_ids"]
    )

    missing_required = await task_query(
        image["project_id"],
        {
            "op": "and",
            "rules": [
                {"field": "annotation.class_name", "op": "eq", "value": "car"},
                {
                    "field": "annotation.attribute.bbox.color",
                    "op": "missing",
                },
            ],
        },
    )
    assert missing_required.status_code == 200, missing_required.text
    assert {item["id"] for item in missing_required.json()["items"]} == set(
        image["expected"]["missing_required_task_ids"]
    )

    video = manifest["video"]
    ai_or = await task_query(
        video["project_id"],
        {
            "op": "or",
            "rules": [
                {
                    "field": "ai.pending_prediction_shape_count",
                    "op": "gt",
                    "value": 0,
                },
                {
                    "field": "ai.pending_tracker_job_count",
                    "op": "gt",
                    "value": 0,
                },
            ],
        },
    )
    assert ai_or.status_code == 200, ai_or.text
    assert {item["id"] for item in ai_or.json()["items"]} == set(
        video["expected"]["ai_review_or_task_ids"]
    )
    ai_and = await task_query(
        video["project_id"],
        {
            "op": "and",
            "rules": [
                {
                    "field": "ai.pending_prediction_shape_count",
                    "op": "gt",
                    "value": 0,
                },
                {
                    "field": "ai.pending_tracker_job_count",
                    "op": "gt",
                    "value": 0,
                },
            ],
        },
    )
    assert ai_and.status_code == 200, ai_and.text
    assert {item["id"] for item in ai_and.json()["items"]} == set(
        video["expected"]["ai_review_and_task_ids"]
    )

    paging = manifest["paging"]
    page_one = await httpx_client.post(
        f"/api/v1/projects/{paging['project_id']}/data-manager/objects/query",
        headers=headers,
        json={
            "filter_json": {},
            "sort_json": [{"field": "annotation.updated_at", "direction": "desc"}],
            "columns_json": [],
            "limit": 100,
        },
    )
    assert page_one.status_code == 200, page_one.text
    page_one_body = page_one.json()
    assert page_one_body["total"] == 101
    assert [item["annotation_id"] for item in page_one_body["items"]] == paging[
        "expected_page_one_object_ids"
    ]
    page_two = await httpx_client.post(
        f"/api/v1/projects/{paging['project_id']}/data-manager/objects/query",
        headers=headers,
        json={
            "filter_json": {},
            "sort_json": [{"field": "annotation.updated_at", "direction": "desc"}],
            "columns_json": [],
            "limit": 100,
            "cursor": page_one_body["next_cursor"],
        },
    )
    assert page_two.status_code == 200, page_two.text
    assert [item["annotation_id"] for item in page_two.json()["items"]] == paging[
        "expected_page_two_object_ids"
    ]

    lidar = manifest["lidar"]
    tracks = await httpx_client.post(
        f"/api/v1/projects/{lidar['project_id']}/data-manager/tracks/query",
        headers=headers,
        json={"filter_json": {}, "sort_json": [], "columns_json": [], "limit": 200},
    )
    assert tracks.status_code == 200, tracks.text
    track_body = tracks.json()
    assert track_body["total"] == 101
    assert {item["track_id"] for item in track_body["items"]} == set(
        lidar["expected_visible_track_refs"]
    )


async def test_filtering_seed_manifest_matches_constructed_memberships(
    httpx_client, db_session
):
    from app.db.models.annotation import Annotation
    from app.db.models.async_job import AsyncJob
    from app.db.models.bug_report import BugReport
    from app.db.models.dataset import Dataset, DatasetItem, Scene
    from app.db.models.prediction import Prediction
    from app.db.models.project_task_view import ProjectTaskView
    from app.db.models.scene_track import SceneTrack
    from app.db.models.user_invitation import UserInvitation
    from app.db.models.video_tracker_job import VideoTrackerJob

    response = await httpx_client.post("/api/v1/__test/seed/filtering")
    assert response.status_code == 200, response.text
    manifest = response.json()

    assert set(manifest["users"]) == {"admin", "anno", "rev"}
    image = manifest["image"]
    video = manifest["video"]
    paging = manifest["paging"]
    lidar = manifest["lidar"]
    operations = manifest["operations"]

    assert image["expected"]["same_object_task_ids"] == [
        image["task_ids"]["same_object"]
    ]
    assert image["expected"]["cross_object_task_ids"] == []
    assert image["expected"]["missing_required_task_ids"] == [
        image["task_ids"]["required_missing"]
    ]
    assert len(image["expected"]["visible_task_ids"]) == 5
    assert image["schema"]["obsolete"] == "obsolete"
    assert set(image["saved_view_ids"]) == {
        "root_or",
        "required_nested",
        "object_filter",
        "removed_attribute",
    }

    image_project_id = UUID(image["project_id"])
    image_annotations = list(
        (
            await db_session.scalars(
                select(Annotation).where(Annotation.project_id == image_project_id)
            )
        ).all()
    )
    assert len(image_annotations) == 9
    assert (
        len(
            [
                row
                for row in image_annotations
                if row.is_active and not row.was_cancelled
            ]
        )
        == 7
    )
    assert {
        str(row.id)
        for row in image_annotations
        if not row.is_active or row.was_cancelled
    } == set(image["object_ids"]["cross_object"][2:])

    video_project_id = UUID(video["project_id"])
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Prediction)
            .where(Prediction.project_id == video_project_id)
        )
        == 5
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(VideoTrackerJob)
            .where(
                VideoTrackerJob.task_id.in_(
                    [UUID(value) for value in video["task_ids"].values()]
                )
            )
        )
        == 2
    )
    assert video["expected"]["ai_review_or_task_ids"] == [
        video["task_ids"][key] for key in ("detection_only", "tracker_only", "both")
    ]
    assert video["expected"]["ai_review_and_task_ids"] == [video["task_ids"]["both"]]

    paging_project_id = UUID(paging["project_id"])
    assert len(paging["object_ids"]) == 101
    assert paging["expected_page_one_object_ids"] == paging["object_ids"][:100]
    assert paging["expected_page_two_object_ids"] == paging["object_ids"][100:]
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Annotation)
            .where(
                Annotation.project_id == paging_project_id,
                Annotation.is_active.is_(True),
                Annotation.was_cancelled.is_(False),
            )
        )
        == 101
    )

    lidar_project_id = UUID(lidar["project_id"])
    assert len(lidar["track_refs"]) == 101
    assert len(set(lidar["track_refs"])) == 101
    assert lidar["hidden_track_ref"] not in lidar["expected_visible_track_refs"]
    assert set(lidar["saved_view_ids"]) == {"track_filter"}
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(SceneTrack)
            .where(SceneTrack.project_id == lidar_project_id)
        )
        == 102
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Annotation)
            .where(Annotation.project_id == lidar_project_id)
        )
        == 203
    )
    lidar_scene = await db_session.get(Scene, UUID(lidar["scene_id"]))
    assert lidar_scene is not None
    lidar_dataset = await db_session.get(Dataset, lidar_scene.dataset_id)
    assert lidar_dataset is not None
    assert lidar_dataset.file_count == 6
    lidar_items = list(
        (
            await db_session.scalars(
                select(DatasetItem).where(DatasetItem.dataset_id == lidar_dataset.id)
            )
        ).all()
    )
    camera_items = [item for item in lidar_items if item.file_type == "image"]
    assert len(lidar_items) == 6
    assert len(camera_items) == 3
    assert all(
        item.width == 640
        and item.height == 480
        and len((item.metadata_ or {}).get("calibration", {}).get("extrinsic", []))
        == 16
        and len((item.metadata_ or {}).get("calibration", {}).get("intrinsic", [])) == 9
        for item in camera_items
    )

    assert len(operations["project_ids"]) == 2
    assert len(operations["dataset_ids"]) == 2
    assert len(operations["template_ids"]) == 2
    assert len(operations["user_ids"]) == len(operations["user_emails"]) == 2
    assert len(operations["invitation_ids"]) == 4
    assert len(operations["job_ids"]) == 2
    assert len(operations["bug_ids"]) == 2
    assert len(operations["audit_ids"]) == 2
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Dataset)
            .where(Dataset.id.in_([UUID(value) for value in operations["dataset_ids"]]))
        )
        == 2
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ProjectTaskView)
            .where(
                ProjectTaskView.id.in_(
                    [UUID(value) for value in image["saved_view_ids"].values()]
                )
            )
        )
        == 4
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(UserInvitation)
            .where(
                UserInvitation.id.in_(
                    [UUID(value) for value in operations["invitation_ids"]]
                )
            )
        )
        == 4
    )
    seeded_jobs = {
        job.id: job
        for job in (
            await db_session.scalars(
                select(AsyncJob).where(
                    AsyncJob.id.in_([UUID(value) for value in operations["job_ids"]])
                )
            )
        ).all()
    }
    assert len(seeded_jobs) == 2
    completed_job = seeded_jobs[UUID(operations["job_ids"][0])]
    assert completed_job.kind == "batch_predict"
    assert completed_job.status == "completed"
    assert completed_job.progress_pct == 100
    assert completed_job.payload == {
        "model_key": "filter-detector",
        "prompt": "alpha complete",
    }
    assert completed_job.result == {"success_count": 2}
    assert completed_job.error_message is None
    assert completed_job.started_at is None and completed_job.completed_at is None

    failed_job = seeded_jobs[UUID(operations["job_ids"][1])]
    assert failed_job.kind == "export"
    assert failed_job.status == "failed"
    assert failed_job.progress_pct == 40
    assert failed_job.payload == {
        "model_key": "filter-export",
        "prompt": "beta failed",
    }
    assert failed_job.result == {}
    assert failed_job.error_message == "fixture failure"
    assert failed_job.started_at is None and failed_job.completed_at is None
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(BugReport)
            .where(BugReport.id.in_([UUID(value) for value in operations["bug_ids"]]))
        )
        == 2
    )


async def test_filtering_seed_cleanup_removes_manifest_rows_only(
    httpx_client, db_session
):
    from app.db.models.async_job import AsyncJob
    from app.db.models.audit_log import AuditLog
    from app.db.models.bug_report import BugReport
    from app.db.models.project import Project
    from app.db.models.project_task_view import ProjectTaskView
    from app.db.models.project_template import ProjectTemplate
    from app.db.models.user import User
    from app.db.models.user_invitation import UserInvitation

    response = await httpx_client.post("/api/v1/__test/seed/filtering")
    assert response.status_code == 200, response.text
    manifest = response.json()
    keep_user = User(
        id=uuid4(),
        email="filter-keep@example.test",
        name="Filter Keep",
        password_hash="x",
        role="super_admin",
        is_active=True,
    )
    db_session.add(keep_user)
    await db_session.flush()
    keep_project = Project(
        display_id="P-KEEP-FILTER",
        name="Keep Filter Project",
        type_label="Image detection",
        type_key="image-det",
        data_type="image",
        owner_id=keep_user.id,
        tool_bindings={},
    )
    db_session.add(keep_project)
    await db_session.flush()
    keep_template = ProjectTemplate(
        display_id="TPL-KEEP-FILTER",
        name="Keep Filter Template",
        type_label="Image detection",
        type_key="image-det",
        data_type="image",
        tool_bindings={},
        label_config={},
        scope="private",
        created_by=keep_user.id,
        source_project_id=keep_project.id,
    )
    keep_job = AsyncJob(
        kind="export",
        user_id=UUID(manifest["users"]["admin"]),
        status="completed",
        progress_pct=100,
        payload={"fixture": "unrelated"},
        result={},
    )
    keep_audit = AuditLog(
        actor_id=UUID(manifest["users"]["admin"]),
        actor_email=manifest["user_emails"]["admin"],
        actor_role="super_admin",
        action="filter.fixture.unrelated",
        target_type="project",
        target_id=str(keep_project.id),
        method="POST",
        path="/keep",
        status_code=200,
        detail_json={"fixture": "unrelated"},
    )
    db_session.add_all([keep_template, keep_job, keep_audit])
    await db_session.flush()

    cleanup = await httpx_client.post("/api/v1/__test/seed/cleanup")
    assert cleanup.status_code == 200, cleanup.text
    assert cleanup.json() == {"ok": True}

    project_ids = [
        manifest["image"]["project_id"],
        manifest["video"]["project_id"],
        manifest["paging"]["project_id"],
        manifest["lidar"]["project_id"],
        *manifest["operations"]["project_ids"],
    ]
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Project)
            .where(Project.id.in_([UUID(value) for value in project_ids]))
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Project)
            .where(Project.id == keep_project.id)
        )
        == 1
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ProjectTemplate)
            .where(ProjectTemplate.id == keep_template.id)
        )
        == 1
    )
    assert (
        await db_session.scalar(
            select(func.count()).select_from(User).where(User.id == keep_user.id)
        )
        == 1
    )
    assert (
        await db_session.scalar(
            select(func.count()).select_from(AsyncJob).where(AsyncJob.id == keep_job.id)
        )
        == 1
    )
    assert (
        await db_session.scalar(
            select(AsyncJob.user_id).where(AsyncJob.id == keep_job.id)
        )
        is None
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.id == keep_audit.id)
        )
        == 1
    )
    assert (
        await db_session.scalar(
            select(AuditLog.actor_id).where(AuditLog.id == keep_audit.id)
        )
        is None
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(User)
            .where(User.email.in_(manifest["operations"]["user_emails"]))
        )
        == 0
    )
    operation_rows = (
        (ProjectTemplate, manifest["operations"]["template_ids"]),
        (UserInvitation, manifest["operations"]["invitation_ids"]),
        (AsyncJob, manifest["operations"]["job_ids"]),
        (BugReport, manifest["operations"]["bug_ids"]),
        (AuditLog, manifest["operations"]["audit_ids"]),
        (ProjectTaskView, list(manifest["image"]["saved_view_ids"].values())),
        (ProjectTaskView, list(manifest["lidar"]["saved_view_ids"].values())),
    )
    for model, raw_ids in operation_rows:
        ids = (
            [int(value) for value in raw_ids]
            if model is AuditLog
            else [UUID(value) for value in raw_ids]
        )
        assert (
            await db_session.scalar(
                select(func.count()).select_from(model).where(model.id.in_(ids))
            )
            == 0
        )
