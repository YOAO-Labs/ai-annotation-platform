"""v0.21.26 · 阶段 0 · 多目标落库底座。

backend 一次追踪返回多实例 (模式 a「自动发现」) 时:
- 主实例 (与用户种子对应, primary 标记 / 无 instance_id 兜底) 回填源 annotation;
- 其余每个 instance_id 各落一条新 annotation (继承 label、source="ai_tracker"、新 track_id)。
无 instance_id 的单实例老 backend 走原路径, 零回归。
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.config import settings
from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, DatasetItem, VideoSegment
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.video_tracker_job import VideoTrackerJob, VideoTrackerJobStatus
from app.services.video_tracking.adapters import TrackerContext, TrackerFrameResult
from app.services.video_tracking.runner import (
    _partition_results_by_instance,
    accept_tracker_job,
    discard_tracker_job,
    run_tracker_job,
)


# ── 纯函数: 按实例分组 ────────────────────────────────────────────────


def _r(frame: int, instance_id=None, primary=False) -> TrackerFrameResult:
    return TrackerFrameResult(
        frame_index=frame,
        geometry={"type": "bbox", "x": float(frame), "y": 0.0, "w": 5.0, "h": 5.0},
        confidence=1.0,
        outside=False,
        instance_id=instance_id,
        primary=primary,
    )


def test_partition_legacy_no_instance_id_all_primary():
    results = [_r(0), _r(1), _r(2)]
    primary, extras = _partition_results_by_instance(results)
    assert [x.frame_index for x in primary] == [0, 1, 2]
    assert extras == {}


def test_partition_primary_flag_routes_flagged_instance_to_source():
    results = [
        _r(0, "a", primary=True),
        _r(0, "b"),
        _r(1, "a", primary=True),
        _r(1, "b"),
        _r(1, "c"),
    ]
    primary, extras = _partition_results_by_instance(results)
    assert {x.frame_index for x in primary} == {0, 1}
    assert all(x.instance_id == "a" for x in primary)
    assert set(extras) == {"b", "c"}
    assert [x.frame_index for x in extras["b"]] == [0, 1]
    assert [x.frame_index for x in extras["c"]] == [1]


def test_partition_primary_flag_covers_all_frames_of_that_instance():
    # backend 只在种子帧标了 primary, 但该 instance 的其余帧也应整体归主。
    results = [_r(0, "obj7", primary=True), _r(1, "obj7"), _r(2, "obj7"), _r(1, "obj9")]
    primary, extras = _partition_results_by_instance(results)
    assert [x.frame_index for x in primary] == [0, 1, 2]
    assert set(extras) == {"obj9"}


def test_partition_no_primary_flag_uses_smallest_instance_id():
    results = [_r(0, "2"), _r(0, "1"), _r(1, "2"), _r(1, "1")]
    primary, extras = _partition_results_by_instance(results)
    assert all(x.instance_id == "1" for x in primary)
    assert set(extras) == {"2"}


def test_partition_no_primary_flag_numeric_ids_use_numeric_min():
    # 数字 instance_id 按数值取 min, 而非字典序 (否则 "10" < "2" 会挑错主实例)。
    results = [_r(0, "2"), _r(0, "10"), _r(1, "2"), _r(1, "10")]
    primary, extras = _partition_results_by_instance(results)
    assert all(x.instance_id == "2" for x in primary)
    assert set(extras) == {"10"}


# ── runner 端到端: 单 seed → 多实例 → 多 annotation ──────────────────


async def _make_video_task(db_session, owner_id):
    project = Project(
        display_id=f"P-MI-{uuid.uuid4().hex[:6]}",
        name="Multi-instance Tracker Project",
        type_key="video-track",
        type_label="视频 · 时序追踪",
        owner_id=owner_id,
        tool_bindings={
            "bbox": {
                "enabled": True,
                "classes": [{"name": "car"}, {"name": "pedestrian"}],
            }
        },
    )
    dataset = Dataset(
        display_id=f"D-MI-{uuid.uuid4().hex[:6]}",
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
        metadata_={"video": {"duration_ms": 3000, "fps": 30, "frame_count": 90}},
    )
    db_session.add(item)
    await db_session.flush()
    task = Task(
        project_id=project.id,
        dataset_item_id=item.id,
        display_id=f"T-MI-{uuid.uuid4().hex[:6]}",
        file_name="clip.mp4",
        file_path="videos/clip.mp4",
        file_type="video",
        status="pending",
    )
    db_session.add(task)
    await db_session.flush()
    return task, item


class _MultiInstanceAdapter:
    """每窗每帧对每个 instance 各产一条结果。obj0 标 primary (跟随源种子),
    obj1/obj2 为新发现目标。instance_id 跨窗稳定 (模拟阶段 B 的 backend 契约)。"""

    model_key = "sam3_video"

    def __init__(self, extra_ids: list[str]) -> None:
        self.extra_ids = extra_ids
        self.contexts: list[TrackerContext] = []

    async def propagate(self, ctx: TrackerContext):
        self.contexts.append(ctx)
        frames = range(ctx.from_frame, ctx.to_frame + 1)
        for frame_index in frames:
            yield TrackerFrameResult(
                frame_index=frame_index,
                geometry={
                    "type": "bbox",
                    "x": float(frame_index),
                    "y": 0.0,
                    "w": 10.0,
                    "h": 10.0,
                },
                confidence=1.0,
                outside=False,
                instance_id="obj0",
                primary=True,
            )
            for oid in self.extra_ids:
                yield TrackerFrameResult(
                    frame_index=frame_index,
                    geometry={
                        "type": "bbox",
                        "x": float(frame_index) + 100.0,
                        "y": 0.0,
                        "w": 8.0,
                        "h": 8.0,
                    },
                    confidence=0.95,
                    outside=False,
                    instance_id=oid,
                    primary=False,
                )


async def test_runner_lands_extra_instances_as_new_tracks(
    db_session, super_admin, monkeypatch
):
    user, _ = super_admin
    task, item = await _make_video_task(db_session, user.id)
    source = Annotation(
        task_id=task.id,
        project_id=task.project_id,
        user_id=user.id,
        annotation_type="bbox",
        class_name="car",
        tool_unit_id="bbox",
        geometry={"type": "bbox", "x": 1, "y": 2, "w": 10, "h": 12},
    )
    db_session.add(source)
    await db_session.flush()
    job = VideoTrackerJob(
        task_id=task.id,
        dataset_item_id=item.id,
        annotation_id=source.id,
        created_by=user.id,
        status=VideoTrackerJobStatus.QUEUED.value,
        model_key="sam3_video",
        direction="forward",
        from_frame=0,
        to_frame=3,
        prompt={
            "text": "car",
            "expected_source_versions": {str(source.id): int(source.version)},
        },
        event_channel="video-tracker-job:test",
    )
    db_session.add(job)
    await db_session.commit()

    # 两窗 (0,1) (2,3), 每窗返回 obj0(primary)+obj1+obj2。
    monkeypatch.setattr(settings, "video_tracker_sam3_window_size_frames", 2)
    adapter = _MultiInstanceAdapter(extra_ids=["obj1", "obj2"])
    monkeypatch.setattr(
        "app.services.video_tracking.runner.get_tracker_adapter",
        lambda _model_key: adapter,
    )
    # 无声明 sam3_video 的已启用 backend → get_tracker_backend 返回 None; stub adapter
    # 忽略 ctx.ml_backend, 无需真实 registry。

    async def collect(_channel: str, _payload: dict) -> None:
        return None

    from sqlalchemy import select

    await run_tracker_job(db_session, job.id, publisher=collect)
    await db_session.refresh(job)
    await db_session.refresh(source)

    assert len(adapter.contexts) == 2
    assert adapter.contexts[0].seeds is None
    continuation = adapter.contexts[1].seeds or []
    assert {seed["obj_id"] for seed in continuation} == {1, 2, 3}
    assert all(seed["geometry"]["type"] == "bbox" for seed in continuation)

    # v0.21.28 · 候选流: 完成 = 暂存待审, committed annotations 未改。
    assert job.status == "pending_review"
    assert job.staged_result and job.staged_result["results"]
    assert source.annotation_type == "bbox"  # 源未回填 (仍原始单帧 bbox)
    pre_extra = (
        (
            await db_session.execute(
                select(Annotation).where(
                    Annotation.task_id == task.id,
                    Annotation.source == "ai_tracker",
                )
            )
        )
        .scalars()
        .all()
    )
    assert pre_extra == []  # 接受前无新轨迹

    # 接受 → 落库。
    await accept_tracker_job(db_session, job.id, actor_id=user.id, publisher=collect)
    await db_session.refresh(job)
    await db_session.refresh(source)
    assert job.status == "accepted"

    # 源 annotation: 主实例 obj0 回填, seed 帧 0 保留 manual, 1..3 prediction。
    assert source.annotation_type == "video_track_bbox"
    src_frames = [kf["frame_index"] for kf in source.geometry["keyframes"]]
    assert src_frames == [0, 1, 2, 3]
    assert source.geometry["keyframes"][0]["source"] == "manual"
    # 主实例几何 x==frame (非 +100 的新目标几何) —— 证明未串到 extras。
    assert source.geometry["keyframes"][3]["bbox"]["x"] == pytest.approx(3.0)

    # 新发现的 obj1/obj2 各落一条 ai_tracker annotation。
    rows = (
        (
            await db_session.execute(
                select(Annotation).where(
                    Annotation.task_id == task.id,
                    Annotation.source == "ai_tracker",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2
    track_ids = {r.track_id for r in rows}
    assert len(track_ids) == 2  # 各自独立 track_id
    for r in rows:
        assert r.class_name == "car"  # 继承 source label
        assert r.tool_unit_id == "bbox"
        assert r.annotation_type == "video_track_bbox"
        assert r.user_id == user.id
        frames = [kf["frame_index"] for kf in r.geometry["keyframes"]]
        assert frames == [0, 1, 2, 3]  # 跨两窗连续
        # 全为预测帧 (新目标无人工 seed)。
        assert all(kf["source"] == "prediction" for kf in r.geometry["keyframes"])
        assert r.geometry["keyframes"][3]["bbox"]["x"] == pytest.approx(103.0)


async def test_runner_sourceless_detection_lands_all_as_new_tracks(
    db_session, super_admin, monkeypatch
):
    """v0.22.1 · B · 无源检测 (annotation_id=None, 画布级文本/种子发起): 所有实例含主实例
    都新建轨迹, 类别取 job.target_class_name (不继承任何 source)。"""
    user, _ = super_admin
    task, item = await _make_video_task(db_session, user.id)
    job = VideoTrackerJob(
        task_id=task.id,
        dataset_item_id=item.id,
        annotation_id=None,
        target_class_name="pedestrian",
        target_tool_unit_id="bbox",
        created_by=user.id,
        status=VideoTrackerJobStatus.QUEUED.value,
        model_key="sam3_video",
        direction="forward",
        from_frame=0,
        to_frame=3,
        prompt={"text": "pedestrian"},
        event_channel="video-tracker-job:test-sourceless",
    )
    db_session.add(job)
    await db_session.commit()

    monkeypatch.setattr(settings, "video_tracker_sam3_window_size_frames", 2)
    adapter = _MultiInstanceAdapter(extra_ids=["obj1"])
    monkeypatch.setattr(
        "app.services.video_tracking.runner.get_tracker_adapter",
        lambda _model_key: adapter,
    )

    async def collect(_channel: str, _payload: dict) -> None:
        return None

    from sqlalchemy import select

    await run_tracker_job(db_session, job.id, publisher=collect)
    await db_session.refresh(job)
    assert job.status == "pending_review"

    await accept_tracker_job(db_session, job.id, actor_id=user.id, publisher=collect)
    await db_session.refresh(job)
    assert job.status == "accepted"
    assert job.staged_result is None

    rows = (
        (
            await db_session.execute(
                select(Annotation).where(
                    Annotation.task_id == task.id,
                    Annotation.source == "ai_tracker",
                )
            )
        )
        .scalars()
        .all()
    )
    # 无源 → 主实例 obj0 + 新发现 obj1 各落一条新轨迹 = 2 条 (无回填对象)。
    assert len(rows) == 2
    assert len({r.track_id for r in rows}) == 2
    for r in rows:
        assert r.class_name == "pedestrian"  # 取 job.target_class_name, 不继承 source
        assert r.tool_unit_id == "bbox"
        assert r.annotation_type == "video_track_bbox"
        assert r.user_id == user.id
        assert all(kf["source"] == "prediction" for kf in r.geometry["keyframes"])


async def test_full_accept_skips_sourceless_instance_with_only_outside_results(
    db_session, super_admin
):
    user, _ = super_admin
    task, item = await _make_video_task(db_session, user.id)
    job = VideoTrackerJob(
        task_id=task.id,
        dataset_item_id=item.id,
        annotation_id=None,
        target_class_name="bus",
        target_tool_unit_id="bbox",
        created_by=user.id,
        status=VideoTrackerJobStatus.PENDING_REVIEW.value,
        model_key="sam3_video",
        direction="forward",
        from_frame=0,
        to_frame=1,
        prompt={"expected_source_versions": {}},
        staged_result={
            "results": [
                {
                    "frame_index": 0,
                    "geometry": {
                        "type": "bbox",
                        "x": 0.1,
                        "y": 0.1,
                        "w": 0.2,
                        "h": 0.2,
                    },
                    "confidence": 0.9,
                    "outside": False,
                    "instance_id": "visible",
                    "primary": False,
                },
                {
                    "frame_index": 0,
                    "geometry": {
                        "type": "bbox",
                        "x": 0.0,
                        "y": 0.0,
                        "w": 0.0,
                        "h": 0.0,
                    },
                    "confidence": 0.0,
                    "outside": True,
                    "instance_id": "missing",
                    "primary": False,
                },
            ],
            "grid_step": 1,
            "output_geometry": "bbox",
        },
        event_channel="video-tracker-job:test-sourceless",
    )
    db_session.add(job)
    await db_session.commit()

    async def collect(_channel: str, _payload: dict) -> None:
        return None

    await accept_tracker_job(db_session, job.id, actor_id=user.id, publisher=collect)
    rows = (
        (
            await db_session.execute(
                select(Annotation).where(
                    Annotation.task_id == task.id,
                    Annotation.source == "ai_tracker",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].geometry["keyframes"]


async def test_create_tracker_job_sourceless_stores_target_category(
    db_session, super_admin
):
    """v0.22.1 · B · 无源发起 (source_annotation_id=None): job 建成且 annotation_id 为空,
    显式目标类别落到 target_class_name/target_tool_unit_id。"""
    from app.schemas.video_tracker_job import VideoTrackerPropagateRequest
    from app.services.video_frame_service import build_context_from_task
    from app.services.video_tracking.jobs import create_tracker_job

    user, _ = super_admin
    task, item = await _make_video_task(db_session, user.id)
    ctx = await build_context_from_task(db_session, task)
    payload = VideoTrackerPropagateRequest(
        from_frame=0,
        to_frame=3,
        model_key="mock_bbox",
        text="pedestrian",
        target_class_name="pedestrian",
        target_tool_unit_id="bbox",
    )
    body = await create_tracker_job(
        db_session, task=task, ctx=ctx, annotation_id=None, payload=payload, user=user
    )
    job = await db_session.get(VideoTrackerJob, body.id)
    assert job is not None
    assert job.annotation_id is None
    assert job.target_class_name == "pedestrian"
    assert job.target_tool_unit_id == "bbox"


async def test_create_tracker_job_sourceless_rejects_invalid_target(
    db_session, super_admin
):
    from fastapi import HTTPException

    from app.schemas.video_tracker_job import VideoTrackerPropagateRequest
    from app.services.video_frame_service import build_context_from_task
    from app.services.video_tracking.jobs import create_tracker_job

    user, _ = super_admin
    task, _item = await _make_video_task(db_session, user.id)
    ctx = await build_context_from_task(db_session, task)
    payload = VideoTrackerPropagateRequest(
        from_frame=0,
        to_frame=3,
        model_key="mock_bbox",
        target_class_name="not-configured",
        target_tool_unit_id="bbox",
    )

    with pytest.raises(HTTPException) as exc:
        await create_tracker_job(
            db_session,
            task=task,
            ctx=ctx,
            annotation_id=None,
            payload=payload,
            user=user,
        )
    assert exc.value.status_code == 422


async def test_mask_track_seed_geometry_is_hydrated_to_bbox(monkeypatch):
    from types import SimpleNamespace

    import app.services.video_tracking.jobs as service

    reference = {"object_key": "raster-masks/sha256/x", "sha256": "x"}
    annotation = SimpleNamespace(
        geometry={
            "type": "video_track_mask",
            "track_id": "mask-track",
            "keyframes": [{"frame_index": 0, "mask": reference}],
        }
    )

    async def _fake_load(value):
        return {"rle": value}

    monkeypatch.setattr(service, "load_coco_rle", _fake_load)
    monkeypatch.setattr(
        service,
        "coco_rle_bbox_norm",
        lambda _rle: {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
    )

    assert await service._seed_geometry_at_frame(annotation, 0) == {
        "type": "bbox",
        "x": 0.1,
        "y": 0.2,
        "w": 0.3,
        "h": 0.4,
    }


async def test_create_tracker_job_rejects_unavailable_real_model(
    db_session, super_admin
):
    """真实 tracker 没有项目已启用且已连接的能力后端时，在排队前直接拒绝。"""
    from fastapi import HTTPException

    from app.schemas.video_tracker_job import VideoTrackerPropagateRequest
    from app.services.video_frame_service import build_context_from_task
    from app.services.video_tracking.jobs import create_tracker_job

    user, _ = super_admin
    task, _item = await _make_video_task(db_session, user.id)
    ctx = await build_context_from_task(db_session, task)
    payload = VideoTrackerPropagateRequest(
        from_frame=0,
        to_frame=3,
        model_key="sam3_video",
        text="pedestrian",
        target_class_name="pedestrian",
    )

    with pytest.raises(HTTPException) as exc:
        await create_tracker_job(
            db_session,
            task=task,
            ctx=ctx,
            annotation_id=None,
            payload=payload,
            user=user,
        )

    assert exc.value.status_code == 422
    assert "No connected project ML backend" in str(exc.value.detail)


async def test_create_tracker_job_with_source_keeps_target_null(
    db_session, super_admin
):
    """有源延展: source_annotation_id 给出时 target_* 留空 (继承源, 不写显式类别)。"""
    from app.schemas.video_tracker_job import VideoTrackerPropagateRequest
    from app.services.video_frame_service import build_context_from_task
    from app.services.video_tracking.jobs import create_tracker_job

    user, _ = super_admin
    task, item = await _make_video_task(db_session, user.id)
    source = Annotation(
        task_id=task.id,
        project_id=task.project_id,
        user_id=user.id,
        annotation_type="video_track_bbox",
        class_name="car",
        tool_unit_id="bbox",
        geometry={
            "type": "video_track_bbox",
            "track_id": "trk_src",
            "keyframes": [
                {
                    "frame_index": 0,
                    "bbox": {"x": 1, "y": 2, "w": 3, "h": 4},
                    "source": "manual",
                }
            ],
            "outside": [],
        },
        track_id="trk_src",
    )
    db_session.add(source)
    await db_session.flush()
    source_id = source.id
    ctx = await build_context_from_task(db_session, task)
    payload = VideoTrackerPropagateRequest(
        from_frame=0,
        to_frame=3,
        model_key="mock_bbox",
        target_class_name="pedestrian",  # 有源时应被忽略
    )
    body = await create_tracker_job(
        db_session,
        task=task,
        ctx=ctx,
        annotation_id=source_id,
        payload=payload,
        user=user,
    )
    job = await db_session.get(VideoTrackerJob, body.id)
    assert job.annotation_id == source_id
    assert job.target_class_name is None


async def test_runner_single_instance_no_extra_tracks(
    db_session, super_admin, monkeypatch
):
    """单实例 (instance_id 全 None) 不建新 annotation —— 零回归。"""
    user, _ = super_admin
    task, item = await _make_video_task(db_session, user.id)
    source = Annotation(
        task_id=task.id,
        project_id=task.project_id,
        user_id=user.id,
        annotation_type="bbox",
        class_name="car",
        geometry={"type": "bbox", "x": 1, "y": 2, "w": 10, "h": 12},
    )
    db_session.add(source)
    await db_session.flush()
    job = VideoTrackerJob(
        task_id=task.id,
        dataset_item_id=item.id,
        annotation_id=source.id,
        created_by=user.id,
        status=VideoTrackerJobStatus.QUEUED.value,
        model_key="mock_bbox",
        direction="forward",
        from_frame=0,
        to_frame=2,
        prompt={
            "type": "bbox",
            "geometry": source.geometry,
            "expected_source_versions": {str(source.id): int(source.version)},
        },
        event_channel="video-tracker-job:test",
    )
    db_session.add(job)
    await db_session.commit()

    async def collect(_channel: str, _payload: dict) -> None:
        return None

    from sqlalchemy import select

    await run_tracker_job(db_session, job.id, publisher=collect)
    await db_session.refresh(job)
    assert job.status == "pending_review"
    await accept_tracker_job(db_session, job.id, actor_id=user.id, publisher=collect)
    await db_session.refresh(job)
    assert job.status == "accepted"

    extra = (
        (
            await db_session.execute(
                select(Annotation).where(
                    Annotation.task_id == task.id,
                    Annotation.source == "ai_tracker",
                )
            )
        )
        .scalars()
        .all()
    )
    assert extra == []


async def test_runner_discard_leaves_annotation_untouched(
    db_session, super_admin, monkeypatch
):
    """v0.21.28 · 丢弃候选: 源 annotation 零改动, 无新轨迹, staged_result 清空。"""
    user, _ = super_admin
    task, item = await _make_video_task(db_session, user.id)
    source = Annotation(
        task_id=task.id,
        project_id=task.project_id,
        user_id=user.id,
        annotation_type="bbox",
        class_name="car",
        tool_unit_id="bbox",
        geometry={"type": "bbox", "x": 1, "y": 2, "w": 10, "h": 12},
    )
    db_session.add(source)
    await db_session.flush()
    job = VideoTrackerJob(
        task_id=task.id,
        dataset_item_id=item.id,
        annotation_id=source.id,
        created_by=user.id,
        status=VideoTrackerJobStatus.QUEUED.value,
        model_key="sam3_video",
        direction="forward",
        from_frame=0,
        to_frame=3,
        prompt={
            "text": "car",
            "expected_source_versions": {str(source.id): int(source.version)},
        },
        event_channel="video-tracker-job:test",
    )
    db_session.add(job)
    await db_session.commit()

    monkeypatch.setattr(settings, "video_tracker_sam3_window_size_frames", 2)
    adapter = _MultiInstanceAdapter(extra_ids=["obj1"])
    monkeypatch.setattr(
        "app.services.video_tracking.runner.get_tracker_adapter",
        lambda _model_key: adapter,
    )

    async def collect(_channel: str, _payload: dict) -> None:
        return None

    from sqlalchemy import select

    await run_tracker_job(db_session, job.id, publisher=collect)
    await db_session.refresh(job)
    assert job.status == "pending_review"

    await discard_tracker_job(db_session, job.id, publisher=collect)
    await db_session.refresh(job)
    await db_session.refresh(source)

    assert job.status == "discarded"
    assert job.staged_result is None  # 清空
    assert source.annotation_type == "bbox"  # 源零改动
    extra = (
        (
            await db_session.execute(
                select(Annotation).where(
                    Annotation.task_id == task.id,
                    Annotation.source == "ai_tracker",
                )
            )
        )
        .scalars()
        .all()
    )
    assert extra == []


# ── v0.21.28 · B-mx · text-multiplex 跨窗 IoU 关联 ─────────────────────


def _mr(
    frame: int, instance_id: str, x: float, *, primary: bool = False
) -> TrackerFrameResult:
    return TrackerFrameResult(
        frame_index=frame,
        geometry={"type": "bbox", "x": x, "y": 0.0, "w": 0.1, "h": 0.1},
        confidence=1.0,
        outside=False,
        instance_id=instance_id,
        primary=primary,
    )


def test_associate_multiplex_window_matches_by_iou_and_births_new():
    from app.services.video_tracking.runner import _associate_multiplex_window

    prev = {
        "1": {"type": "bbox", "x": 0.10, "y": 0.0, "w": 0.1, "h": 0.1},
        "2": {"type": "bbox", "x": 0.50, "y": 0.0, "w": 0.1, "h": 0.1},
    }
    nxt = [3]  # 下一个新全局 id
    win = [
        _mr(2, "a", 0.11),  # 与 global 1 边界高 IoU → 复用 "1"
        _mr(2, "b", 0.51),  # 与 global 2 → "2"
        _mr(2, "c", 0.90),  # 无匹配 → 新 global "3"
    ]
    out = _associate_multiplex_window(win, prev, nxt)
    remap = {r.geometry["x"]: r.instance_id for r in out}
    assert remap[0.11] == "1"
    assert remap[0.51] == "2"
    assert remap[0.90] == "3"
    assert nxt == [4]  # 消耗了一个新全局 id
    assert set(prev) == {"1", "2", "3"}  # prev 更新为本窗末帧几何


def test_associate_multiplex_window_empty_window_keeps_boundary():
    # 空窗 (backend 空窗返回) 不应抹掉跨窗边界, 否则下一窗全部实例被当作新发现。
    from app.services.video_tracking.runner import _associate_multiplex_window

    prev = {
        "1": {"type": "bbox", "x": 0.10, "y": 0.0, "w": 0.1, "h": 0.1},
        "2": {"type": "bbox", "x": 0.50, "y": 0.0, "w": 0.1, "h": 0.1},
    }
    before = {k: dict(v) for k, v in prev.items()}
    nxt = [3]
    out = _associate_multiplex_window([], prev, nxt)
    assert out == []
    assert prev == before  # 边界保留
    assert nxt == [3]  # 未消耗新全局 id


def test_associate_multiplex_window_all_outside_keeps_boundary():
    # 短暂遮挡 → 整窗全 outside (无几何) 时同样保留边界, 避免同物体遮挡后被拆两条轨迹。
    from app.services.video_tracking.runner import _associate_multiplex_window

    prev = {"1": {"type": "bbox", "x": 0.10, "y": 0.0, "w": 0.1, "h": 0.1}}
    before = {k: dict(v) for k, v in prev.items()}
    nxt = [2]
    occluded = TrackerFrameResult(
        frame_index=2,
        geometry={},
        confidence=0.0,
        outside=True,
        instance_id="1",
        primary=False,
    )
    out = _associate_multiplex_window([occluded], prev, nxt)
    assert len(out) == 1
    assert prev == before  # 全 outside 窗不抹跨窗边界


class _WindowLocalMultiplexAdapter:
    """模拟 text-multiplex: 窗内 obj_id **局部且跨窗重排**。两个物理目标 left(x≈0.1)/
    right(x≈0.5); window 0: left=id"1"(primary)/right=id"2", window 1: left=id"2"/right=id"1"
    (id 互换)。平台须按边界帧 IoU 关联成 2 条跨窗一致的轨迹 (left→源, right→新 track)。"""

    model_key = "sam3_video"

    async def propagate(self, ctx: TrackerContext):
        first_window = min(ctx.from_frame, ctx.to_frame) == 0
        for f in range(ctx.from_frame, ctx.to_frame + 1):
            left_id = "1" if first_window else "2"
            right_id = "2" if first_window else "1"
            yield TrackerFrameResult(
                frame_index=f,
                geometry={
                    "type": "bbox",
                    "x": 0.10 + 0.005 * f,
                    "y": 0.0,
                    "w": 0.1,
                    "h": 0.1,
                },
                confidence=1.0,
                outside=False,
                instance_id=left_id,
                primary=True,
            )
            yield TrackerFrameResult(
                frame_index=f,
                geometry={
                    "type": "bbox",
                    "x": 0.50 + 0.005 * f,
                    "y": 0.0,
                    "w": 0.1,
                    "h": 0.1,
                },
                confidence=1.0,
                outside=False,
                instance_id=right_id,
                primary=False,
            )


async def test_runner_associates_window_local_ids_across_windows(
    db_session, super_admin, monkeypatch
):
    """窗内 id 跨窗重排时, 平台 IoU 关联把同一物理目标的帧归到一条全局轨迹 (不因 id 互换分裂)。"""
    from app.services.video_tracking.runner import accept_tracker_job

    user, _ = super_admin
    task, item = await _make_video_task(db_session, user.id)
    source = Annotation(
        task_id=task.id,
        project_id=task.project_id,
        user_id=user.id,
        annotation_type="bbox",
        class_name="car",
        tool_unit_id="bbox",
        geometry={"type": "bbox", "x": 0.1, "y": 0.0, "w": 0.1, "h": 0.1},
    )
    db_session.add(source)
    await db_session.flush()
    job = VideoTrackerJob(
        task_id=task.id,
        dataset_item_id=item.id,
        annotation_id=source.id,
        created_by=user.id,
        status=VideoTrackerJobStatus.QUEUED.value,
        model_key="sam3_video",
        direction="forward",
        from_frame=0,
        to_frame=3,
        prompt={
            "text": "car",
            "expected_source_versions": {str(source.id): int(source.version)},
        },
        event_channel="video-tracker-job:test",
    )
    db_session.add(job)
    await db_session.commit()

    monkeypatch.setattr(
        settings, "video_tracker_sam3_window_size_frames", 2
    )  # 两窗 (0,1)(2,3)
    adapter = _WindowLocalMultiplexAdapter()
    monkeypatch.setattr(
        "app.services.video_tracking.runner.get_tracker_adapter", lambda _k: adapter
    )

    async def collect(_c: str, _p: dict) -> None:
        return None

    await run_tracker_job(db_session, job.id, publisher=collect)
    await accept_tracker_job(db_session, job.id, actor_id=user.id, publisher=collect)
    await db_session.refresh(source)

    # 源轨迹 (primary=left): 4 帧全为 left 几何 (x≈0.1), 不混入 right (x≈0.5)。
    src_kfs = sorted(source.geometry["keyframes"], key=lambda k: k["frame_index"])
    assert [k["frame_index"] for k in src_kfs] == [0, 1, 2, 3]
    assert all(k["bbox"]["x"] < 0.3 for k in src_kfs if k.get("bbox")), (
        "源轨迹应全是 left 目标, 关联把跨窗 id 互换的 left 帧归回一条"
    )

    # right → 恰 1 条新 ai_tracker 轨迹 (非 2 条: 证明未因 id 互换分裂), 4 帧全 right (x≈0.5)。
    from sqlalchemy import select

    rows = (
        (
            await db_session.execute(
                select(Annotation).where(
                    Annotation.task_id == task.id, Annotation.source == "ai_tracker"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1, f"应恰 1 条 right 轨迹, 实得 {len(rows)}"
    right_kfs = sorted(rows[0].geometry["keyframes"], key=lambda k: k["frame_index"])
    assert [k["frame_index"] for k in right_kfs] == [0, 1, 2, 3]
    assert all(k["bbox"]["x"] > 0.3 for k in right_kfs if k.get("bbox"))


# ── v0.22.2 · M · 多选批量: 多源各回填各自源 ──────────────────────────


class _MultiSourceAdapter:
    """两个种子源 obj "1"/"2", 各自几何区分 (obj1 x≈frame, obj2 x≈frame+100), 模拟 PVS
    逐 obj_id 追踪 (instance_id=str(obj_id))。多源批量落库测试用。"""

    model_key = "sam3_video_interactive"

    async def propagate(self, ctx: TrackerContext):
        for f in range(ctx.from_frame, ctx.to_frame + 1):
            yield TrackerFrameResult(
                frame_index=f,
                geometry={
                    "type": "bbox",
                    "x": float(f),
                    "y": 0.0,
                    "w": 10.0,
                    "h": 10.0,
                },
                confidence=1.0,
                outside=False,
                instance_id="1",
                primary=True,
            )
            yield TrackerFrameResult(
                frame_index=f,
                geometry={
                    "type": "bbox",
                    "x": float(f) + 100.0,
                    "y": 0.0,
                    "w": 8.0,
                    "h": 8.0,
                },
                confidence=1.0,
                outside=False,
                instance_id="2",
                primary=False,
            )


async def _seed_two_source_job(db_session, user):
    """建两条源轨迹 + 一个带 {obj_id ↔ source_annotation_id} 种子的多源 job。"""
    task, item = await _make_video_task(db_session, user.id)
    src_a = Annotation(
        task_id=task.id,
        project_id=task.project_id,
        user_id=user.id,
        annotation_type="bbox",
        class_name="car",
        tool_unit_id="bbox",
        geometry={"type": "bbox", "x": 0, "y": 0, "w": 10, "h": 10},
    )
    src_b = Annotation(
        task_id=task.id,
        project_id=task.project_id,
        user_id=user.id,
        annotation_type="bbox",
        class_name="car",
        tool_unit_id="bbox",
        geometry={"type": "bbox", "x": 100, "y": 0, "w": 8, "h": 8},
    )
    db_session.add_all([src_a, src_b])
    await db_session.flush()
    job = VideoTrackerJob(
        task_id=task.id,
        dataset_item_id=item.id,
        annotation_id=None,  # v0.22.2 · §8 · 多源 job 不认单主 annotation_id → NULL
        created_by=user.id,
        status=VideoTrackerJobStatus.QUEUED.value,
        model_key="sam3_video_interactive",
        direction="forward",
        from_frame=0,
        to_frame=3,
        prompt={
            "seeds": [
                {"obj_id": 1, "source_annotation_id": str(src_a.id)},
                {"obj_id": 2, "source_annotation_id": str(src_b.id)},
            ],
            "expected_source_versions": {
                str(src_a.id): int(src_a.version),
                str(src_b.id): int(src_b.version),
            },
        },
        event_channel="video-tracker-job:test-multi-source",
    )
    db_session.add(job)
    await db_session.commit()
    return task, src_a, src_b, job


async def test_runner_multi_source_backfills_each_own_track(
    db_session, super_admin, monkeypatch
):
    """2 条源轨迹各带 obj_id 种子 → 1 job → 各回填各自源 (不串), 无额外新轨迹。"""
    user, _ = super_admin
    task, src_a, src_b, job = await _seed_two_source_job(db_session, user)

    monkeypatch.setattr(settings, "video_tracker_sam3_window_size_frames", 2)
    adapter = _MultiSourceAdapter()
    monkeypatch.setattr(
        "app.services.video_tracking.runner.get_tracker_adapter", lambda _k: adapter
    )

    async def collect(_c: str, _p: dict) -> None:
        return None

    from sqlalchemy import select

    await run_tracker_job(db_session, job.id, publisher=collect)
    await db_session.refresh(job)
    assert job.status == "pending_review"

    await accept_tracker_job(db_session, job.id, actor_id=user.id, publisher=collect)
    await db_session.refresh(job)
    await db_session.refresh(src_a)
    await db_session.refresh(src_b)
    assert job.status == "accepted"

    # 各回填各自源: src_a=obj1 (x≈frame), src_b=obj2 (x≈frame+100), 帧末几何区分证明不串。
    assert src_a.annotation_type == "video_track_bbox"
    assert src_b.annotation_type == "video_track_bbox"
    assert [kf["frame_index"] for kf in src_a.geometry["keyframes"]] == [0, 1, 2, 3]
    assert [kf["frame_index"] for kf in src_b.geometry["keyframes"]] == [0, 1, 2, 3]
    assert src_a.geometry["keyframes"][3]["bbox"]["x"] == pytest.approx(3.0)
    assert src_b.geometry["keyframes"][3]["bbox"]["x"] == pytest.approx(103.0)

    # 两 obj 都有源 → 无额外 ai_tracker 新轨迹。
    rows = (
        (
            await db_session.execute(
                select(Annotation).where(
                    Annotation.task_id == task.id,
                    Annotation.source == "ai_tracker",
                )
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


async def test_accept_multi_source_soft_deleted_source_fails_closed(
    db_session, super_admin, monkeypatch
):
    """一源在 job 运行后被软删 → 整批冲突且零写入。"""
    from app.services.video_tracking.runner import TrackerJobStateConflict

    user, _ = super_admin
    task, src_a, src_b, job = await _seed_two_source_job(db_session, user)

    monkeypatch.setattr(settings, "video_tracker_sam3_window_size_frames", 2)
    adapter = _MultiSourceAdapter()
    monkeypatch.setattr(
        "app.services.video_tracking.runner.get_tracker_adapter", lambda _k: adapter
    )

    async def collect(_c: str, _p: dict) -> None:
        return None

    from sqlalchemy import select

    await run_tracker_job(db_session, job.id, publisher=collect)
    await db_session.refresh(job)
    assert job.status == "pending_review"

    # 接受前软删 src_b (obj2 的源)。
    src_b.is_active = False
    await db_session.flush()

    with pytest.raises(TrackerJobStateConflict, match="inactive"):
        await accept_tracker_job(
            db_session, job.id, actor_id=user.id, publisher=collect
        )
    await db_session.refresh(job)
    await db_session.refresh(src_a)
    assert job.status == "pending_review"
    assert src_a.annotation_type == "bbox"

    # 不把失去来源的候选静默降级为新轨迹。
    rows = (
        (
            await db_session.execute(
                select(Annotation).where(
                    Annotation.task_id == task.id,
                    Annotation.source == "ai_tracker",
                )
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


# ── v0.22.2 · M1 · 建 job 侧: 多源 endpoint/schema/service ────────────────


async def test_create_tracker_job_multi_source_builds_seeds(db_session, super_admin):
    """v0.22.2 · M · 多选批量: source_annotation_ids ≥1 → job.annotation_id 为 NULL,
    prompt.seeds 每源一条 (obj_id 1..N + source_annotation_id + from_frame 处几何)。"""
    from app.schemas.video_tracker_job import VideoTrackerPropagateRequest
    from app.services.video_frame_service import build_context_from_task
    from app.services.video_tracking.jobs import create_tracker_job

    user, _ = super_admin
    task, item = await _make_video_task(db_session, user.id)
    # 普通 bbox 源 (resolve 取不到 → 回退整条几何) + video_track_bbox 源 (取 frame 0 关键帧)。
    src_a = Annotation(
        task_id=task.id,
        project_id=task.project_id,
        user_id=user.id,
        annotation_type="bbox",
        class_name="car",
        tool_unit_id="bbox",
        geometry={"type": "bbox", "x": 0, "y": 0, "w": 10, "h": 10},
    )
    src_b = Annotation(
        task_id=task.id,
        project_id=task.project_id,
        user_id=user.id,
        annotation_type="video_track_bbox",
        class_name="car",
        tool_unit_id="bbox",
        geometry={
            "type": "video_track_bbox",
            "track_id": "trk_b",
            "keyframes": [
                {
                    "frame_index": 0,
                    "bbox": {"x": 5, "y": 5, "w": 3, "h": 3},
                    "source": "manual",
                }
            ],
            "outside": [],
        },
        track_id="trk_b",
    )
    db_session.add_all([src_a, src_b])
    await db_session.flush()
    source_ids = [src_a.id, src_b.id]
    ctx = await build_context_from_task(db_session, task)
    payload = VideoTrackerPropagateRequest(
        from_frame=0,
        to_frame=3,
        model_key="mock_bbox",
        source_annotation_ids=source_ids,
        target_class_name="pedestrian",  # 多源时应被忽略 (各源继承自身 label)
    )
    body = await create_tracker_job(
        db_session, task=task, ctx=ctx, annotation_id=None, payload=payload, user=user
    )
    job = await db_session.get(VideoTrackerJob, body.id)
    assert job is not None
    assert job.annotation_id is None  # 多源不认单主
    assert job.target_class_name is None  # 多源不存显式目标类别
    seeds = job.prompt["seeds"]
    assert [s["obj_id"] for s in seeds] == [1, 2]
    assert seeds[0]["source_annotation_id"] == str(source_ids[0])
    assert seeds[1]["source_annotation_id"] == str(source_ids[1])
    # 普通 bbox 源: 回退整条几何。
    assert seeds[0]["geometry"] == {"type": "bbox", "x": 0, "y": 0, "w": 10, "h": 10}
    # video_track_bbox 源: 取 frame 0 关键帧 → result-style bbox。
    assert seeds[1]["geometry"] == {"type": "bbox", "x": 5, "y": 5, "w": 3, "h": 3}


async def test_track_video_endpoint_multi_source_builds_job(
    httpx_client, super_admin, db_session, monkeypatch
):
    """v0.22.2 · M · track_video 端点透传 source_annotation_ids: 多源建 job (annotation_id
    为 None, prompt.seeds 各源一条)。"""
    user, token = super_admin
    task, item = await _make_video_task(db_session, user.id)
    src_a = Annotation(
        task_id=task.id,
        project_id=task.project_id,
        user_id=user.id,
        annotation_type="bbox",
        class_name="car",
        tool_unit_id="bbox",
        geometry={"type": "bbox", "x": 0, "y": 0, "w": 10, "h": 10},
    )
    src_b = Annotation(
        task_id=task.id,
        project_id=task.project_id,
        user_id=user.id,
        annotation_type="bbox",
        class_name="car",
        tool_unit_id="bbox",
        geometry={"type": "bbox", "x": 100, "y": 0, "w": 8, "h": 8},
    )
    db_session.add_all([src_a, src_b])
    await db_session.flush()
    await db_session.commit()

    class FakeAsyncResult:
        id = "tracker-celery-task"

    monkeypatch.setattr(
        "celery.current_app.send_task",
        lambda name, args=None, queue=None, **kwargs: FakeAsyncResult(),
    )

    resp = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/video:track",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "from_frame": 0,
            "to_frame": 3,
            "model_key": "mock_bbox",
            "source_annotation_ids": [str(src_a.id), str(src_b.id)],
        },
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["annotation_id"] is None  # 多源不认单主
    assert body["status"] == "queued"
    row = await db_session.get(VideoTrackerJob, uuid.UUID(body["id"]))
    seeds = row.prompt["seeds"]
    assert [s["obj_id"] for s in seeds] == [1, 2]
    assert {s["source_annotation_id"] for s in seeds} == {str(src_a.id), str(src_b.id)}
    assert all(s["geometry"]["type"] == "bbox" for s in seeds)


class _MultiSourcePlusDiscoveryAdapter:
    """两个种子源 obj"1"/"2" + 一个无源新发现 obj"3", 几何各自区分。多源 accept 的 touched
    (回填源 + 新建) 断言用。"""

    model_key = "sam3_video_interactive"

    async def propagate(self, ctx: TrackerContext):
        for f in range(ctx.from_frame, ctx.to_frame + 1):
            for iid, base in (("1", 0.0), ("2", 100.0), ("3", 200.0)):
                yield TrackerFrameResult(
                    frame_index=f,
                    geometry={
                        "type": "bbox",
                        "x": float(f) + base,
                        "y": 0.0,
                        "w": 8.0,
                        "h": 8.0,
                    },
                    confidence=1.0,
                    outside=False,
                    instance_id=iid,
                    primary=(iid == "1"),
                )


async def test_accept_multi_source_touched_covers_sources_and_created(
    db_session, super_admin, monkeypatch
):
    """v0.22.2 · M · accept 后 job.prompt.touched_annotation_ids 覆盖回填源 + 新建; 未接受
    时为空。VideoTrackerJobOut 把它提到顶层字段。"""
    from app.schemas.video_tracker_job import VideoTrackerJobOut

    user, _ = super_admin
    task, src_a, src_b, job = await _seed_two_source_job(db_session, user)

    monkeypatch.setattr(settings, "video_tracker_sam3_window_size_frames", 2)
    adapter = _MultiSourcePlusDiscoveryAdapter()
    monkeypatch.setattr(
        "app.services.video_tracking.runner.get_tracker_adapter", lambda _k: adapter
    )

    async def collect(_c: str, _p: dict) -> None:
        return None

    from sqlalchemy import select

    await run_tracker_job(db_session, job.id, publisher=collect)
    await db_session.refresh(job)
    assert job.status == "pending_review"
    # 未接受 → 无 touched。
    assert "touched_annotation_ids" not in (job.prompt or {})
    assert (
        VideoTrackerJobOut.model_validate(
            job, from_attributes=True
        ).touched_annotation_ids
        is None
    )

    await accept_tracker_job(db_session, job.id, actor_id=user.id, publisher=collect)
    await db_session.refresh(job)
    assert job.status == "accepted"

    # obj3 无源 → 一条新建 ai_tracker 轨迹。
    created = (
        (
            await db_session.execute(
                select(Annotation).where(
                    Annotation.task_id == task.id, Annotation.source == "ai_tracker"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(created) == 1
    touched = set(job.prompt["touched_annotation_ids"])
    assert touched == {str(src_a.id), str(src_b.id), str(created[0].id)}
    # VideoTrackerJobOut 顶层字段与 prompt 落库一致。
    out = VideoTrackerJobOut.model_validate(job, from_attributes=True)
    assert {str(x) for x in out.touched_annotation_ids} == touched


# ── v0.23.5 · WS-D · D4 · accept version conflict → 409 ────────────────


async def _seed_pending_review_job_with_source(
    db_session, owner_id, *, expected_versions: dict | None
):
    """Run a job to pending_review; optionally inject expected_source_versions
    into prompt (mirroring what create_tracker_job records at creation time)."""
    task, item = await _make_video_task(db_session, owner_id)
    source = Annotation(
        task_id=task.id,
        project_id=task.project_id,
        user_id=owner_id,
        annotation_type="bbox",
        class_name="car",
        tool_unit_id="bbox",
        geometry={"type": "bbox", "x": 1, "y": 2, "w": 10, "h": 12},
    )
    db_session.add(source)
    await db_session.flush()
    prompt: dict = {"text": "car"}
    if expected_versions is not None:
        prompt["expected_source_versions"] = expected_versions
    job = VideoTrackerJob(
        task_id=task.id,
        dataset_item_id=item.id,
        annotation_id=source.id,
        created_by=owner_id,
        status=VideoTrackerJobStatus.QUEUED.value,
        model_key="sam3_video",
        direction="forward",
        from_frame=0,
        to_frame=3,
        prompt=prompt,
        event_channel="video-tracker-job:test",
    )
    db_session.add(job)
    await db_session.commit()

    # Drive the job to pending_review with staged results.
    adapter = _MultiInstanceAdapter(extra_ids=[])
    monkeypatch_holder["adapter"] = adapter
    return task, source, job


# module-level holder so the test body can patch get_tracker_adapter after
# the helper returns (tests need to monkeypatch within their own monkeypatch
# fixture scope).
monkeypatch_holder: dict = {}


async def test_accept_tracker_job_conflict_on_source_version_mismatch(
    db_session, super_admin, monkeypatch
):
    """source annotation version bumped between job creation and accept → 409,
    source geometry unchanged (no last-writer-wins)."""
    from app.services.video_tracking.runner import TrackerJobStateConflict

    user, _ = super_admin
    task, source, job = await _seed_pending_review_job_with_source(
        db_session,
        user.id,
        expected_versions={str("__SOURCE__"): 1},  # placeholder, fixed below
    )
    # Replace the placeholder with the actual source id + version snapshot.
    expected = {str(source.id): int(source.version)}
    job.prompt = {**(job.prompt or {}), "expected_source_versions": expected}
    await db_session.commit()

    monkeypatch.setattr(settings, "video_tracker_sam3_window_size_frames", 2)
    monkeypatch.setattr(
        "app.services.video_tracking.runner.get_tracker_adapter",
        lambda _model_key: monkeypatch_holder["adapter"],
    )

    async def collect(_channel: str, _payload: dict) -> None:
        return None

    from app.services.video_tracking.runner import run_tracker_job

    await run_tracker_job(db_session, job.id, publisher=collect)
    await db_session.refresh(job)
    await db_session.refresh(source)
    assert job.status == "pending_review"
    original_geometry = dict(source.geometry or {})

    # Simulate a concurrent edit: bump the source annotation's version.
    source.version = (source.version or 1) + 1
    await db_session.commit()
    await db_session.refresh(source)

    with pytest.raises(TrackerJobStateConflict) as exc:
        await accept_tracker_job(
            db_session, job.id, actor_id=user.id, publisher=collect
        )
    assert (
        "version changed" in str(exc.value).lower()
        or "conflict" in str(exc.value).lower()
    )

    # Source geometry untouched (no last-writer-wins).
    await db_session.refresh(source)
    assert source.geometry == original_geometry
    # Job is NOT accepted.
    await db_session.refresh(job)
    assert job.status != "accepted"


async def test_accept_tracker_job_legacy_without_expected_versions_fails_closed(
    db_session, super_admin, monkeypatch
):
    """A source candidate without a creation-time version cannot be accepted safely."""
    from app.services.video_tracking.runner import TrackerJobStateConflict

    user, _ = super_admin
    task, source, job = await _seed_pending_review_job_with_source(
        db_session,
        user.id,
        expected_versions=None,  # legacy: no snapshot
    )
    monkeypatch.setattr(settings, "video_tracker_sam3_window_size_frames", 2)
    monkeypatch.setattr(
        "app.services.video_tracking.runner.get_tracker_adapter",
        lambda _model_key: monkeypatch_holder["adapter"],
    )

    async def collect(_channel: str, _payload: dict) -> None:
        return None

    from app.services.video_tracking.runner import run_tracker_job

    await run_tracker_job(db_session, job.id, publisher=collect)
    await db_session.refresh(job)
    assert job.status == "pending_review"

    # Even if the row is currently readable, drift since creation is unknowable.
    source.version = (source.version or 1) + 5
    await db_session.commit()
    await db_session.refresh(source)

    with pytest.raises(TrackerJobStateConflict, match="snapshot is missing"):
        await accept_tracker_job(
            db_session, job.id, actor_id=user.id, publisher=collect
        )
    await db_session.refresh(job)
    assert job.status == "pending_review"
    assert job.staged_result is not None


async def test_accept_tracker_job_rejects_locked_or_soft_deleted_source(
    db_session, super_admin, monkeypatch
):
    from app.services.video_tracking.runner import (
        TrackerJobStateConflict,
        run_tracker_job,
    )

    user, _ = super_admin
    _task, source, job = await _seed_pending_review_job_with_source(
        db_session, user.id, expected_versions=None
    )
    monkeypatch.setattr(settings, "video_tracker_sam3_window_size_frames", 2)
    monkeypatch.setattr(
        "app.services.video_tracking.runner.get_tracker_adapter",
        lambda _model_key: monkeypatch_holder["adapter"],
    )

    async def collect(_channel: str, _payload: dict) -> None:
        return None

    await run_tracker_job(db_session, job.id, publisher=collect)
    job.prompt = {
        **(job.prompt or {}),
        "expected_source_versions": {str(source.id): int(source.version)},
    }
    source.is_locked = True
    await db_session.commit()
    with pytest.raises(TrackerJobStateConflict, match="locked"):
        await accept_tracker_job(
            db_session, job.id, actor_id=user.id, publisher=collect
        )


async def test_accept_tracker_job_rechecks_task_status(
    db_session, super_admin, monkeypatch
):
    from app.services.video_tracking.runner import (
        TrackerJobStateConflict,
        run_tracker_job,
    )

    user, _ = super_admin
    task, _source, job = await _seed_pending_review_job_with_source(
        db_session, user.id, expected_versions=None
    )
    monkeypatch.setattr(settings, "video_tracker_sam3_window_size_frames", 2)
    monkeypatch.setattr(
        "app.services.video_tracking.runner.get_tracker_adapter",
        lambda _model_key: monkeypatch_holder["adapter"],
    )

    async def collect(_channel: str, _payload: dict) -> None:
        return None

    await run_tracker_job(db_session, job.id, publisher=collect)
    # Review-state accept is rejected by the canonical frozen-evidence guard
    # (unknown legacy evidence) before the legacy status lock message.
    task.status = "review"
    await db_session.commit()
    with pytest.raises(TrackerJobStateConflict) as exc:
        await accept_tracker_job(
            db_session, job.id, actor_id=user.id, publisher=collect
        )
    assert exc.value.detail["reason"] == "review_contributors_unknown"

    # A completed task is still rejected by the status/phase lock.
    task.status = "completed"
    await db_session.commit()
    with pytest.raises(TrackerJobStateConflict) as exc:
        await accept_tracker_job(
            db_session, job.id, actor_id=user.id, publisher=collect
        )
    assert exc.value.detail["reason"] == "task_locked"


async def test_accept_tracker_job_rechecks_segment_lease(
    db_session, super_admin, monkeypatch
):
    from app.services.video_tracking.runner import (
        TrackerJobStateConflict,
        run_tracker_job,
    )

    user, _ = super_admin
    _task, _source, job = await _seed_pending_review_job_with_source(
        db_session, user.id, expected_versions=None
    )
    segment = VideoSegment(
        dataset_item_id=job.dataset_item_id,
        segment_index=0,
        start_frame=0,
        end_frame=3,
        assignee_id=user.id,
        locked_by=user.id,
        lock_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    db_session.add(segment)
    await db_session.flush()
    job.segment_id = segment.id
    await db_session.commit()
    monkeypatch.setattr(settings, "video_tracker_sam3_window_size_frames", 2)
    monkeypatch.setattr(
        "app.services.video_tracking.runner.get_tracker_adapter",
        lambda _model_key: monkeypatch_holder["adapter"],
    )

    async def collect(_channel: str, _payload: dict) -> None:
        return None

    await run_tracker_job(db_session, job.id, publisher=collect)
    with pytest.raises(TrackerJobStateConflict, match="segment lease"):
        await accept_tracker_job(
            db_session, job.id, actor_id=user.id, publisher=collect
        )
