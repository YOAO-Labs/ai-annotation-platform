import uuid
import pytest
from sqlalchemy import select, text, update

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
from app.cli.video.rebuild_timetable import rebuild_item_timetable
from app.services.system_settings_service import SystemSettingsService
from app.db.models.video_derivative import VideoDerivativeOutput
from app.services import video_derivatives as video_contract
from app.services import storage_access
from app.services.content_snapshot import ContentError
from tests.video_derivative_fixtures import (
    inventory,
    publish_result,
    install_video_transport,
    owned_metadata,
    owned_output,
    video_authority,
    saved_url,
)
from tests.factory import build_tool_bindings
from app.services.video_frame_service import (
    build_context_from_dataset_item,
    list_chunks,
)


@pytest.fixture(autouse=True)
def owned_video_transport(monkeypatch):
    return install_video_transport(monkeypatch)


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
        file_path="videos/clip.mp4",
        file_type="video",
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
    task = Task(
        project_id=project.id,
        dataset_item_id=item.id,
        display_id=f"T-VFS-{uuid.uuid4().hex[:6]}",
        file_name="clip.mp4",
        file_path="videos/clip.mp4",
        file_type="video",
        status="pending",
    )
    db_session.add(task)
    await db_session.flush()
    await owned_metadata(db_session, item)
    return task, item


@pytest.mark.parametrize("playback_path", [None, "playback/clip.mp4"])
async def test_video_manifest_v2_exposes_derived_service_urls_only(
    db_session,
    httpx_client,
    super_admin,
    monkeypatch,
    playback_path,
    owned_video_transport,
):
    user, token = super_admin
    task, item = await _make_video_task(db_session, user.id)
    generation = await owned_metadata(
        db_session, item, force=True, playback=bool(playback_path)
    )
    monkeypatch.setattr(
        "app.services.video_segment_service.settings.video_segment_size_frames", 30
    )
    task_resp = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/video/manifest-v2",
        headers={"Authorization": f"Bearer {token}"},
    )
    video_resp = await httpx_client.get(
        f"/api/v1/videos/{item.id}/manifest",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert task_resp.status_code == 200, task_resp.text
    assert video_resp.status_code == 200, video_resp.text
    body = task_resp.json()
    assert body["task_id"] == str(task.id) and body["dataset_item_id"] == str(item.id)
    assert body["metadata_generation_id"] == str(generation.id)
    assert body["video_url"] is None
    assert body["poster_url"] == saved_url(generation, "poster.webp")
    assert body["chunks_manifest_url"].endswith(f"/api/v1/tasks/{task.id}/video/chunks")
    assert body["frame_service_base"].endswith(f"/api/v1/tasks/{task.id}/video/frames")
    assert [
        (s["segment_index"], s["start_frame"], s["end_frame"]) for s in body["segments"]
    ] == [(0, 0, 29), (1, 30, 59), (2, 60, 89)]
    assert video_resp.json()["dataset_item_id"] == str(item.id)
    assert video_resp.json()["video_url"] is None
    assert [call for call in owned_video_transport.calls if call[1] is not None] == [
        (generation.id, "poster.webp"),
        (generation.id, "poster.webp"),
    ]


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
    from app.services.video_frame_service import build_context_from_task
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
    ctx = await build_context_from_task(db_session, task)
    segments = await ensure_segments(db_session, ctx)
    assert len(segments) == 2

    await submit_segment(db_session, ctx, segments[0].id, owner, privileged=True)
    assert task.status != "review"
    assert task.annotation_contributor_ids == [str(owner.id)]
    await submit_segment(db_session, ctx, segments[1].id, other, privileged=True)
    await db_session.refresh(task)
    assert task.status == "review"
    assert task.review_round_id is not None
    assert task.review_submitter_id == other.id
    assert task.review_contributor_ids == sorted([str(owner.id), str(other.id)])


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
    db_session, httpx_client, super_admin, owned_video_transport
):
    user, token = super_admin
    task, item = await _make_video_task(db_session, user.id)
    await SystemSettingsService.set_many(
        db_session, {"video_chunk_warmup_lookahead": 0}, actor_id=user.id
    )
    resp = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/video/chunks?from_frame=0&to_frame=65",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["fallback_video_url"] is None
    assert [chunk["chunk_id"] for chunk in body["chunks"]] == [0, 1]
    assert {chunk["status"] for chunk in body["chunks"]} == {"pending"}
    assert len(owned_video_transport.queued) == 1
    assert set(map(str, owned_video_transport.queued[0])) == {
        chunk["output_id"] for chunk in body["chunks"]
    }
    rows = (
        await db_session.scalars(
            select(VideoChunk).where(VideoChunk.dataset_item_id == item.id)
        )
    ).all()
    assert len(rows) == 2 and all(row.output_id for row in rows)
    assert owned_video_transport.calls == []


async def test_video_warmup_zero_skips_neighbor_enqueue(
    db_session, super_admin, owned_video_transport
):
    user, _ = super_admin
    _, item = await _make_video_task(db_session, user.id)
    await SystemSettingsService.set_many(
        db_session, {"video_chunk_warmup_lookahead": 0}, actor_id=user.id
    )
    authority = await video_authority(db_session, item)
    ctx = await build_context_from_dataset_item(
        db_session, item.id, authority=authority
    )
    response = await list_chunks(db_session, ctx, 0, 0)
    assert len(owned_video_transport.queued) == 1
    assert list(map(str, owned_video_transport.queued[0])) == [
        str(response.chunks[0].output_id)
    ]
    rows = (
        await db_session.scalars(
            select(VideoChunk).where(VideoChunk.dataset_item_id == item.id)
        )
    ).all()
    assert [row.chunk_id for row in rows] == [0]


@pytest.mark.parametrize("path", ["chunks?from_frame=0&to_frame=0", "chunks/0"])
async def test_warmup_setting_failure_does_not_expire_the_primary_response(
    db_session, httpx_client, super_admin, monkeypatch, path, owned_video_transport
):
    user, token = super_admin
    task, item = await _make_video_task(db_session, user.id)
    output = await owned_output(db_session, item, "chunk")

    async def unavailable(db, *args, **kwargs):
        await db.execute(text("SELECT 1 / 0"))

    monkeypatch.setattr(SystemSettingsService, "get", unavailable)
    response = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/video/{path}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    chunks = body["chunks"] if "chunks" in body else [body]
    assert [chunk["chunk_id"] for chunk in chunks] == [0]
    assert chunks[0]["status"] == "ready" and chunks[0]["output_id"] == str(output.id)
    assert owned_video_transport.queued == []


async def test_video_chunk_api_exposes_generation_diagnostics(
    db_session, httpx_client, super_admin
):
    user, token = super_admin
    task, item = await _make_video_task(db_session, user.id)
    diagnostics = {
        "source_codec": "h264",
        "output_codec": "h264",
        "keyframe_aligned": True,
        "start_byte_offset": 100,
        "end_byte_offset": 9000,
        "smart_copy_eligible": True,
        "fallback_reason": None,
    }
    output = await owned_output(
        db_session, item, "chunk", generation_mode="smart_copy", diagnostics=diagnostics
    )
    resp = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/video/chunks/0",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ready" and body["generation_mode"] == "smart_copy"
    assert body["output_id"] == str(output.id)
    assert body["metadata_generation_id"] == str(item.video_output_id)
    assert (
        body["sha256"]
        == output.inventory["objects"][output.storage_ref["prefix"] + "chunk.mp4"][
            "sha256"
        ]
    )
    assert body["diagnostics"] == diagnostics


async def test_video_chunk_samples_returns_description_and_stored_order(
    db_session, httpx_client, super_admin, monkeypatch
):
    monkeypatch.setattr(
        "app.services.video_derivatives.settings.video_chunk_size_frames", 3
    )
    user, token = super_admin
    _, item = await _make_video_task(db_session, user.id)
    # Decode order differs from presentation rank for B frames.
    samples = [
        {
            "frame_index": index,
            "pts_ms": pts,
            "duration_ms": 33,
            "is_keyframe": index == 9,
            "size_bytes": size,
            "offset_in_chunk": offset,
        }
        for index, pts, size, offset in [
            (9, 0, 5000, 48),
            (11, 67, 1200, 5048),
            (10, 33, 800, 6248),
        ]
    ]
    diagnostics = {
        "source_codec": "h264",
        "output_codec": "h264",
        "codec_string": "avc1.64001f",
        "description": "AAAA",
        "width": 1920,
        "height": 1080,
        "samples": samples,
    }
    output = await owned_output(
        db_session,
        item,
        "chunk",
        chunk_id=3,
        generation_mode="smart_copy",
        diagnostics=diagnostics,
        data=b"x" * 7048,
    )
    resp = await httpx_client.get(
        f"/api/v1/videos/{item.id}/chunks/3/samples",
        headers={"Authorization": f"Bearer {token}"},
        params={"expected_output_id": str(output.id)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dataset_item_id"] == str(item.id) and body["chunk_id"] == 3
    assert body["output_id"] == str(output.id) and body[
        "metadata_generation_id"
    ] == str(item.video_output_id)
    assert body["codec_string"] == "avc1.64001f" and body["description"] == "AAAA"
    assert body["width"] == 1920 and body["height"] == 1080
    assert body["samples"] == samples


async def test_video_chunk_samples_404_when_no_samples(
    db_session, httpx_client, super_admin
):
    user, token = super_admin
    _, item = await _make_video_task(db_session, user.id)
    await owned_output(
        db_session,
        item,
        "chunk",
        generation_mode="smart_copy",
        diagnostics={"source_codec": "h264"},
    )
    resp = await httpx_client.get(
        f"/api/v1/videos/{item.id}/chunks/0/samples",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404
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
    db_session, httpx_client, super_admin, owned_video_transport
):
    user, token = super_admin
    task, item = await _make_video_task(db_session, user.id)
    output = await owned_output(db_session, item, "frame", frame_index=12)
    resp = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/video/frames/12?format=webp&w=512",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["cache-control"] == "private, max-age=3600"
    body = resp.json()
    assert body["status"] == "ready" and body["url"] == saved_url(output, "frame.webp")
    assert body["output_id"] == str(output.id)
    assert owned_video_transport.queued == []


async def test_video_frame_pending_does_not_enqueue_duplicate_worker(
    db_session, httpx_client, super_admin, owned_video_transport
):
    user, token = super_admin
    task, item = await _make_video_task(db_session, user.id)
    output = await owned_output(
        db_session, item, "frame", frame_index=12, ready=False, claimed=True
    )
    resp = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/video/frames/12?format=webp&w=512",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 202, resp.text
    assert resp.headers["cache-control"] == "no-store"
    assert resp.json()["status"] == "pending" and resp.json()["output_id"] == str(
        output.id
    )
    assert owned_video_transport.queued == []


async def test_video_frame_prefetch_creates_missing_rows(
    db_session, httpx_client, super_admin, owned_video_transport
):
    user, token = super_admin
    _, item = await _make_video_task(db_session, user.id)
    resp = await httpx_client.post(
        f"/api/v1/videos/{item.id}/frames:prefetch",
        headers={"Authorization": f"Bearer {token}"},
        json={"frame_indices": [3, 3, 4], "width": 320, "format": "jpeg"},
    )
    assert resp.status_code == 200, resp.text
    frames = resp.json()["frames"]
    assert [frame["frame_index"] for frame in frames] == [3, 4]
    assert {frame["status"] for frame in frames} == {"pending"}
    assert all(
        frame["metadata_generation_id"] == str(item.video_output_id) for frame in frames
    )
    assert len(owned_video_transport.queued) == 1
    assert set(map(str, owned_video_transport.queued[0])) == {
        frame["output_id"] for frame in frames
    }
    outputs = (
        await db_session.scalars(
            select(VideoDerivativeOutput).where(
                VideoDerivativeOutput.id.in_(owned_video_transport.queued[0])
            )
        )
    ).all()
    assert {
        (output.spec["frame_index"], output.spec["width"], output.spec["format"])
        for output in outputs
    } == {(3, 320, "jpeg"), (4, 320, "jpeg")}


async def test_video_frame_retry_resets_failed_rows_only(
    db_session, httpx_client, super_admin, owned_video_transport
):
    user, token = super_admin
    task, item = await _make_video_task(db_session, user.id)
    ready = await owned_output(db_session, item, "frame", frame_index=8)
    db_session.add(
        VideoFrameCache(
            dataset_item_id=item.id,
            frame_index=7,
            width=512,
            format="webp",
            status="failed",
            error="ffmpeg failed",
        )
    )
    await db_session.commit()
    resp = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/video/frames:retry",
        headers={"Authorization": f"Bearer {token}"},
        json={"frame_indices": [7, 8], "width": 512, "format": "webp"},
    )
    assert resp.status_code == 200, resp.text
    frames = resp.json()["frames"]
    assert [frame["frame_index"] for frame in frames] == [7]
    assert frames[0]["status"] == "pending"
    assert len(owned_video_transport.queued) == 1 and list(
        map(str, owned_video_transport.queued[0])
    ) == [frames[0]["output_id"]]
    slot = await db_session.scalar(
        select(VideoFrameCache).where(
            VideoFrameCache.dataset_item_id == item.id, VideoFrameCache.frame_index == 8
        )
    )
    assert slot.output_id == ready.id and slot.status == "ready"


async def test_video_asset_failures_list_metadata_chunk_and_frame_errors(
    db_session, httpx_client, super_admin
):
    user, token = super_admin
    task, item = await _make_video_task(db_session, user.id)
    item.metadata_ = {
        **(item.metadata_ or {}),
        "video": {
            **((item.metadata_ or {}).get("video") or {}),
            "probe_error": "ffprobe failed",
        },
    }
    db_session.add_all(
        [
            VideoChunk(
                dataset_item_id=item.id,
                chunk_id=2,
                start_frame=60,
                end_frame=89,
                status="failed",
                error="chunk failed",
            ),
            VideoFrameCache(
                dataset_item_id=item.id,
                frame_index=12,
                width=320,
                format="webp",
                status="failed",
                error="frame failed",
            ),
        ]
    )
    await db_session.flush()

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


async def test_video_asset_retry_queues_saved_attempts_for_existing_slots(
    db_session, httpx_client, super_admin, owned_video_transport
):
    user, token = super_admin
    _, item = await _make_video_task(db_session, user.id)
    active = item.video_output_id
    chunk = VideoChunk(
        dataset_item_id=item.id,
        chunk_id=1,
        start_frame=30,
        end_frame=59,
        status="failed",
        error="chunk failed",
    )
    frame = VideoFrameCache(
        dataset_item_id=item.id,
        frame_index=8,
        width=320,
        format="jpeg",
        status="failed",
        error="frame failed",
    )
    db_session.add_all([chunk, frame])
    await db_session.flush()
    payloads = [
        {"asset_type": "poster", "dataset_item_id": str(item.id)},
        {"asset_type": "chunk", "dataset_item_id": str(item.id), "chunk_id": 1},
        {
            "asset_type": "frame",
            "dataset_item_id": str(item.id),
            "frame_index": 8,
            "width": 320,
            "format": "jpeg",
        },
    ]
    for payload in payloads:
        response = await httpx_client.post(
            "/api/v1/storage/video-assets/retry",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
        )
        assert response.status_code == 202, response.text
    assert len(owned_video_transport.queued) == 3
    identifiers = [batch[0] for batch in owned_video_transport.queued]
    assert all(len(batch) == 1 for batch in owned_video_transport.queued)
    outputs = (
        await db_session.scalars(
            select(VideoDerivativeOutput).where(
                VideoDerivativeOutput.id.in_(identifiers)
            )
        )
    ).all()
    assert {output.kind for output in outputs} == {"metadata", "chunk", "frame"}
    assert all(
        output.state == "building" and output.auth_ref["policy"] == "storage"
        for output in outputs
    )
    assert all(
        output.original_ref["owner_kind"] == "dataset_asset" for output in outputs
    )
    await db_session.refresh(item)
    await db_session.refresh(chunk)
    await db_session.refresh(frame)
    assert (
        item.video_output_id == active and item.video_building_output_id in identifiers
    )
    assert (
        chunk.status == frame.status == "pending" and chunk.error is frame.error is None
    )
    assert chunk.output_id in identifiers and frame.output_id in identifiers
    assert owned_video_transport.calls == []


async def test_video_asset_retry_accepts_any_managed_linked_project(
    db_session, httpx_client, super_admin, project_admin, owned_video_transport
):
    """一个 dataset item 可被多个项目的 task 关联：只要其中任一链接项目是该
    管理员合法管理的，重试就应放行（与失败列表 any-owned-project 作用域一致），
    而不是依赖 LIMIT 1 任选一条链接行。"""

    owner, token = project_admin
    owned_task, item = await _make_video_task(db_session, owner.id)

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
    assert len(owned_video_transport.queued) == 1
    output = await db_session.get(
        VideoDerivativeOutput, owned_video_transport.queued[0][0]
    )
    assert output.kind == "metadata" and output.auth_ref["policy"] == "storage"
    assert output.auth_ref["owner_id"] == str(item.id)


async def test_video_asset_retry_rejects_unmanaged_linked_projects_only(
    db_session, httpx_client, super_admin, project_admin
):
    """所有链接项目都不归该管理员管理时保持 403。"""

    owner, token = project_admin
    _, item = await _make_video_task(db_session, owner.id)

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


async def test_rebuild_timetable_cli_publishes_new_metadata_generation(
    db_session, super_admin, monkeypatch
):
    user, _ = super_admin
    _, item = await _make_video_task(db_session, user.id)
    active = item.video_output_id
    db_session.add(
        VideoFrameIndex(
            dataset_item_id=item.id, frame_index=0, pts_ms=999, is_keyframe=False
        )
    )
    await db_session.flush()
    frames = [
        {
            "frame_index": 0,
            "pts_ms": 0,
            "is_keyframe": True,
            "pict_type": "I",
            "byte_offset": 10,
        },
        {
            "frame_index": 1,
            "pts_ms": 33,
            "is_keyframe": False,
            "pict_type": "P",
            "byte_offset": 20,
        },
    ]
    invoked = []

    class NoLegacyStorage:
        @property
        def client(self):
            raise AssertionError(
                "CLI must not download through a caller's mutable storage bucket"
            )

    async def finish_outputs(identifiers):
        assert not db_session.in_transaction()
        assert len(identifiers) == 1
        output = await db_session.get(VideoDerivativeOutput, uuid.UUID(identifiers[0]))
        assert output.id != active and output.spec == {
            "schema": 1,
            "timetable_stride": 1,
        }
        authority = await video_authority(db_session, item, register=False)
        video = {
            **item.metadata_["video"],
            "frame_timetable_frame_count": 2,
            "frame_timetable_error": None,
            "poster_frame_path": output.storage_ref["prefix"] + "poster.webp",
            "playback_path": output.storage_ref["prefix"] + "playback.mp4",
        }
        result = {"schema": 1, "kind": "metadata", "video": video, "frames": frames}
        await publish_result(
            db_session,
            output,
            authority,
            result,
            inventory(
                output, result, {"poster.webp": b"poster", "playback.mp4": b"playback"}
            ),
        )
        invoked.extend(identifiers)

    monkeypatch.setattr("app.workers.media_derivatives.run_outputs", finish_outputs)
    count = await rebuild_item_timetable(db_session, item, storage=NoLegacyStorage())
    rows = (
        await db_session.scalars(
            select(VideoFrameIndex)
            .where(VideoFrameIndex.dataset_item_id == item.id)
            .order_by(VideoFrameIndex.frame_index)
        )
    ).all()
    await db_session.refresh(item)
    assert count == 2 and [row.pts_ms for row in rows] == [0, 33]
    assert item.metadata_["video"]["frame_timetable_frame_count"] == 2
    assert item.video_output_id != active and str(item.video_output_id) == invoked[0]
    assert item.video_building_output_id is None
    old = await db_session.get(VideoDerivativeOutput, active)
    assert old.state == "ready"
    assert (
        await rebuild_item_timetable(
            db_session, item, storage=NoLegacyStorage(), dry_run=True
        )
        == 2
    )
    assert len(invoked) == 1


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
        await s.flush()
        await owned_metadata(s, item)
        return item.id, dataset.id, user.id


async def _cleanup_video_item(maker, item_id, dataset_id, user_id):
    from sqlalchemy import delete
    from app.db.models.user import User
    from app.db.models.storage_maintenance import StorageOperation
    from app.services.video_derivative_objects import purpose

    async with maker() as s:
        identifiers = list(
            await s.scalars(
                select(VideoDerivativeOutput.id).where(
                    VideoDerivativeOutput.dataset_item_id == item_id
                )
            )
        )
        if identifiers:
            assert not await s.scalar(
                select(StorageOperation.id)
                .where(
                    StorageOperation.purpose.in_(
                        [purpose(identifier) for identifier in identifiers]
                    )
                )
                .limit(1)
            )
        await s.execute(delete(Dataset).where(Dataset.id == dataset_id))
        if identifiers:
            await s.execute(
                update(VideoDerivativeOutput)
                .where(
                    VideoDerivativeOutput.id.in_(identifiers),
                    VideoDerivativeOutput.state.in_(["building", "ready"]),
                )
                .values(state="retiring")
            )
            await s.execute(
                update(VideoDerivativeOutput)
                .where(
                    VideoDerivativeOutput.id.in_(identifiers),
                    VideoDerivativeOutput.state == "retiring",
                )
                .values(state="deleted")
            )
            await s.execute(
                delete(VideoDerivativeOutput).where(
                    VideoDerivativeOutput.id.in_(identifiers)
                )
            )
        await s.execute(delete(User).where(User.id == user_id))
        await s.commit()


async def _concurrent_video_prepare(test_engine, kind):
    """NOWAIT contention may retry; no duplicate slot or owner may be committed."""
    import asyncio
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.ext.asyncio import async_sessionmaker

    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    item_id, dataset_id, user_id = await _make_committed_video_item(maker)
    try:
        barrier = asyncio.Barrier(4)

        async def worker():
            async with maker() as db:
                await barrier.wait()
                for attempt in range(50):
                    try:
                        item = await db.get(
                            DatasetItem, item_id, populate_existing=True
                        )
                        authority = await video_authority(db, item, register=False)
                        generation = await db.get(
                            VideoDerivativeOutput,
                            item.video_output_id,
                            populate_existing=True,
                        )
                        spec = (
                            video_contract.frame_spec(generation, 5, 320, "webp")
                            if kind == "frame"
                            else video_contract.chunk_spec(generation, 0)
                        )
                        output, _ = await video_contract.prepare(
                            db, item_id, authority, kind, spec=spec
                        )
                        identifier = output.id
                        await db.commit()
                        return identifier
                    except DBAPIError as exc:
                        if not storage_access.unavailable(exc):
                            raise
                    except ContentError as exc:
                        if exc.code not in {
                            "storage_original_busy",
                            "storage_maintenance_busy",
                            "content_busy",
                        }:
                            raise
                    await db.rollback()
                    await asyncio.sleep(0.005)
                raise AssertionError("bounded NOWAIT contention retries exhausted")

        identifiers = await asyncio.gather(
            *(worker() for _ in range(4)), return_exceptions=True
        )
        # Settle every competing connection before teardown, even on a failure.
        for result in identifiers:
            if isinstance(result, BaseException):
                raise result
        assert len(set(identifiers)) == 1
        async with maker() as db:
            model = VideoFrameCache if kind == "frame" else VideoChunk
            slots = list(
                await db.scalars(select(model).where(model.dataset_item_id == item_id))
            )
            outputs = list(
                await db.scalars(
                    select(VideoDerivativeOutput).where(
                        VideoDerivativeOutput.dataset_item_id == item_id,
                        VideoDerivativeOutput.kind == kind,
                    )
                )
            )
            assert len(slots) == len(outputs) == 1
            assert slots[0].output_id == outputs[0].id == identifiers[0]
            assert outputs[0].state == "building" and outputs[0].lease_token is None
    finally:
        await _cleanup_video_item(maker, item_id, dataset_id, user_id)


async def test_prepare_frame_concurrent_requests_keep_one_attempt(test_engine):
    await _concurrent_video_prepare(test_engine, "frame")


async def test_prepare_chunk_concurrent_requests_keep_one_attempt(test_engine):
    await _concurrent_video_prepare(test_engine, "chunk")


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

    from app.services.video_frame_service import build_context_from_dataset_item
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
                ctx = await build_context_from_dataset_item(s, item_id)
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
