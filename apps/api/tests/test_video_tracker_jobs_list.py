"""v0.10.36 · GET /video-tracker-jobs 列表 + 聚合计数端点测试."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.video_tracker_job import VideoTrackerJob, VideoTrackerJobStatus
from tests.factory import create_membership, build_tool_bindings


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _make_video_task(
    db: AsyncSession, owner_id: uuid.UUID
) -> tuple[Task, DatasetItem]:
    suffix = uuid.uuid4().hex[:6]
    project = Project(
        display_id=f"P-VTL-{suffix}",
        name=f"VTL-{suffix}",
        type_key="video-track",
        type_label="视频 · 时序追踪",
        owner_id=owner_id,
        tool_bindings=build_tool_bindings(["car"]),
    )
    dataset = Dataset(
        display_id=f"D-VTL-{suffix}",
        name="videos",
        data_type="video",
        created_by=owner_id,
    )
    db.add_all([project, dataset])
    await db.flush()
    # Keep an explicit project role for the owner: an employee owner acts
    # through membership, and managers are unaffected by the extra row.
    await create_membership(
        db,
        project_id=project.id,
        user_id=owner_id,
        role="annotator",
        assigned_by=owner_id,
    )
    await db.flush()
    item = DatasetItem(
        dataset_id=dataset.id,
        file_name="clip.mp4",
        file_path="videos/clip.mp4",
        file_type="video",
        metadata_={"video": {"duration_ms": 3000, "fps": 30, "frame_count": 90}},
    )
    db.add(item)
    await db.flush()
    task = Task(
        project_id=project.id,
        dataset_item_id=item.id,
        display_id=f"T-VTL-{suffix}",
        file_name="clip.mp4",
        file_path="videos/clip.mp4",
        file_type="video",
        status="pending",
        # Unbatched task visibility requires the effective annotator to match.
        assignee_id=owner_id,
    )
    db.add(task)
    await db.flush()
    return task, item


async def _make_job(
    db: AsyncSession,
    task: Task,
    item: DatasetItem,
    owner_id: uuid.UUID,
    *,
    status: str,
    model_key: str = "sam2_video",
) -> VideoTrackerJob:
    annotation = Annotation(
        task_id=task.id,
        project_id=task.project_id,
        user_id=owner_id,
        annotation_type="bbox",
        class_name="car",
        geometry={"type": "bbox", "x": 1, "y": 2, "w": 10, "h": 12},
    )
    db.add(annotation)
    await db.flush()
    job = VideoTrackerJob(
        task_id=task.id,
        dataset_item_id=item.id,
        annotation_id=annotation.id,
        created_by=owner_id,
        status=status,
        model_key=model_key,
        direction="forward",
        from_frame=0,
        to_frame=2,
        prompt={
            "expected_source_versions": {str(annotation.id): int(annotation.version)}
        },
        event_channel="video-tracker-job:test",
    )
    db.add(job)
    await db.flush()
    return job


async def test_list_empty(httpx_client, super_admin):
    _, token = super_admin
    res = await httpx_client.get("/api/v1/video-tracker-jobs", headers=_bearer(token))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["items"] == []
    assert body["next_cursor"] is None
    assert body["counts"] == {
        "queued": 0,
        "running": 0,
        "completed": 0,
        "failed": 0,
        "cancelled": 0,
        "pending_review": 0,
        "partially_reviewed": 0,
        "accepted": 0,
        "discarded": 0,
    }


async def test_list_requires_admin(httpx_client, annotator):
    _, token = annotator
    res = await httpx_client.get("/api/v1/video-tracker-jobs", headers=_bearer(token))
    assert res.status_code == 403


async def test_counts_and_status_filter(httpx_client, super_admin, db_session):
    user, token = super_admin
    task, item = await _make_video_task(db_session, user.id)
    await _make_job(
        db_session, task, item, user.id, status=VideoTrackerJobStatus.QUEUED.value
    )
    await _make_job(
        db_session, task, item, user.id, status=VideoTrackerJobStatus.RUNNING.value
    )
    await _make_job(
        db_session, task, item, user.id, status=VideoTrackerJobStatus.RUNNING.value
    )
    await _make_job(
        db_session, task, item, user.id, status=VideoTrackerJobStatus.COMPLETED.value
    )
    await _make_job(
        db_session, task, item, user.id, status=VideoTrackerJobStatus.FAILED.value
    )
    await db_session.commit()

    res = await httpx_client.get("/api/v1/video-tracker-jobs", headers=_bearer(token))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["counts"]["queued"] == 1
    assert body["counts"]["running"] == 2
    assert body["counts"]["completed"] == 1
    assert body["counts"]["failed"] == 1
    assert body["counts"]["cancelled"] == 0
    assert len(body["items"]) == 5
    # 返回项带 project_id / task_id
    assert all(i["project_id"] == str(task.project_id) for i in body["items"])
    assert all(i["task_id"] == str(task.id) for i in body["items"])

    # status filter — counts 仍是全量, items 受过滤
    res = await httpx_client.get(
        "/api/v1/video-tracker-jobs?status=running", headers=_bearer(token)
    )
    body = res.json()
    assert len(body["items"]) == 2
    assert all(i["status"] == "running" for i in body["items"])
    assert body["counts"]["completed"] == 1  # counts 忽略 status 过滤


async def test_project_and_model_key_filter(httpx_client, super_admin, db_session):
    user, token = super_admin
    task_a, item_a = await _make_video_task(db_session, user.id)
    task_b, item_b = await _make_video_task(db_session, user.id)
    await _make_job(
        db_session, task_a, item_a, user.id, status="completed", model_key="sam2_video"
    )
    await _make_job(
        db_session, task_b, item_b, user.id, status="completed", model_key="sam3_video"
    )
    await db_session.commit()

    res = await httpx_client.get(
        f"/api/v1/video-tracker-jobs?project_id={task_a.project_id}",
        headers=_bearer(token),
    )
    body = res.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["project_id"] == str(task_a.project_id)
    assert body["counts"]["completed"] == 1

    res = await httpx_client.get(
        "/api/v1/video-tracker-jobs?model_key=sam3_video",
        headers=_bearer(token),
    )
    body = res.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["model_key"] == "sam3_video"


async def test_project_admin_list_is_scoped_to_owned_projects(
    httpx_client, project_admin, super_admin, db_session
):
    project_owner, token = project_admin
    other_owner, _ = super_admin
    own_task, own_item = await _make_video_task(db_session, project_owner.id)
    other_task, other_item = await _make_video_task(db_session, other_owner.id)
    await _make_job(
        db_session,
        own_task,
        own_item,
        project_owner.id,
        status=VideoTrackerJobStatus.PENDING_REVIEW.value,
    )
    await _make_job(
        db_session,
        other_task,
        other_item,
        other_owner.id,
        status=VideoTrackerJobStatus.PENDING_REVIEW.value,
    )
    await db_session.commit()

    res = await httpx_client.get("/api/v1/video-tracker-jobs", headers=_bearer(token))
    assert res.status_code == 200, res.text
    body = res.json()
    assert [item["project_id"] for item in body["items"]] == [str(own_task.project_id)]
    assert body["counts"]["pending_review"] == 1

    cross_project = await httpx_client.get(
        f"/api/v1/video-tracker-jobs?project_id={other_task.project_id}",
        headers=_bearer(token),
    )
    assert cross_project.status_code == 200, cross_project.text
    assert cross_project.json()["items"] == []
    assert cross_project.json()["counts"]["pending_review"] == 0


async def test_cursor_pagination(httpx_client, super_admin, db_session):
    user, token = super_admin
    task, item = await _make_video_task(db_session, user.id)
    for _ in range(5):
        await _make_job(db_session, task, item, user.id, status="completed")
    await db_session.commit()

    res = await httpx_client.get(
        "/api/v1/video-tracker-jobs?limit=2", headers=_bearer(token)
    )
    body = res.json()
    assert len(body["items"]) == 2
    cursor = body["next_cursor"]
    assert cursor is not None
    page1_ids = {i["id"] for i in body["items"]}

    res2 = await httpx_client.get(
        f"/api/v1/video-tracker-jobs?limit=2&cursor={cursor}", headers=_bearer(token)
    )
    body2 = res2.json()
    assert len(body2["items"]) == 2
    page2_ids = {i["id"] for i in body2["items"]}
    assert page1_ids.isdisjoint(page2_ids)

    res3 = await httpx_client.get(
        f"/api/v1/video-tracker-jobs?limit=2&cursor={body2['next_cursor']}",
        headers=_bearer(token),
    )
    body3 = res3.json()
    assert len(body3["items"]) == 1
    assert body3["next_cursor"] is None


async def test_invalid_status_rejected(httpx_client, super_admin):
    _, token = super_admin
    res = await httpx_client.get(
        "/api/v1/video-tracker-jobs?status=weird", headers=_bearer(token)
    )
    assert res.status_code == 422


# ── v0.21.28 · 候选/接受端点 ──────────────────────────────────────────


async def _make_staged_job(db, task, item, owner_id):
    """建一个 PENDING_REVIEW 的 job + 源 annotation, 带 2 帧暂存结果。"""
    annotation = Annotation(
        task_id=task.id,
        project_id=task.project_id,
        user_id=owner_id,
        annotation_type="bbox",
        class_name="car",
        tool_unit_id="bbox",
        geometry={"type": "bbox", "x": 1, "y": 2, "w": 10, "h": 12},
    )
    db.add(annotation)
    await db.flush()
    job = VideoTrackerJob(
        task_id=task.id,
        dataset_item_id=item.id,
        annotation_id=annotation.id,
        created_by=owner_id,
        status=VideoTrackerJobStatus.PENDING_REVIEW.value,
        model_key="sam2_video",
        direction="forward",
        from_frame=0,
        to_frame=2,
        prompt={
            "expected_source_versions": {str(annotation.id): int(annotation.version)}
        },
        event_channel="video-tracker-job:test",
        staged_result={
            "results": [
                {
                    "frame_index": f,
                    "geometry": {
                        "type": "bbox",
                        "x": float(f),
                        "y": 0.0,
                        "w": 5.0,
                        "h": 5.0,
                    },
                    "confidence": 1.0,
                    "outside": False,
                    "instance_id": None,
                    "primary": False,
                }
                for f in (1, 2)
            ],
            "grid_step": 1,
            "output_geometry": "bbox",
        },
    )
    db.add(job)
    await db.flush()
    return job, annotation


async def test_preview_and_accept_applies_staged(httpx_client, super_admin, db_session):
    user, token = super_admin
    task, item = await _make_video_task(db_session, user.id)
    job, annotation = await _make_staged_job(db_session, task, item, user.id)
    await db_session.commit()

    # preview → 返回暂存逐帧结果。
    res = await httpx_client.get(
        f"/api/v1/video-tracker-jobs/{job.id}/preview", headers=_bearer(token)
    )
    assert res.status_code == 200, res.text
    assert len(res.json()["results"]) == 2

    # accept → 落库 + status accepted。
    res = await httpx_client.post(
        f"/api/v1/video-tracker-jobs/{job.id}/accept", headers=_bearer(token)
    )
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "accepted"
    await db_session.refresh(annotation)
    assert annotation.annotation_type == "video_track_bbox"
    frames = [kf["frame_index"] for kf in annotation.geometry["keyframes"]]
    assert 1 in frames and 2 in frames


async def test_preview_typed_results_roundtrip_rich_shape(
    httpx_client, super_admin, db_session
):
    """typed VideoTrackerJobPreview.results 忠实回传多实例 staged 形状 (instance_id 非空 /
    primary / confidence=None), 且对缺失可选键容错 —— 不因收紧类型而 500。"""
    user, token = super_admin
    task, item = await _make_video_task(db_session, user.id)
    annotation = Annotation(
        task_id=task.id,
        project_id=task.project_id,
        user_id=user.id,
        annotation_type="bbox",
        class_name="car",
        tool_unit_id="bbox",
        geometry={"type": "bbox", "x": 1, "y": 2, "w": 10, "h": 12},
    )
    db_session.add(annotation)
    await db_session.flush()
    job = VideoTrackerJob(
        task_id=task.id,
        dataset_item_id=item.id,
        annotation_id=annotation.id,
        created_by=user.id,
        status=VideoTrackerJobStatus.PENDING_REVIEW.value,
        model_key="sam2_video",
        direction="forward",
        from_frame=0,
        to_frame=1,
        prompt={},
        event_channel="video-tracker-job:test",
        staged_result={
            "results": [
                {
                    "frame_index": 0,
                    "geometry": {
                        "type": "bbox",
                        "x": 0.0,
                        "y": 0.0,
                        "w": 5.0,
                        "h": 5.0,
                    },
                    "confidence": None,
                    "outside": False,
                    "instance_id": "2",
                    "primary": True,
                },
                # 缺失全部可选键的极简行 (只有 frame_index + geometry): 验证 typed 容错。
                {
                    "frame_index": 1,
                    "geometry": {
                        "type": "bbox",
                        "x": 1.0,
                        "y": 0.0,
                        "w": 5.0,
                        "h": 5.0,
                    },
                },
            ],
            "grid_step": 2,
            "output_geometry": "polygon",
        },
    )
    db_session.add(job)
    await db_session.commit()

    res = await httpx_client.get(
        f"/api/v1/video-tracker-jobs/{job.id}/preview", headers=_bearer(token)
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["grid_step"] == 2
    assert body["output_geometry"] == "polygon"
    rows = body["results"]
    assert len(rows) == 2

    first = rows[0]
    assert first["frame_index"] == 0
    assert first["geometry"]["type"] == "bbox"
    assert first["confidence"] is None
    assert first["outside"] is False
    assert first["instance_id"] == "2"
    assert first["primary"] is True

    # 缺失可选键 → 默认值填充, 而非 500。
    second = rows[1]
    assert second["frame_index"] == 1
    assert second["confidence"] is None
    assert second["outside"] is False
    assert second["instance_id"] is None
    assert second["primary"] is False


async def test_discard_leaves_annotation_untouched(
    httpx_client, super_admin, db_session
):
    user, token = super_admin
    task, item = await _make_video_task(db_session, user.id)
    job, annotation = await _make_staged_job(db_session, task, item, user.id)
    await db_session.commit()

    res = await httpx_client.post(
        f"/api/v1/video-tracker-jobs/{job.id}/discard", headers=_bearer(token)
    )
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "discarded"
    await db_session.refresh(annotation)
    assert annotation.annotation_type == "bbox"  # 源零改动

    # 重复丢弃幂等，不会把已清空的 staged_result 当非法候选。
    repeated = await httpx_client.post(
        f"/api/v1/video-tracker-jobs/{job.id}/discard", headers=_bearer(token)
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["status"] == "discarded"


async def test_discard_rejects_non_reviewable_status(
    httpx_client, super_admin, db_session
):
    user, token = super_admin
    task, item = await _make_video_task(db_session, user.id)
    job = await _make_job(
        db_session,
        task,
        item,
        user.id,
        status=VideoTrackerJobStatus.QUEUED.value,
    )
    await db_session.commit()

    res = await httpx_client.post(
        f"/api/v1/video-tracker-jobs/{job.id}/discard", headers=_bearer(token)
    )

    assert res.status_code == 409, res.text
    await db_session.refresh(job)
    assert job.status == VideoTrackerJobStatus.QUEUED.value


async def test_accept_rejects_non_reviewable_status(
    httpx_client, super_admin, db_session
):
    """accept 与 discard 对称: 非 reviewable (无暂存) 状态返回 409 而非静默 200, 避免
    双击接受时第二次请求悄悄成功、审计记两次、UI 无从区分。"""
    user, token = super_admin
    task, item = await _make_video_task(db_session, user.id)
    job = await _make_job(
        db_session,
        task,
        item,
        user.id,
        status=VideoTrackerJobStatus.QUEUED.value,
    )
    await db_session.commit()

    res = await httpx_client.post(
        f"/api/v1/video-tracker-jobs/{job.id}/accept", headers=_bearer(token)
    )

    assert res.status_code == 409, res.text
    await db_session.refresh(job)
    assert job.status == VideoTrackerJobStatus.QUEUED.value


async def test_cancel_rejects_pending_review_candidate(
    httpx_client, super_admin, db_session
):
    """候选待审 (pending_review) 不能 cancel: 返回 409 引导用户改用 discard, 而不是
    静默返回 200 让人以为取消没生效。"""
    user, token = super_admin
    task, item = await _make_video_task(db_session, user.id)
    staged_job, _ = await _make_staged_job(db_session, task, item, user.id)
    await db_session.commit()

    res = await httpx_client.delete(
        f"/api/v1/video-tracker-jobs/{staged_job.id}", headers=_bearer(token)
    )

    assert res.status_code == 409, res.text
    await db_session.refresh(staged_job)
    assert staged_job.status == VideoTrackerJobStatus.PENDING_REVIEW.value


async def test_task_reviewable_jobs_supports_workbench_restore(
    httpx_client, annotator, db_session
):
    user, token = annotator
    task, item = await _make_video_task(db_session, user.id)
    staged_job, _ = await _make_staged_job(db_session, task, item, user.id)
    await _make_job(
        db_session,
        task,
        item,
        user.id,
        status=VideoTrackerJobStatus.RUNNING.value,
    )
    await db_session.commit()

    res = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/video/tracker-jobs/reviewable",
        headers=_bearer(token),
    )

    assert res.status_code == 200, res.text
    body = res.json()
    assert [row["id"] for row in body] == [str(staged_job.id)]
    assert body[0]["status"] == "pending_review"


async def test_task_active_jobs_supports_ws_reconnect(
    httpx_client, annotator, db_session
):
    """刷新后重连: /active 只返回运行中 (queued/running) 任务, 待审候选归 /reviewable。"""
    user, token = annotator
    task, item = await _make_video_task(db_session, user.id)
    # 一个待审候选 (应被 /active 排除)。
    await _make_staged_job(db_session, task, item, user.id)
    running = await _make_job(
        db_session, task, item, user.id, status=VideoTrackerJobStatus.RUNNING.value
    )
    queued = await _make_job(
        db_session, task, item, user.id, status=VideoTrackerJobStatus.QUEUED.value
    )
    await db_session.commit()

    res = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/video/tracker-jobs/active",
        headers=_bearer(token),
    )

    assert res.status_code == 200, res.text
    body = res.json()
    assert {row["id"] for row in body} == {str(running.id), str(queued.id)}
    assert all(row["status"] in ("running", "queued") for row in body)
