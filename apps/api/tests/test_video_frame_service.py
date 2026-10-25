from datetime import timedelta
import uuid
import pytest
from sqlalchemy import select, update

from app.db.models.dataset import (
    Dataset,
    DatasetItem,
    VideoChunk,
    VideoFrameCache,
    VideoFrameIndex,
    VideoSegment,
)
from app.db.models.annotation import Annotation
from app.db.models.audit_log import AuditLog
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.video_tracker_job import VideoTrackerJob
from app.application.video_timetable_rebuild import VideoTimetableRebuildWorkflow
from app.cli.video import rebuild_timetable as timetable_cli
from app.services.system_settings_service import SystemSettingsService
from tests.factory import build_tool_bindings
from app.services.video_frame_service import (
    list_chunks,
)
from app.services.video_context import VideoContext
from app.schemas.task import VideoMetadata
from tests.media_owner_fixtures import media_owner as media_owner


@pytest.fixture(autouse=True)
def bind_media_owner_to_test_savepoint(media_owner):
    """Keep API owner calls on the fixture SAVEPOINT and use deterministic S3."""
    return media_owner


async def _make_video_task(db_session, owner_id):
    project = Project(
        display_id=f"P-VFS-{uuid.uuid4().hex[:6]}",
        name="Video Frame Service Project",
        type_key="video-track",
        type_label="视频 · 时序追踪",
        data_type="video",
        owner_id=owner_id,
        tool_bindings=build_tool_bindings(["car"]),
    )
    dataset = Dataset(
        display_id=f"D-VFS-{uuid.uuid4().hex[:6]}",
        name="videos",
        data_type="video",
        created_by=owner_id,
    )
    db_session.add_all([project, dataset])
    await db_session.flush()
    item = DatasetItem(
        dataset_id=dataset.id,
        file_name="clip.mp4",
        file_path=f"videos/{uuid.uuid4()}/clip.mp4",
        file_type="video",
        width=1920,
        height=1080,
        thumbnail_path="posters/clip.webp",
        metadata_={
            "video": {
                "duration_ms": 3000,
                "fps": 30,
                "frame_count": 90,
                "playback_path": "playback/clip.mp4",
                "playback_codec": "h264",
                "poster_frame_path": "posters/clip.webp",
            }
        },
    )
    db_session.add(item)
    await db_session.flush()
    await db_session.info["media_owner_test_binding"].seed_video_item(item, owner_id)
    task = Task(
        project_id=project.id,
        dataset_item_id=item.id,
        display_id=f"T-VFS-{uuid.uuid4().hex[:6]}",
        file_name="clip.mp4",
        file_path=item.file_path,
        file_type="video",
        status="pending",
    )
    db_session.add(task)
    await db_session.flush()
    return task, item


async def _context_for_task(db, task):
    item = await db.get(DatasetItem, task.dataset_item_id)
    dataset = await db.get(Dataset, item.dataset_id)
    return VideoContext(
        metadata=VideoMetadata.model_validate(
            (item.metadata_ or {}).get("video") or {}
        ),
        dataset_item_id=item.id,
        source_file_path=item.file_path,
        thumbnail_path=item.thumbnail_path,
        source_width=item.width,
        source_height=item.height,
        task_id=task.id,
        project_id=task.project_id,
        actor_id=task.assignee_id or dataset.created_by,
    )


async def _context_for_item(db, dataset_item_id, task=None):
    item = await db.get(DatasetItem, dataset_item_id)
    dataset = await db.get(Dataset, item.dataset_id)
    return VideoContext(
        metadata=VideoMetadata.model_validate(
            (item.metadata_ or {}).get("video") or {}
        ),
        dataset_item_id=item.id,
        source_file_path=item.file_path,
        thumbnail_path=item.thumbnail_path,
        source_width=item.width,
        source_height=item.height,
        task_id=task.id if task is not None else None,
        project_id=task.project_id if task is not None else None,
        actor_id=(task.assignee_id or dataset.created_by)
        if task is not None
        else dataset.created_by,
    )


async def test_video_manifest_v2_exposes_service_urls(
    db_session, httpx_client, super_admin, monkeypatch
):
    user, token = super_admin
    task, item = await _make_video_task(db_session, user.id)
    monkeypatch.setattr(
        "app.services.video_segment_service.settings.video_segment_size_frames",
        30,
    )

    task_resp = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/video/manifest-v2",
        headers={"Authorization": f"Bearer {token}"},
    )
    video_resp = await httpx_client.get(
        f"/api/v1/videos/{item.id}/manifest",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert task_resp.status_code == 200
    assert video_resp.status_code == 200
    body = task_resp.json()
    assert body["task_id"] == str(task.id)
    assert body["dataset_item_id"] == str(item.id)
    await db_session.refresh(item)
    assert body["video_url"] == (
        "http://storage.local/" + item.metadata_["video"]["playback_path"]
    )
    assert body["poster_url"] == (
        "http://storage.local/" + item.metadata_["video"]["poster_frame_path"]
    )
    assert body["chunks_manifest_url"].endswith(f"/api/v1/tasks/{task.id}/video/chunks")
    assert body["frame_service_base"].endswith(f"/api/v1/tasks/{task.id}/video/frames")
    assert [
        (s["segment_index"], s["start_frame"], s["end_frame"]) for s in body["segments"]
    ] == [
        (0, 0, 29),
        (1, 30, 59),
        (2, 60, 89),
    ]
    assert video_resp.json()["dataset_item_id"] == str(item.id)


async def test_video_segments_facade_lists_lazy_segments(
    db_session, httpx_client, super_admin, monkeypatch
):
    user, token = super_admin
    task, item = await _make_video_task(db_session, user.id)
    monkeypatch.setattr(
        "app.services.video_segment_service.settings.video_segment_size_frames",
        45,
    )

    resp = await httpx_client.get(
        f"/api/v1/videos/{item.id}/segments",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["task_id"] == str(task.id)
    assert body["dataset_item_id"] == str(item.id)
    assert body["segment_size_frames"] == 45
    assert [(s["start_frame"], s["end_frame"]) for s in body["segments"]] == [
        (0, 44),
        (45, 89),
    ]


async def test_video_segment_claim_heartbeat_release(
    db_session, httpx_client, super_admin, monkeypatch
):
    user, token = super_admin
    task, _ = await _make_video_task(db_session, user.id)
    monkeypatch.setattr(
        "app.services.video_segment_service.settings.video_segment_lock_ttl_seconds",
        300,
    )

    segments_resp = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/video/segments",
        headers={"Authorization": f"Bearer {token}"},
    )
    segment_id = segments_resp.json()["segments"][0]["id"]

    claim_resp = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/video/segments/{segment_id}:claim",
        headers={"Authorization": f"Bearer {token}"},
    )
    heartbeat_resp = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/video/segments/{segment_id}:heartbeat",
        headers={"Authorization": f"Bearer {token}"},
    )
    release_resp = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/video/segments/{segment_id}:release",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert claim_resp.status_code == 200
    assert claim_resp.json()["status"] == "locked"
    assert claim_resp.json()["assignee_id"] == str(user.id)
    assert claim_resp.json()["locked_by"] == str(user.id)
    assert heartbeat_resp.status_code == 200
    assert heartbeat_resp.json()["status"] == "locked"
    assert release_resp.status_code == 200
    assert release_resp.json()["status"] == "assigned"
    assert release_resp.json()["locked_by"] is None


async def test_submitted_video_segment_stays_completed_after_refresh_and_release(
    db_session, httpx_client, super_admin, monkeypatch
):
    user, token = super_admin
    task, _ = await _make_video_task(db_session, user.id)
    task.reviewer_id = user.id
    task.reviewer_is_override = True
    project = await db_session.get(Project, task.project_id)
    project.video_collaboration = {"enabled": True, "overlap_frames": 2}
    monkeypatch.setattr(
        "app.services.video_segment_service.settings.video_segment_size_frames",
        45,
    )
    headers = {"Authorization": f"Bearer {token}"}

    listed = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/video/segments", headers=headers
    )
    segment_id = listed.json()["segments"][0]["id"]
    await httpx_client.post(
        f"/api/v1/tasks/{task.id}/video/segments/{segment_id}:claim",
        headers=headers,
    )
    submitted = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/video/segments/{segment_id}:submit",
        headers=headers,
    )
    refreshed = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/video/segments", headers=headers
    )
    released = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/video/segments/{segment_id}:release",
        headers=headers,
    )

    assert submitted.json()["status"] == "completed"
    assert refreshed.json()["segments"][0]["status"] == "completed"
    assert released.json()["status"] == "completed"

    # Completing the remaining segments starts a fresh review round; a prior
    # round's selected reviewer must not pin subsequent batch assignments.
    for segment in listed.json()["segments"][1:]:
        submitted = await httpx_client.post(
            f"/api/v1/tasks/{task.id}/video/segments/{segment['id']}:submit",
            headers=headers,
        )
        assert submitted.status_code == 200, submitted.text
    await db_session.refresh(task)
    assert task.status == "review"
    assert task.reviewer_id is None
    assert task.reviewer_is_override is False


async def test_video_submit_does_not_credit_shared_item_segment_assignees(
    db_session, httpx_client, super_admin, annotator, monkeypatch
):
    owner, token = super_admin
    foreign_user, _ = annotator
    task, item = await _make_video_task(db_session, owner.id)
    project = await db_session.get(Project, task.project_id)
    project.video_collaboration = {"enabled": True, "overlap_frames": 2}
    monkeypatch.setattr(
        "app.services.video_segment_service.settings.video_segment_size_frames", 45
    )
    headers = {"Authorization": f"Bearer {token}"}
    listed = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/video/segments", headers=headers
    )
    segment_ids = [uuid.UUID(row["id"]) for row in listed.json()["segments"]]
    assert len(segment_ids) == 2
    first = await db_session.get(VideoSegment, segment_ids[0])
    # Segment identity is global to the dataset item; this assignee does not
    # establish any contribution to this project's task.
    first.status = "completed"
    first.assignee_id = foreign_user.id
    await db_session.flush()
    claimed = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/video/segments/{segment_ids[1]}:claim",
        headers=headers,
    )
    assert claimed.status_code == 200, claimed.text
    submitted = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/video/segments/{segment_ids[1]}:submit",
        headers=headers,
    )
    assert submitted.status_code == 200, submitted.text
    audit = await db_session.scalar(
        select(AuditLog).where(
            AuditLog.target_id == str(task.id), AuditLog.action == "task.submit"
        )
    )
    assert audit is not None
    assert audit.detail_json["project_id"] == str(project.id)
    assert str(owner.id) in audit.detail_json["contributor_ids"]
    assert str(foreign_user.id) not in audit.detail_json["contributor_ids"]

    # A2 · the last segment submitter is recorded as a contributor and frozen
    # with the round; the shared dataset-item segment assignee is not credited.
    await db_session.refresh(task)
    assert task.status == "review"
    assert task.review_submitter_id == owner.id
    assert str(owner.id) in (task.review_contributor_ids or [])
    assert str(foreign_user.id) not in (task.review_contributor_ids or [])
    assert str(owner.id) in (task.annotation_contributor_ids or [])


async def test_segment_evidence_retains_each_actual_submitter(
    db_session, super_admin, monkeypatch
):
    from app.services.video_segment_service import ensure_segments, submit_segment
    from tests.factory import create_user

    owner, _ = super_admin
    other = await create_user(
        db_session, "super_admin", "segment-submitter@test.local", "Other submitter"
    )
    task, _ = await _make_video_task(db_session, owner.id)
    project = await db_session.get(Project, task.project_id)
    project.video_collaboration = {"enabled": True, "overlap_frames": 2}
    monkeypatch.setattr(
        "app.services.video_segment_service.settings.video_segment_size_frames", 45
    )
    ctx = await _context_for_task(db_session, task)
    segments = await ensure_segments(db_session, ctx)
    assert len(segments) == 2

    await submit_segment(
        db_session, ctx, segments[0].id, owner, privileged=True, task=task
    )
    assert task.status != "review"
    assert task.annotation_contributor_ids == [str(owner.id)]
    await submit_segment(
        db_session, ctx, segments[1].id, other, privileged=True, task=task
    )
    await db_session.refresh(task)
    assert task.status == "review"
    assert task.review_round_id is not None
    assert task.review_submitter_id == other.id
    assert task.review_contributor_ids == sorted([str(owner.id), str(other.id)])


@pytest.mark.parametrize("operation", ["submit", "reopen"])
@pytest.mark.parametrize(
    "binding",
    ["different_task", "missing_task", "task_on_taskless", "transient_on_taskless"],
)
async def test_segment_task_binding_mismatch_fails_before_database_access(
    db_session, test_engine, super_admin, operation, binding
):
    from sqlalchemy import event

    from app.services.video_segment_service import reopen_segment, submit_segment

    user, _ = super_admin
    task, item = await _make_video_task(db_session, user.id)
    if binding in {"different_task", "missing_task"}:
        ctx = await _context_for_task(db_session, task)
        bound_task = Task(
            id=uuid.uuid4(),
            project_id=task.project_id,
            dataset_item_id=item.id,
            display_id=f"T-VFS-{uuid.uuid4().hex[:6]}",
            file_name="clip.mp4",
            file_path="videos/clip.mp4",
            file_type="video",
            status="pending",
        )
        if binding == "missing_task":
            bound_task = None
    else:
        ctx = await _context_for_item(db_session, item.id)
        bound_task = task
        # The binding guard must inspect the loaded identity without triggering
        # an async refresh when the Task's mapped id attribute has expired.
        db_session.expire(bound_task, ["id"])
        if binding == "transient_on_taskless":
            bound_task = Task()

    statements = []

    def count_statement(_conn, _cursor, statement, *_args):
        statements.append(statement)

    event.listen(test_engine.sync_engine, "before_cursor_execute", count_statement)
    try:
        with pytest.raises(ValueError):
            if operation == "submit":
                await submit_segment(
                    db_session,
                    ctx,
                    uuid.uuid4(),
                    user,
                    privileged=True,
                    task=bound_task,
                )
            else:
                await reopen_segment(db_session, ctx, uuid.uuid4(), task=bound_task)
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", count_statement)

    assert statements == []
    assert task.status == "pending"
    assert list(db_session.new) == []
    assert list(db_session.dirty) == []


async def test_video_context_values_survive_rollback_without_lazy_loads(
    db_session, test_engine, super_admin
):
    from sqlalchemy import event

    user, _ = super_admin
    task, item = await _make_video_task(db_session, user.id)
    ctx = await _context_for_task(db_session, task)
    expected_item_id = item.id
    expected_source_file_path = item.file_path
    expected_task_id = task.id
    expected_project_id = task.project_id
    await db_session.commit()
    await db_session.execute(select(1))

    await db_session.rollback()
    await db_session.connection()
    statements = []

    def count_statement(_conn, _cursor, statement, *_args):
        statements.append(statement)

    event.listen(test_engine.sync_engine, "before_cursor_execute", count_statement)
    try:
        assert ctx.dataset_item_id == expected_item_id
        assert ctx.task_id == expected_task_id
        assert ctx.project_id == expected_project_id
        assert ctx.source_file_path == expected_source_file_path
        assert ctx.thumbnail_path == "posters/clip.webp"
        assert ctx.metadata.fps == 30
        assert statements == []
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", count_statement)

    assert statements == []


async def test_taskless_video_context_keeps_taskless_segment_behavior(
    db_session, super_admin
):
    from app.services.video_segment_service import (
        claim_segment,
        ensure_segments,
        heartbeat_segment,
        release_segment,
        reopen_segment,
        submit_segment,
        unassign_segment,
    )

    user, _ = super_admin
    task, item = await _make_video_task(db_session, user.id)
    ctx = await _context_for_item(db_session, item.id)
    assert ctx.task_id is None
    segments = await ensure_segments(db_session, ctx)
    segment_id = segments[0].id

    claimed = await claim_segment(db_session, ctx, segment_id, user, privileged=True)
    assert claimed.status == "locked"
    heartbeat = await heartbeat_segment(
        db_session, ctx, segment_id, user, privileged=True
    )
    assert heartbeat.status == "locked"
    released = await release_segment(db_session, ctx, segment_id, user, privileged=True)
    assert released.status == "assigned"
    assert released.locked_by is None
    await claim_segment(db_session, ctx, segment_id, user, privileged=True)

    result = await submit_segment(db_session, ctx, segment_id, user, privileged=True)

    assert result.status == "completed"
    reopened = await reopen_segment(db_session, ctx, segment_id, actor_id=user.id)
    assert reopened.status == "assigned"
    unassigned = await unassign_segment(db_session, ctx, segment_id, actor_id=user.id)
    assert unassigned.status == "open"
    assert unassigned.assignee_id is None
    assert task.status == "pending"


async def test_mask_validation_reuses_context_source_dimensions(
    db_session, test_engine, super_admin
):
    from dataclasses import replace

    from sqlalchemy import event

    from app.services.raster_mask_storage import validate_mask_geometry_for_task

    owner, _ = super_admin
    task, item = await _make_video_task(db_session, owner.id)
    item.width, item.height = 3, 2
    item.metadata_ = {
        **item.metadata_,
        "video": {**item.metadata_["video"], "width": 99, "height": 99},
    }
    await db_session.flush()
    ctx = await _context_for_task(db_session, task)
    db_session.expunge(item)
    geometry = {
        "type": "video_track_mask",
        "keyframes": [{"frame_index": 0, "mask": {"size": [2, 3]}}],
    }
    statements = []

    def count_statement(_conn, _cursor, statement, *_args):
        statements.append(statement)

    event.listen(test_engine.sync_engine, "before_cursor_execute", count_statement)
    try:
        await validate_mask_geometry_for_task(
            db_session, task, geometry, video_context=ctx
        )
        geometry["keyframes"][0]["mask"]["size"] = [99, 99]
        with pytest.raises(ValueError):
            await validate_mask_geometry_for_task(
                db_session, task, geometry, video_context=ctx
            )
        geometry["keyframes"][0]["mask"]["size"] = [2, 3]
        with pytest.raises(ValueError):
            await validate_mask_geometry_for_task(
                db_session,
                task,
                geometry,
                video_context=replace(ctx, dataset_item_id=uuid.uuid4()),
            )
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", count_statement)

    assert statements == []


async def test_video_collaboration_derives_overlap_work_ranges(
    db_session, httpx_client, super_admin, monkeypatch
):
    user, token = super_admin
    task, _ = await _make_video_task(db_session, user.id)
    project = await db_session.get(Project, task.project_id)
    project.video_collaboration = {"enabled": True, "overlap_frames": 10}
    monkeypatch.setattr(
        "app.services.video_segment_service.settings.video_segment_size_frames", 30
    )
    await db_session.flush()

    response = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/video/segments",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["collaboration_enabled"] is True
    assert body["overlap_frames"] == 10
    assert [
        (row["work_start_frame"], row["work_end_frame"]) for row in body["segments"]
    ] == [(0, 34), (25, 64), (55, 89)]


async def test_video_collaboration_scopes_annotation_write_and_read_to_segment(
    db_session, httpx_client, super_admin, monkeypatch
):
    user, token = super_admin
    task, _ = await _make_video_task(db_session, user.id)
    project = await db_session.get(Project, task.project_id)
    project.video_collaboration = {"enabled": True, "overlap_frames": 10}
    monkeypatch.setattr(
        "app.services.video_segment_service.settings.video_segment_size_frames", 30
    )
    await db_session.flush()
    headers = {"Authorization": f"Bearer {token}"}

    segments = (
        await httpx_client.get(
            f"/api/v1/tasks/{task.id}/video/segments", headers=headers
        )
    ).json()["segments"]
    segment = segments[0]
    claim = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/video/segments/{segment['id']}:claim",
        headers=headers,
    )
    assert claim.status_code == 200

    create = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations",
        headers=headers,
        json={
            "video_segment_id": segment["id"],
            "annotation_type": "video_bbox",
            "tool_unit_id": "bbox",
            "class_name": "car",
            "geometry": {
                "type": "video_bbox",
                "frame_index": 34,
                "x": 0.1,
                "y": 0.1,
                "w": 0.2,
                "h": 0.2,
            },
        },
    )
    assert create.status_code == 201
    assert create.json()["video_segment_id"] == segment["id"]

    missing_scope = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/annotations", headers=headers
    )
    scoped = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/annotations?video_segment_id={segment['id']}",
        headers=headers,
    )
    outside = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations",
        headers=headers,
        json={
            "video_segment_id": segment["id"],
            "annotation_type": "video_bbox",
            "tool_unit_id": "bbox",
            "class_name": "car",
            "geometry": {
                "type": "video_bbox",
                "frame_index": 35,
                "x": 0.1,
                "y": 0.1,
                "w": 0.2,
                "h": 0.2,
            },
        },
    )

    assert missing_scope.status_code == 422
    assert scoped.status_code == 200
    assert [row["id"] for row in scoped.json()] == [create.json()["id"]]
    assert outside.status_code == 422
    assert outside.json()["detail"]["reason"] == "video_geometry_outside_segment"


async def test_video_collaboration_public_enablement_is_available_for_empty_video_project(
    db_session, httpx_client, super_admin
):
    user, token = super_admin
    task, _ = await _make_video_task(db_session, user.id)

    response = await httpx_client.patch(
        f"/api/v1/projects/{task.project_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"video_collaboration": {"enabled": True, "overlap_frames": 10}},
    )

    assert response.status_code == 200
    assert response.json()["video_collaboration"] == {
        "enabled": True,
        "overlap_frames": 10,
    }


async def test_video_collaboration_revalidates_tool_binding_updates(
    db_session, httpx_client, super_admin
):
    user, token = super_admin
    task, _ = await _make_video_task(db_session, user.id)
    project = await db_session.get(Project, task.project_id)
    project.video_collaboration = {"enabled": True, "overlap_frames": 10}
    await db_session.flush()

    response = await httpx_client.patch(
        f"/api/v1/projects/{task.project_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "tool_bindings": {
                "lidar_box_3d": {
                    "enabled": True,
                    "classes": [{"name": "car"}],
                    "attribute_schema": {"fields": []},
                }
            }
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == {
        "reason": "video_collaboration_invalid_tools",
        "tool_units": ["lidar_box_3d"],
    }


async def test_video_segment_non_assignee_cannot_claim_assigned_segment(
    db_session, httpx_client, annotator, reviewer
):
    assigned_user, _ = annotator
    review_user, review_token = reviewer
    task, item = await _make_video_task(db_session, assigned_user.id)
    batch = TaskBatch(
        project_id=task.project_id,
        dataset_id=item.dataset_id,
        display_id=f"B-VFS-{uuid.uuid4().hex[:6]}",
        name="Video batch",
        status="active",
        annotator_id=assigned_user.id,
        assigned_user_ids=[str(assigned_user.id), str(review_user.id)],
    )
    db_session.add_all(
        [
            ProjectMember(
                project_id=task.project_id,
                user_id=assigned_user.id,
                role="annotator",
                assigned_by=assigned_user.id,
            ),
            ProjectMember(
                project_id=task.project_id,
                user_id=review_user.id,
                role="reviewer",
                assigned_by=assigned_user.id,
            ),
            batch,
        ]
    )
    await db_session.flush()
    task.batch_id = batch.id
    segment = VideoSegment(
        dataset_item_id=item.id,
        segment_index=0,
        start_frame=0,
        end_frame=89,
        assignee_id=assigned_user.id,
        status="assigned",
    )
    db_session.add(segment)
    await db_session.flush()

    resp = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/video/segments/{segment.id}:claim",
        headers={"Authorization": f"Bearer {review_token}"},
    )

    assert resp.status_code == 403


async def test_video_chunks_create_pending_rows_and_enqueue(
    db_session, httpx_client, super_admin, monkeypatch, media_dispatcher
):
    user, token = super_admin
    task, item = await _make_video_task(db_session, user.id)

    resp = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/video/chunks?from_frame=0&to_frame=65",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200
    body = resp.json()
    await db_session.refresh(item)
    assert body["fallback_video_url"] == (
        "http://storage.local/" + item.metadata_["video"]["playback_path"]
    )
    assert [chunk["chunk_id"] for chunk in body["chunks"]] == [0, 1]
    assert {chunk["status"] for chunk in body["chunks"]} == {"pending"}
    assert media_dispatcher.video_chunks == [(item.id, [0, 1])]

    rows = (
        await db_session.execute(
            VideoChunk.__table__.select().where(VideoChunk.dataset_item_id == item.id)
        )
    ).all()
    assert len(rows) == 2


async def test_video_warmup_zero_skips_neighbor_enqueue(
    db_session, super_admin, media_dispatcher
):
    from aap_assets_media.contract import UsageRequest

    user, _ = super_admin
    _, item = await _make_video_task(db_session, user.id)
    item_id = item.id
    await SystemSettingsService.set_many(
        db_session, {"video_chunk_warmup_lookahead": 0}, actor_id=user.id
    )
    await db_session.flush()
    preferences = await db_session.info["media_owner_test_binding"].native.call(
        "read_media_preferences", UsageRequest()
    )
    assert preferences.warmup_lookahead == 0

    ctx = await _context_for_item(db_session, item.id)
    await list_chunks(db_session, ctx, 0, 0)

    assert media_dispatcher.video_chunks == [(item_id, [0])]
    rows = (
        await db_session.execute(
            VideoChunk.__table__.select().where(VideoChunk.dataset_item_id == item_id)
        )
    ).all()
    assert [row.chunk_id for row in rows] == [0]


@pytest.mark.parametrize("neighbor_status", [None, "pending", "ready", "failed"])
async def test_video_warmup_dispatches_new_or_failed_neighbors_only(
    db_session, super_admin, media_dispatcher, monkeypatch, neighbor_status
):
    from aap_assets_media.contract import UsageRequest

    user, _ = super_admin
    _, item = await _make_video_task(db_session, user.id)
    item_id = item.id
    monkeypatch.setattr(
        "app.services.video_frame_service.settings.video_chunk_size_frames", 30
    )
    await SystemSettingsService.set_many(
        db_session, {"video_chunk_warmup_lookahead": 1}, actor_id=user.id
    )
    preferences = await db_session.info["media_owner_test_binding"].native.call(
        "read_media_preferences", UsageRequest()
    )
    assert preferences.warmup_lookahead == 1
    if neighbor_status is not None:
        await db_session.info["media_owner_test_binding"].seed_chunk(
            item,
            user.id,
            1,
            status=neighbor_status,
            chunk_size_frames=30,
        )
    ctx = await _context_for_item(db_session, item.id)

    await list_chunks(db_session, ctx, 0, 0)

    expected = [(item_id, [0])]
    if neighbor_status in {None, "failed"}:
        expected.append((item_id, [1]))
    assert media_dispatcher.video_chunks == expected


@pytest.mark.parametrize("path", ["chunks?from_frame=0&to_frame=0", "chunks/0"])
async def test_warmup_setting_failure_does_not_expire_the_primary_response(
    db_session, httpx_client, super_admin, monkeypatch, media_dispatcher, path
):
    user, token = super_admin
    task, item = await _make_video_task(db_session, user.id)
    task_id = task.id
    await db_session.info["media_owner_test_binding"].seed_chunk(
        item, user.id, 0, status="ready"
    )

    from aap_assets_media.contract import MediaFailure
    from app.services.assets_media import SqlAlchemyMediaDatabase

    original_call = SqlAlchemyMediaDatabase.call

    async def unavailable(self, operation, request):
        if operation == "read_media_preferences":
            raise MediaFailure("storage_unavailable")
        return await original_call(self, operation, request)

    monkeypatch.setattr(SqlAlchemyMediaDatabase, "call", unavailable)
    response = await httpx_client.get(
        f"/api/v1/tasks/{task_id}/video/{path}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    chunks = body["chunks"] if "chunks" in body else [body]
    assert [chunk["chunk_id"] for chunk in chunks] == [0]
    assert chunks[0]["status"] == "ready"
    assert media_dispatcher.video_chunks == []


async def test_video_chunk_api_exposes_generation_diagnostics(
    db_session, httpx_client, super_admin, monkeypatch
):
    user, token = super_admin
    task, item = await _make_video_task(db_session, user.id)
    await db_session.info["media_owner_test_binding"].seed_chunk(
        item,
        user.id,
        0,
        status="ready",
        start_pts_ms=0,
        end_pts_ms=1967,
        generation_mode="smart_copy",
        diagnostics={
            "keyframe_aligned": True,
            "start_byte_offset": 100,
            "end_byte_offset": 9000,
            "smart_copy_eligible": True,
        },
    )

    resp = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/video/chunks/0",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["generation_mode"] == "smart_copy"
    assert body["diagnostics"]["source_codec"] == "h264"
    assert body["diagnostics"]["keyframe_aligned"] is True
    assert body["diagnostics"]["smart_copy_eligible"] is True


async def test_video_chunk_samples_returns_description_and_stored_order(
    db_session, httpx_client, super_admin
):
    user, token = super_admin
    task, item = await _make_video_task(db_session, user.id)
    # 后端 sample manifest 保持 packet decode order,frame_index 按 PTS presentation rank;
    # B 帧下 pts 非单调(frame 10 key → frame 12 → frame 11)。前端按 timestamp 解码,不能假设
    # 数组顺序即展示顺序,故此处断言存储顺序原样回传。
    samples = [
        {
            "frame_index": 0,
            "pts_ms": 0,
            "duration_ms": 33,
            "is_keyframe": True,
            "size_bytes": 5000,
            "offset_in_chunk": 48,
        },
        {
            "frame_index": 2,
            "pts_ms": 67,
            "duration_ms": 33,
            "is_keyframe": False,
            "size_bytes": 1200,
            "offset_in_chunk": 5048,
        },
        {
            "frame_index": 1,
            "pts_ms": 33,
            "duration_ms": 33,
            "is_keyframe": False,
            "size_bytes": 800,
            "offset_in_chunk": 6248,
        },
    ]
    await db_session.info["media_owner_test_binding"].seed_chunk(
        item,
        user.id,
        0,
        status="ready",
        start_pts_ms=0,
        end_pts_ms=100,
        generation_mode="smart_copy",
        diagnostics={
            "codec_string": "avc1.64001f",
            "description": "AAAA",
            "width": 1920,
            "height": 1080,
            "samples": samples,
        },
    )

    resp = await httpx_client.get(
        f"/api/v1/videos/{item.id}/chunks/0/samples",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dataset_item_id"] == str(item.id)
    assert body["chunk_id"] == 0
    assert body["codec_string"] == "avc1.64001f"
    assert body["description"] == "AAAA"
    assert body["width"] == 1920 and body["height"] == 1080
    # samples 保持存储(decode)顺序,不按 pts 重排。
    assert [s["frame_index"] for s in body["samples"]] == [0, 2, 1]
    assert [s["offset_in_chunk"] for s in body["samples"]] == [48, 5048, 6248]
    assert [s["pts_ms"] for s in body["samples"]] == [0, 67, 33]
    assert body["samples"][0]["is_keyframe"] is True


async def test_video_chunk_samples_404_when_no_samples(
    db_session, httpx_client, super_admin
):
    user, token = super_admin
    task, item = await _make_video_task(db_session, user.id)
    await db_session.info["media_owner_test_binding"].seed_chunk(
        item,
        user.id,
        0,
        status="ready",
        start_pts_ms=0,
        end_pts_ms=300,
        generation_mode="smart_copy",
        diagnostics={"source_codec": "h264"},  # chunk manifest has no samples.
    )

    resp = await httpx_client.get(
        f"/api/v1/videos/{item.id}/chunks/0/samples",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "samples_not_available"


async def test_video_chunk_samples_404_when_item_invisible(
    db_session, httpx_client, project_admin, annotator
):
    owner, _ = project_admin
    _other_user, token = annotator
    _task, item = await _make_video_task(db_session, owner.id)
    # 资源真实存在，但当前用户既不是所有者，也不是项目成员。
    resp = await httpx_client.get(
        f"/api/v1/videos/{item.id}/chunks/0/samples",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


async def test_video_frame_ready_returns_cached_url_without_enqueue(
    db_session, httpx_client, super_admin, monkeypatch, media_dispatcher
):
    user, token = super_admin
    task, item = await _make_video_task(db_session, user.id)
    await db_session.info["media_owner_test_binding"].seed_frame(
        item, user.id, 12, width=512, format="webp", status="ready"
    )

    resp = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/video/frames/12?format=webp&w=512",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "private, max-age=3600"
    body = resp.json()
    assert body["status"] == "ready"
    assert body["url"].endswith("/frame.webp")
    assert media_dispatcher.video_frames == []


async def test_video_frame_pending_does_not_enqueue_duplicate_worker(
    db_session, httpx_client, super_admin, media_dispatcher
):
    user, token = super_admin
    task, item = await _make_video_task(db_session, user.id)
    await db_session.info["media_owner_test_binding"].seed_frame(
        item, user.id, 12, width=512, format="webp", status="pending"
    )

    resp = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/video/frames/12?format=webp&w=512",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 202
    assert resp.headers["cache-control"] == "no-store"
    assert resp.json()["status"] == "pending"
    assert media_dispatcher.video_frames == []


async def test_video_frame_prefetch_creates_missing_rows(
    db_session, httpx_client, super_admin, media_dispatcher
):
    user, token = super_admin
    task, item = await _make_video_task(db_session, user.id)

    resp = await httpx_client.post(
        f"/api/v1/videos/{item.id}/frames:prefetch",
        headers={"Authorization": f"Bearer {token}"},
        json={"frame_indices": [3, 3, 4], "width": 320, "format": "jpeg"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert [frame["frame_index"] for frame in body["frames"]] == [3, 4]
    assert {frame["status"] for frame in body["frames"]} == {"pending"}
    assert media_dispatcher.video_frames == [
        (
            item.id,
            [
                {"frame_index": 3, "width": 320, "format": "jpeg"},
                {"frame_index": 4, "width": 320, "format": "jpeg"},
            ],
        )
    ]


async def test_video_frame_retry_resets_failed_rows_only(
    db_session, httpx_client, super_admin, media_dispatcher
):
    user, token = super_admin
    task, item = await _make_video_task(db_session, user.id)
    binding = db_session.info["media_owner_test_binding"]
    await binding.seed_frame(
        item,
        user.id,
        7,
        width=512,
        format="webp",
        status="failed",
        error="ffmpeg failed",
    )
    await binding.seed_frame(item, user.id, 8, width=512, format="webp", status="ready")

    resp = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/video/frames:retry",
        headers={"Authorization": f"Bearer {token}"},
        json={"frame_indices": [7, 8], "width": 512, "format": "webp"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert [frame["frame_index"] for frame in body["frames"]] == [7]
    assert body["frames"][0]["status"] == "pending"
    assert media_dispatcher.video_frames == [
        (item.id, [{"frame_index": 7, "width": 512, "format": "webp"}])
    ]


async def test_video_asset_failures_list_metadata_chunk_and_frame_errors(
    db_session, httpx_client, super_admin
):
    user, token = super_admin
    task, item = await _make_video_task(db_session, user.id)
    binding = db_session.info["media_owner_test_binding"]
    await binding.seed_failed_metadata(item, user.id)
    video = dict(item.metadata_["video"])
    video["probe_error"] = "ffprobe failed"
    item.metadata_ = {**item.metadata_, "video": video}
    await binding.seed_chunk(
        item, user.id, 2, status="failed", error="chunk failed", chunk_size_frames=30
    )
    await binding.seed_frame(
        item,
        user.id,
        12,
        width=320,
        format="webp",
        status="failed",
        error="frame failed",
    )

    resp = await httpx_client.get(
        "/api/v1/storage/video-assets/failures",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    by_type = {item["asset_type"]: item for item in body["items"]}
    assert by_type["probe"]["error"] == "ffprobe failed"
    assert by_type["probe"]["task_display_id"] == task.display_id
    assert by_type["chunk"]["chunk_id"] == 2
    assert by_type["frame"]["frame_index"] == 12
    assert by_type["frame"]["width"] == 320


async def test_video_asset_retry_queues_existing_media_tasks(
    db_session, httpx_client, super_admin, media_dispatcher
):
    user, token = super_admin
    _, item = await _make_video_task(db_session, user.id)
    binding = db_session.info["media_owner_test_binding"]
    await binding.seed_failed_metadata(item, user.id)
    await binding.seed_chunk(
        item, user.id, 1, status="failed", error="chunk failed", chunk_size_frames=30
    )
    await binding.seed_frame(
        item,
        user.id,
        8,
        width=320,
        format="jpeg",
        status="failed",
        error="frame failed",
    )
    chunk = await db_session.scalar(
        select(VideoChunk).where(
            VideoChunk.dataset_item_id == item.id, VideoChunk.chunk_id == 1
        )
    )
    frame = await db_session.scalar(
        select(VideoFrameCache).where(
            VideoFrameCache.dataset_item_id == item.id,
            VideoFrameCache.frame_index == 8,
            VideoFrameCache.width == 320,
            VideoFrameCache.format == "jpeg",
        )
    )

    metadata_resp = await httpx_client.post(
        "/api/v1/storage/video-assets/retry",
        headers={"Authorization": f"Bearer {token}"},
        json={"asset_type": "poster", "dataset_item_id": str(item.id)},
    )
    chunk_resp = await httpx_client.post(
        "/api/v1/storage/video-assets/retry",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "asset_type": "chunk",
            "dataset_item_id": str(item.id),
            "chunk_id": 1,
        },
    )
    frame_resp = await httpx_client.post(
        "/api/v1/storage/video-assets/retry",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "asset_type": "frame",
            "dataset_item_id": str(item.id),
            "frame_index": 8,
            "width": 320,
            "format": "jpeg",
        },
    )

    assert metadata_resp.status_code == 202
    assert chunk_resp.status_code == 202
    assert frame_resp.status_code == 202
    await db_session.refresh(chunk)
    await db_session.refresh(frame)
    assert chunk.status == "pending"
    assert chunk.error is None
    assert chunk.generation_mode is None
    assert chunk.diagnostics == {}
    assert chunk.storage_key is None
    assert chunk.byte_size is None
    assert frame.status == "pending"
    assert frame.error is None
    assert (frame.dataset_item_id, frame.frame_index, frame.width, frame.format) == (
        item.id,
        8,
        320,
        "jpeg",
    )
    assert frame.storage_key is None
    assert frame.byte_size is None


async def test_video_asset_retry_preserves_missing_asset_errors(
    db_session, httpx_client, super_admin, media_dispatcher
):
    user, token = super_admin
    _, item = await _make_video_task(db_session, user.id)
    headers = {"Authorization": f"Bearer {token}"}

    missing_chunk_id = await httpx_client.post(
        "/api/v1/storage/video-assets/retry",
        headers=headers,
        json={"asset_type": "chunk", "dataset_item_id": str(item.id)},
    )
    missing_chunk = await httpx_client.post(
        "/api/v1/storage/video-assets/retry",
        headers=headers,
        json={
            "asset_type": "chunk",
            "dataset_item_id": str(item.id),
            "chunk_id": 99,
        },
    )
    missing_frame_fields = await httpx_client.post(
        "/api/v1/storage/video-assets/retry",
        headers=headers,
        json={"asset_type": "frame", "dataset_item_id": str(item.id)},
    )
    missing_frame = await httpx_client.post(
        "/api/v1/storage/video-assets/retry",
        headers=headers,
        json={
            "asset_type": "frame",
            "dataset_item_id": str(item.id),
            "frame_index": 8,
            "width": 320,
            "format": "webp",
        },
    )
    missing_item = await httpx_client.post(
        "/api/v1/storage/video-assets/retry",
        headers=headers,
        json={"asset_type": "poster", "dataset_item_id": str(uuid.uuid4())},
    )

    assert missing_chunk_id.status_code == 400
    assert missing_chunk.status_code == 404
    assert missing_frame_fields.status_code == 400
    assert missing_frame.status_code == 404
    assert missing_item.status_code == 404


async def test_video_asset_retry_commit_survives_dispatch_failure(
    db_session, httpx_client, super_admin, media_dispatcher, monkeypatch
):
    user, token = super_admin
    _, item = await _make_video_task(db_session, user.id)
    binding = db_session.info["media_owner_test_binding"]
    await binding.seed_chunk(
        item,
        user.id,
        1,
        status="failed",
        error="chunk failed",
        chunk_size_frames=30,
        generation_mode="transcode",
        diagnostics={"output_codec": "avc1.4d001f"},
    )
    chunk = await db_session.scalar(
        select(VideoChunk).where(
            VideoChunk.dataset_item_id == item.id, VideoChunk.chunk_id == 1
        )
    )

    def fail_dispatch(_item_id, _chunk_ids):
        raise RuntimeError("dispatch unavailable")

    monkeypatch.setattr(media_dispatcher, "dispatch_video_chunks", fail_dispatch)
    response = await httpx_client.post(
        "/api/v1/storage/video-assets/retry",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "asset_type": "chunk",
            "dataset_item_id": str(item.id),
            "chunk_id": 1,
        },
    )
    assert response.status_code == 202

    await db_session.refresh(chunk)
    assert chunk.status == "pending"
    assert chunk.error is None
    assert chunk.generation_mode is None
    assert chunk.diagnostics == {}


async def test_video_asset_retry_accepts_any_managed_linked_project(
    db_session, httpx_client, project_admin, media_dispatcher
):
    """一个 dataset item 可被多个项目的 task 关联：只要其中任一链接项目是该
    管理员合法管理的，重试就应放行（与失败列表 any-owned-project 作用域一致），
    而不是依赖 LIMIT 1 任选一条链接行。"""

    owner, token = project_admin
    owned_task, item = await _make_video_task(db_session, owner.id)
    await db_session.info["media_owner_test_binding"].seed_failed_metadata(
        item, owner.id
    )

    # Second project owned by someone else, linking the same dataset item.
    from tests.factory import create_user

    other_user = await create_user(
        db_session, "project_admin", f"vfs-other-{uuid.uuid4().hex[:6]}@test.local", "O"
    )
    other_project = Project(
        display_id=f"P-VFS-{uuid.uuid4().hex[:6]}",
        name="Other Project",
        type_key="video-track",
        type_label="视频 · 时序追踪",
        data_type="video",
        owner_id=other_user.id,
        tool_bindings=build_tool_bindings(["car"]),
    )
    db_session.add(other_project)
    await db_session.flush()
    other_task = Task(
        project_id=other_project.id,
        dataset_item_id=item.id,
        display_id=f"T-VFS-{uuid.uuid4().hex[:6]}",
        file_name="clip.mp4",
        file_path="videos/clip.mp4",
        file_type="video",
        status="pending",
    )
    db_session.add(other_task)
    await db_session.flush()

    resp = await httpx_client.post(
        "/api/v1/storage/video-assets/retry",
        headers={"Authorization": f"Bearer {token}"},
        json={"asset_type": "poster", "dataset_item_id": str(item.id)},
    )
    assert resp.status_code == 202, resp.text


async def test_video_asset_retry_rejects_unmanaged_linked_projects_only(
    db_session, httpx_client, super_admin, project_admin
):
    """所有链接项目都不归该管理员管理时保持 403。"""

    owner, token = project_admin
    _, item = await _make_video_task(db_session, owner.id)
    await db_session.info["media_owner_test_binding"].seed_failed_metadata(
        item, owner.id
    )

    from tests.factory import create_user

    other_user = await create_user(
        db_session,
        "project_admin",
        f"vfs-denied-{uuid.uuid4().hex[:6]}@test.local",
        "D",
    )
    other_project = Project(
        display_id=f"P-VFS-{uuid.uuid4().hex[:6]}",
        name="Denied Project",
        type_key="video-track",
        type_label="视频 · 时序追踪",
        data_type="video",
        owner_id=other_user.id,
        tool_bindings=build_tool_bindings(["car"]),
    )
    db_session.add(other_project)
    await db_session.flush()
    db_session.add(
        Task(
            project_id=other_project.id,
            dataset_item_id=item.id,
            display_id=f"T-VFS-{uuid.uuid4().hex[:6]}",
            file_name="clip.mp4",
            file_path="videos/clip.mp4",
            file_type="video",
            status="pending",
        )
    )
    await db_session.flush()

    # Re-point the first task at a fresh item so the shared item is only linked
    # to the unmanaged project.
    await db_session.execute(
        update(Task)
        .where(
            Task.id
            == (
                await db_session.scalar(
                    select(Task.id).where(
                        Task.project_id != other_project.id,
                        Task.dataset_item_id == item.id,
                    )
                )
            )
        )
        .values(dataset_item_id=None)
    )
    await db_session.flush()

    resp = await httpx_client.post(
        "/api/v1/storage/video-assets/retry",
        headers={"Authorization": f"Bearer {token}"},
        json={"asset_type": "poster", "dataset_item_id": str(item.id)},
    )
    assert resp.status_code == 403


async def test_timetable_workflow_replaces_rows(db_session, super_admin, monkeypatch):
    from app.services.assets_media import media_gateway
    from app.application import video_timetable_rebuild

    user, _ = super_admin
    _, item = await _make_video_task(db_session, user.id)
    await db_session.refresh(item)
    video_metadata = dict(item.metadata_["video"])
    video_metadata["frame_count"] = 4
    video_metadata.update({"probe_error": "keep", "frame_timetable_error": "previous"})
    item.metadata_ = {**item.metadata_, "video": video_metadata}
    db_session.add(
        VideoFrameIndex(
            dataset_item_id=item.id,
            frame_index=0,
            pts_ms=999,
            is_keyframe=False,
        )
    )
    await db_session.flush()
    rows = [
        {
            "frame_index": 0,
            "pts_ms": 0,
            "is_keyframe": False,
            "pict_type": "P",
            "byte_offset": 10,
        },
        {
            "frame_index": 2,
            "pts_ms": 66,
            "is_keyframe": True,
            "pict_type": "I",
            "byte_offset": 30,
        },
        {
            "frame_index": 3,
            "pts_ms": 99,
            "is_keyframe": False,
            "pict_type": "P",
            "byte_offset": 40,
        },
    ]

    async def probe(*_args, **_kwargs):
        return rows

    monkeypatch.setattr(video_timetable_rebuild, "probe_video_timetable", probe)
    binding = db_session.info["media_owner_test_binding"]

    async def run_media_job(job_id):
        await binding.run_timetable(job_id, frame_rows=rows)
        return True

    monkeypatch.setattr(video_timetable_rebuild, "run_media_job", run_media_job)
    source = await binding._source(item, user.id)
    workflow = VideoTimetableRebuildWorkflow(
        gateway_factory=lambda: media_gateway(db_session)
    )
    dry_count = await workflow.rebuild_item(source, dry_run=True, sparse_stride=3)
    assert dry_count == 3
    assert (
        await db_session.scalar(
            select(VideoFrameIndex.pts_ms).where(
                VideoFrameIndex.dataset_item_id == item.id,
                VideoFrameIndex.frame_index == 0,
            )
        )
        == 999
    )
    assert item.metadata_["video"]["frame_timetable_error"] == "previous"

    count = await workflow.rebuild_item(source, sparse_stride=3)
    rows = (
        await db_session.execute(
            VideoFrameIndex.__table__.select()
            .where(VideoFrameIndex.dataset_item_id == item.id)
            .order_by(VideoFrameIndex.frame_index.asc())
        )
    ).all()
    await db_session.refresh(item)

    assert count == 3
    assert [row.frame_index for row in rows] == [0, 2, 3]
    assert [row.pts_ms for row in rows] == [0, 66, 99]
    assert item.metadata_["video"]["frame_timetable_frame_count"] == 4
    assert item.metadata_["video"]["probe_error"] == "keep"
    assert "frame_timetable_error" not in item.metadata_["video"]


async def test_timetable_workflow_records_error_without_losing_video_metadata(
    db_session, super_admin, monkeypatch
):
    from aap_assets_media import contract as c
    from app.services.assets_media import media_gateway
    from app.application import video_timetable_rebuild

    user, _ = super_admin
    _, item = await _make_video_task(db_session, user.id)
    await db_session.refresh(item)
    video_metadata = dict(item.metadata_["video"])
    video_metadata["probe_error"] = "metadata failure"
    item.metadata_ = {**item.metadata_, "video": video_metadata}
    await db_session.flush()
    binding = db_session.info["media_owner_test_binding"]
    source = await binding._source(item, user.id)
    workflow = VideoTimetableRebuildWorkflow(
        gateway_factory=lambda: media_gateway(db_session)
    )

    async def fail_media_job(job_id):
        await binding.run_timetable(job_id, error="ffprobe frame timetable failed")
        return True

    monkeypatch.setattr(video_timetable_rebuild, "run_media_job", fail_media_job)
    with pytest.raises(c.MediaFailure):
        await workflow.rebuild_item(source)
    await db_session.refresh(item)

    assert item.metadata_["video"]["probe_error"] == "metadata failure"
    assert item.metadata_["video"]["playback_path"].endswith("/playback.mp4")


async def test_rebuild_timetable_cli_records_failure_after_rollback(
    db_session, super_admin, monkeypatch
):
    from app.services.assets_media import media_gateway
    from app.application import video_timetable_rebuild

    user, _ = super_admin
    _, failed = await _make_video_task(db_session, user.id)
    _, following = await _make_video_task(db_session, user.id)
    failed.created_at = following.created_at - timedelta(seconds=1)
    binding = db_session.info["media_owner_test_binding"]
    for item in (failed, following):
        updated_video = dict(item.metadata_["video"])
        updated_video["frame_count"] = 4
        item.metadata_ = {**item.metadata_, "video": updated_video}
    binding.storage.sources.pop(failed.file_path)
    original_video = dict(failed.metadata_["video"])
    failed_id, following_id = str(failed.id), str(following.id)
    await db_session.flush()
    db_session.add(
        VideoFrameIndex(
            dataset_item_id=failed.id,
            frame_index=0,
            pts_ms=999,
            is_keyframe=True,
        )
    )
    await db_session.commit()

    rows = [
        {"frame_index": 0, "pts_ms": 0, "is_keyframe": False},
        {"frame_index": 2, "pts_ms": 66, "is_keyframe": True},
        {"frame_index": 3, "pts_ms": 99, "is_keyframe": False},
    ]

    async def probe(*_args, **_kwargs):
        return rows

    async def run_media_job(job_id):
        from app.db.models.asset_media import AssetMediaJob

        job = await db_session.get(AssetMediaJob, job_id)
        item = await db_session.get(DatasetItem, job.dataset_item_id)
        if item.id == failed.id:
            await binding.run_timetable(job_id, error="source unavailable")
        else:
            await binding.run_timetable(job_id, frame_rows=rows)
        return True

    monkeypatch.setattr(video_timetable_rebuild, "probe_video_timetable", probe)
    monkeypatch.setattr(video_timetable_rebuild, "run_media_job", run_media_job)
    monkeypatch.setattr(
        timetable_cli,
        "VideoTimetableRebuildWorkflow",
        lambda: VideoTimetableRebuildWorkflow(
            gateway_factory=lambda: media_gateway(db_session)
        ),
    )

    assert (
        await timetable_cli.async_main(
            [
                "--dataset-item-id",
                failed_id,
                "--dataset-item-id",
                following_id,
                "--keep-going",
                "--sparse-stride",
                "3",
            ]
        )
        == 1
    )
    await db_session.refresh(failed)
    await db_session.refresh(following)
    failed_pts = list(
        await db_session.scalars(
            select(VideoFrameIndex.pts_ms).where(
                VideoFrameIndex.dataset_item_id == failed.id
            )
        )
    )
    following_frames = list(
        await db_session.scalars(
            select(VideoFrameIndex.frame_index)
            .where(VideoFrameIndex.dataset_item_id == following.id)
            .order_by(VideoFrameIndex.frame_index)
        )
    )
    assert failed_pts == [999]
    assert following_frames == [0, 2, 3]
    assert following.metadata_["video"]["frame_timetable_frame_count"] == 4
    assert {
        key: value
        for key, value in failed.metadata_["video"].items()
        if key != "frame_timetable_error"
    } == original_video


async def test_video_tracker_job_create_get_cancel(
    db_session, httpx_client, project_admin, annotator, monkeypatch
):
    owner, _ = project_admin
    user, token = annotator
    task, item = await _make_video_task(db_session, owner.id)
    monkeypatch.setattr(
        "app.services.video_segment_service.settings.video_segment_size_frames",
        45,
    )
    batch = TaskBatch(
        project_id=task.project_id,
        dataset_id=item.dataset_id,
        display_id=f"B-VTJ-{uuid.uuid4().hex[:6]}",
        name="Video tracker batch",
        status="active",
        annotator_id=user.id,
        assigned_user_ids=[str(user.id)],
    )
    db_session.add_all(
        [
            ProjectMember(
                project_id=task.project_id,
                user_id=user.id,
                role="annotator",
                assigned_by=owner.id,
            ),
            batch,
        ]
    )
    await db_session.flush()
    task.batch_id = batch.id
    annotation = Annotation(
        task_id=task.id,
        project_id=task.project_id,
        user_id=user.id,
        annotation_type="bbox",
        class_name="car",
        geometry={"type": "bbox", "x": 1, "y": 2, "width": 10, "height": 12},
    )
    db_session.add(annotation)
    await db_session.flush()
    queued_jobs: list[str] = []

    class FakeAsyncResult:
        id = "tracker-celery-task"

    def _fake_send_task(name, args=None, queue=None, **kwargs):
        assert name == "app.workers.video_tracker.run_video_tracker_job"
        assert queue == "gpu"
        queued_jobs.append(args[0])
        return FakeAsyncResult()

    monkeypatch.setattr(
        "celery.current_app.send_task",
        _fake_send_task,
    )

    segments_resp = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/video/segments",
        headers={"Authorization": f"Bearer {token}"},
    )
    segment_id = segments_resp.json()["segments"][0]["id"]
    await httpx_client.post(
        f"/api/v1/tasks/{task.id}/video/segments/{segment_id}:claim",
        headers={"Authorization": f"Bearer {token}"},
    )

    create_resp = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/video/tracks/{annotation.id}:propagate",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "from_frame": 0,
            "to_frame": 12,
            "model_key": "mock_bbox",
            "direction": "forward",
            "segment_id": segment_id,
            "prompt": {"type": "bbox", "geometry": annotation.geometry},
        },
    )

    assert create_resp.status_code == 202
    body = create_resp.json()
    assert body["status"] == "queued"
    assert body["task_id"] == str(task.id)
    assert body["annotation_id"] == str(annotation.id)
    assert body["event_channel"] == f"video-tracker-job:{body['id']}"
    assert body["celery_task_id"] == "tracker-celery-task"
    assert queued_jobs == [body["id"]]

    get_resp = await httpx_client.get(
        f"/api/v1/video-tracker-jobs/{body['id']}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == body["id"]

    cancel_resp = await httpx_client.delete(
        f"/api/v1/video-tracker-jobs/{body['id']}",
        headers={"Authorization": f"Bearer {token}"},
    )
    repeat_cancel_resp = await httpx_client.delete(
        f"/api/v1/video-tracker-jobs/{body['id']}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert cancel_resp.status_code == 200
    assert cancel_resp.json()["status"] == "cancelled"
    assert cancel_resp.json()["cancel_requested_at"] is not None
    assert repeat_cancel_resp.status_code == 200
    assert repeat_cancel_resp.json()["status"] == "cancelled"
    row = await db_session.get(VideoTrackerJob, uuid.UUID(body["id"]))
    assert row is not None
    assert row.status == "cancelled"


async def test_video_tracker_job_requires_current_segment_lock(
    db_session, httpx_client, project_admin, annotator, monkeypatch
):
    owner, _ = project_admin
    user, token = annotator
    task, item = await _make_video_task(db_session, owner.id)
    monkeypatch.setattr(
        "app.services.video_segment_service.settings.video_segment_size_frames",
        45,
    )
    batch = TaskBatch(
        project_id=task.project_id,
        dataset_id=item.dataset_id,
        display_id=f"B-VTJ-{uuid.uuid4().hex[:6]}",
        name="Video tracker batch",
        status="active",
        annotator_id=user.id,
        assigned_user_ids=[str(user.id)],
    )
    db_session.add_all(
        [
            ProjectMember(
                project_id=task.project_id,
                user_id=user.id,
                role="annotator",
                assigned_by=owner.id,
            ),
            batch,
        ]
    )
    await db_session.flush()
    task.batch_id = batch.id
    annotation = Annotation(
        task_id=task.id,
        project_id=task.project_id,
        user_id=user.id,
        annotation_type="bbox",
        class_name="car",
        geometry={"type": "bbox", "x": 1, "y": 2, "width": 10, "height": 12},
    )
    db_session.add(annotation)
    await db_session.flush()

    segments_resp = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/video/segments",
        headers={"Authorization": f"Bearer {token}"},
    )
    segment_id = segments_resp.json()["segments"][0]["id"]

    resp = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/video/tracks/{annotation.id}:propagate",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "from_frame": 0,
            "to_frame": 12,
            "model_key": "mock_bbox",
            "direction": "forward",
            "segment_id": segment_id,
            "prompt": {"type": "bbox", "geometry": annotation.geometry},
        },
    )

    assert resp.status_code == 409


async def test_video_tracker_job_rejects_polyline_track(
    db_session, httpx_client, project_admin, annotator
):
    # polyline 轨迹传播暂不支持: runner 会把它静默改写成空 bbox 轨迹, 故入口必须 400 拒绝
    # (且早于段锁校验, 无需 claim segment 即返回 400)。
    owner, _ = project_admin
    user, token = annotator
    task, item = await _make_video_task(db_session, owner.id)
    batch = TaskBatch(
        project_id=task.project_id,
        dataset_id=item.dataset_id,
        display_id=f"B-VTP-{uuid.uuid4().hex[:6]}",
        name="Video tracker batch",
        status="active",
        annotator_id=user.id,
        assigned_user_ids=[str(user.id)],
    )
    db_session.add_all(
        [
            ProjectMember(
                project_id=task.project_id,
                user_id=user.id,
                role="annotator",
                assigned_by=owner.id,
            ),
            batch,
        ]
    )
    await db_session.flush()
    task.batch_id = batch.id
    annotation = Annotation(
        task_id=task.id,
        project_id=task.project_id,
        user_id=user.id,
        annotation_type="video_track_polyline",
        class_name="car",
        geometry={
            "type": "video_track_polyline",
            "track_id": "poly-1",
            "keyframes": [
                {
                    "frame_index": 0,
                    "points": [[0.1, 0.1], [0.3, 0.2], [0.5, 0.1]],
                    "source": "manual",
                }
            ],
            "outside": [],
        },
    )
    db_session.add(annotation)
    await db_session.flush()

    resp = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/video/tracks/{annotation.id}:propagate",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "from_frame": 0,
            "to_frame": 12,
            "model_key": "mock_bbox",
            "direction": "forward",
        },
    )

    assert resp.status_code == 400, resp.text
    assert "polyline" in resp.json()["detail"]


async def _make_committed_video_item(maker, *, frame_count: int = 90):
    """并发回归测试脚手架: 用独立连接建 user→dataset→item 并真实 commit, 返回
    (item_id, dataset_id, user_id)。共享的 db_session 是单连接 SAVEPOINT 隔离, 表达不了
    真并发, 故并发用例必须自建数据并 commit, 末尾用 _cleanup_video_item 清理。"""
    from app.core.security import hash_password
    from app.db.models.user import User

    async with maker() as s:
        user = User(
            id=uuid.uuid4(),
            email=f"race-{uuid.uuid4().hex[:6]}@test.local",
            name="Race",
            password_hash=hash_password("Test1234"),
            role="super_admin",
            is_active=True,
        )
        s.add(user)
        await s.flush()
        dataset = Dataset(
            display_id=f"D-RACE-{uuid.uuid4().hex[:6]}",
            name="race",
            data_type="video",
            created_by=user.id,
        )
        s.add(dataset)
        await s.flush()
        item = DatasetItem(
            dataset_id=dataset.id,
            file_name="clip.mp4",
            file_path="videos/clip.mp4",
            file_type="video",
            metadata_={
                "video": {"fps": 30, "frame_count": frame_count, "duration_ms": 3000}
            },
        )
        s.add(item)
        await s.commit()
        return item.id, dataset.id, user.id


async def _cleanup_video_item(maker, item_id, dataset_id, user_id):
    from sqlalchemy import delete

    from app.db.models.asset_media import AssetMediaJob
    from app.db.models.user import User

    async with maker() as s:
        await s.execute(
            delete(AssetMediaJob).where(AssetMediaJob.dataset_item_id == item_id)
        )
        await s.execute(
            delete(VideoSegment).where(VideoSegment.dataset_item_id == item_id)
        )
        await s.execute(delete(VideoChunk).where(VideoChunk.dataset_item_id == item_id))
        await s.execute(
            delete(VideoFrameCache).where(VideoFrameCache.dataset_item_id == item_id)
        )
        await s.execute(delete(DatasetItem).where(DatasetItem.id == item_id))
        await s.execute(delete(Dataset).where(Dataset.id == dataset_id))
        await s.execute(delete(User).where(User.id == user_id))
        await s.commit()


async def test_frame_scheduling_concurrent_requests_share_one_owner_job(test_engine):
    """Concurrent frame requests converge on one durable Assets owner slot."""
    import asyncio

    from sqlalchemy import select as sa_select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from aap_assets_media import contract as c
    from app.db.models.asset_media import AssetMediaJob
    from app.services.assets_media import SqlAlchemyMediaDatabase

    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    item_id, dataset_id, user_id = await _make_committed_video_item(maker)
    try:
        barrier = asyncio.Barrier(4)

        async def worker():
            async with maker() as s:
                await barrier.wait()
                native = SqlAlchemyMediaDatabase(s, commit=False)
                for _attempt in range(30):
                    try:
                        accepted = await native.call(
                            "schedule_video_media",
                            c.ScheduleWorkRequest(
                                operation_id=None,
                                dataset_item_id=item_id,
                                spec=c.FrameSpec(
                                    frame_index=5, width=320, format="webp"
                                ),
                            ),
                        )
                        await s.commit()
                        return accepted.work.job_id
                    except c.MediaFailure as exc:
                        await s.rollback()
                        if exc.code not in {"source_busy", "operation_busy"}:
                            raise
                        await asyncio.sleep(0.01)
                raise AssertionError("frame owner scheduling did not settle")

        results = await asyncio.gather(*[worker() for _ in range(4)])

        assert len(set(results)) == 1
        async with maker() as s:
            jobs = list(
                (
                    await s.scalars(
                        sa_select(AssetMediaJob).where(
                            AssetMediaJob.dataset_item_id == item_id,
                            AssetMediaJob.kind == "frame",
                        )
                    )
                ).all()
            )
            assert len(jobs) == 1
            assert jobs[0].job_id == results[0]
    finally:
        await _cleanup_video_item(maker, item_id, dataset_id, user_id)


async def test_chunk_scheduling_concurrent_requests_share_one_owner_job(test_engine):
    """Concurrent chunk reads converge on the owner slot without ORM helpers."""
    import asyncio

    from sqlalchemy import select as sa_select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from aap_assets_media import contract as c
    from app.db.models.asset_media import AssetMediaJob
    from app.services.assets_media import SqlAlchemyMediaDatabase

    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    item_id, dataset_id, user_id = await _make_committed_video_item(maker)
    try:
        barrier = asyncio.Barrier(4)

        async def worker():
            async with maker() as s:
                await barrier.wait()
                native = SqlAlchemyMediaDatabase(s, commit=False)
                for _attempt in range(30):
                    try:
                        accepted = await native.call(
                            "schedule_video_media",
                            c.ScheduleWorkRequest(
                                operation_id=None,
                                dataset_item_id=item_id,
                                spec=c.ChunkSpec(chunk_id=0, chunk_size_frames=60),
                            ),
                        )
                        await s.commit()
                        return accepted.work.job_id
                    except c.MediaFailure as exc:
                        await s.rollback()
                        if exc.code not in {"source_busy", "operation_busy"}:
                            raise
                        await asyncio.sleep(0.01)
                raise AssertionError("chunk owner scheduling did not settle")

        results = await asyncio.gather(*[worker() for _ in range(4)])

        assert len(set(results)) == 1
        async with maker() as s:
            jobs = list(
                (
                    await s.scalars(
                        sa_select(AssetMediaJob).where(
                            AssetMediaJob.dataset_item_id == item_id,
                            AssetMediaJob.kind == "chunk",
                        )
                    )
                ).all()
            )
            assert len(jobs) == 1
            assert jobs[0].job_id == results[0]
    finally:
        await _cleanup_video_item(maker, item_id, dataset_id, user_id)


async def test_ensure_segments_concurrent_insert_does_not_raise(
    test_engine, monkeypatch
):
    """回归: 多个请求同时打开同一视频的 segments 会并发走 ensure_segments 全量创建。旧实现
    select-then-insert 撞 uq_video_segments_item_segment → 未捕获 IntegrityError → 500。修复
    用一个 SAVEPOINT 包整批 INSERT + 冲突 re-select: N 个并发请求都不抛错、都拿到同一份全量
    segments, DB 最终只有 segment_count 行。
    """
    import asyncio

    from sqlalchemy import select as sa_select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.services.video_segment_service import ensure_segments

    monkeypatch.setattr(
        "app.services.video_segment_service.settings.video_segment_size_frames", 45
    )

    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    # frame_count=90 + segment_size=45 → 2 个 segment
    item_id, dataset_id, user_id = await _make_committed_video_item(maker)
    try:
        barrier = asyncio.Barrier(4)

        async def worker():
            async with maker() as s:
                ctx = await _context_for_item(s, item_id)
                await barrier.wait()
                segs = await ensure_segments(s, ctx)
                await s.commit()
                return len(segs)

        results = await asyncio.gather(*[worker() for _ in range(4)])

        assert results == [2, 2, 2, 2]
        async with maker() as s:
            rows = (
                (
                    await s.execute(
                        sa_select(VideoSegment).where(
                            VideoSegment.dataset_item_id == item_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(rows) == 2
    finally:
        await _cleanup_video_item(maker, item_id, dataset_id, user_id)
