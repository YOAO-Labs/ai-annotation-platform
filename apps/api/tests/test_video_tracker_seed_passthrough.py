"""v0.21.27 · U-pvs-1 · runner 透传 PVS 点/多目标种子 (画布点 → PVS track)。

PVS backend 已支持 `context.seeds[]` (含 points), 缺口在平台侧: runner 需从 job.prompt
读出 seeds 并经 TrackerContext → adapter context 透传。关键契约:
- seeds 只在**种子窗 (首窗, 含原始种子帧)** 下发 —— points 锚在种子帧, 后续窗靠
  source_geometry (上一窗末帧框) 续追, 不重发点种子;
- 无 seeds 时行为与已发 B-pvs 框种子完全一致 (零回归)。
"""

import pytest

from app.config import settings
from app.db.models.annotation import Annotation
from app.db.models.video_tracker_job import VideoTrackerJob, VideoTrackerJobStatus
from app.services.video_tracking.adapters import TrackerContext, TrackerFrameResult
from app.services.video_tracking.runner import run_tracker_job
from tests.tracker_input_test_ports import yield_tracker_fake_results

# 复用多实例测试里的视频 task/dataset 建置, 避免重复 ~40 行 fixture。
from tests.test_video_tracker_multi_instance import _make_video_task


@pytest.fixture(autouse=True)
def _tracker_input_owner_test_port(tracker_input_owner_fakes, savepoint_routing_owner):
    tracker_input_owner_fakes.declare_tracker_backend = True
    return tracker_input_owner_fakes


class _CaptureAdapter:
    """逐窗记录 (from_frame, to_frame, seeds), 每帧回一条主实例结果。"""

    model_key = "sam3_video_interactive"

    def __init__(self) -> None:
        self.windows: list[tuple[int, int, object]] = []

    async def propagate(self, ctx: TrackerContext):
        self.windows.append((ctx.from_frame, ctx.to_frame, ctx.seeds))
        results = []
        for frame_index in range(ctx.from_frame, ctx.to_frame + 1):
            results.append(
                TrackerFrameResult(
                    frame_index=frame_index,
                    geometry={
                        "type": "bbox",
                        "x": float(frame_index),
                        "y": 0.0,
                        "w": 5.0,
                        "h": 5.0,
                    },
                    confidence=1.0,
                    outside=False,
                )
            )
        async for result in yield_tracker_fake_results(ctx, results):
            yield result


async def _run_with_capture(db_session, super_admin, monkeypatch, *, direction: str):
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
    seeds = [{"obj_id": 1, "points": [[0.5, 0.5, 1]]}]
    job = VideoTrackerJob(
        task_id=task.id,
        dataset_item_id=item.id,
        annotation_id=source.id,
        created_by=user.id,
        status=VideoTrackerJobStatus.QUEUED.value,
        model_key="sam3_video_interactive",
        direction=direction,
        from_frame=0,
        to_frame=3,
        prompt={"seeds": seeds},
        event_channel="video-tracker-job:test",
    )
    db_session.add(job)
    await db_session.commit()

    # 窗口 2 帧 → (0,1)(2,3) 两窗。
    monkeypatch.setattr(settings, "video_tracker_sam3_window_size_frames", 2)
    adapter = _CaptureAdapter()
    monkeypatch.setattr(
        "app.services.video_tracking.runner.get_tracker_adapter",
        lambda _model_key: adapter,
    )

    async def collect(_channel: str, _payload: dict) -> None:
        return None

    await run_tracker_job(db_session, job.id, publisher=collect)
    await db_session.refresh(job)
    assert job.status == "pending_review"
    return adapter, seeds


async def test_seeds_only_on_seed_window_forward(db_session, super_admin, monkeypatch):
    adapter, seeds = await _run_with_capture(
        db_session, super_admin, monkeypatch, direction="forward"
    )
    # forward: 迭代顺序 (0,1)(2,3); 种子窗 = 首窗 (0,1)。
    assert [(w[0], w[1]) for w in adapter.windows] == [(0, 1), (2, 3)]
    assert adapter.windows[0][2] == seeds  # 种子窗带 points
    assert adapter.windows[1][2] is None  # 后续窗不重发


async def test_seeds_only_on_seed_window_backward(db_session, super_admin, monkeypatch):
    adapter, seeds = await _run_with_capture(
        db_session, super_admin, monkeypatch, direction="backward"
    )
    # backward: 窗口列表反转, 首个迭代窗 = 含种子帧(to_frame=3)的 (2,3)。
    assert [(w[0], w[1]) for w in adapter.windows] == [(2, 3), (0, 1)]
    assert adapter.windows[0][2] == seeds  # 种子窗(含 to_frame)带 points
    assert adapter.windows[1][2] is None


class _TwoInstanceCaptureAdapter:
    """每窗每帧产 2 实例: "1"(primary) + "2"(extra); 记录每窗收到的 ctx.seeds。"""

    model_key = "sam3_video_interactive"

    def __init__(self) -> None:
        self.windows: list[tuple[int, int, object]] = []

    async def propagate(self, ctx: TrackerContext):
        self.windows.append((ctx.from_frame, ctx.to_frame, ctx.seeds))
        results = []
        for f in range(ctx.from_frame, ctx.to_frame + 1):
            results.append(
                TrackerFrameResult(
                    frame_index=f,
                    geometry={
                        "type": "bbox",
                        "x": float(f),
                        "y": 0.0,
                        "w": 5.0,
                        "h": 5.0,
                    },
                    confidence=1.0,
                    outside=False,
                    instance_id="1",
                    primary=True,
                )
            )
            results.append(
                TrackerFrameResult(
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
            )
        async for result in yield_tracker_fake_results(ctx, results):
            yield result


class _SingleInstanceWithoutPrimaryCaptureAdapter:
    """模拟真实 PVS：单目标带 instance_id，但不额外标记 primary。"""

    model_key = "sam3_video_interactive"

    def __init__(self, *, reverse_results: bool = False) -> None:
        self.windows: list[tuple[int, int, object, dict]] = []
        self.reverse_results = reverse_results

    async def propagate(self, ctx: TrackerContext):
        self.windows.append(
            (ctx.from_frame, ctx.to_frame, ctx.seeds, ctx.source_geometry)
        )
        frame_indices = list(range(ctx.from_frame, ctx.to_frame + 1))
        if self.reverse_results:
            frame_indices.reverse()
        results = []
        for frame_index in frame_indices:
            results.append(
                TrackerFrameResult(
                    frame_index=frame_index,
                    geometry={
                        "type": "bbox",
                        "x": float(frame_index),
                        "y": 0.0,
                        "w": 5.0,
                        "h": 5.0,
                    },
                    confidence=1.0,
                    outside=False,
                    instance_id="1",
                    primary=False,
                )
            )
        async for result in yield_tracker_fake_results(ctx, results):
            yield result


@pytest.mark.parametrize(
    ("direction", "reverse_results", "expected_boundary_x"),
    [
        ("forward", False, 1.0),
        ("forward", True, 1.0),
        ("backward", False, 2.0),
    ],
)
async def test_single_instance_without_primary_continues_from_previous_window(
    db_session,
    super_admin,
    monkeypatch,
    direction,
    reverse_results,
    expected_boundary_x,
):
    """无源单目标 PVS 必须按追踪方向使用窗口边界帧续追。"""
    user, _ = super_admin
    task, item = await _make_video_task(db_session, user.id)
    seeds = [{"obj_id": 1, "points": [[0.5, 0.5, 1]]}]
    job = VideoTrackerJob(
        task_id=task.id,
        dataset_item_id=item.id,
        annotation_id=None,
        target_class_name="car",
        created_by=user.id,
        status=VideoTrackerJobStatus.QUEUED.value,
        model_key="sam3_video_interactive",
        direction=direction,
        from_frame=0,
        to_frame=3,
        prompt={"seeds": seeds},
        event_channel="video-tracker-job:test-single-instance",
    )
    db_session.add(job)
    await db_session.commit()

    monkeypatch.setattr(settings, "video_tracker_sam3_window_size_frames", 2)
    adapter = _SingleInstanceWithoutPrimaryCaptureAdapter(
        reverse_results=reverse_results
    )
    monkeypatch.setattr(
        "app.services.video_tracking.runner.get_tracker_adapter",
        lambda _model_key: adapter,
    )

    async def collect(_channel: str, _payload: dict) -> None:
        return None

    await run_tracker_job(db_session, job.id, publisher=collect)
    await db_session.refresh(job)
    assert job.status == "pending_review"
    assert adapter.windows[0][2] == seeds
    assert adapter.windows[1][2] is None
    assert adapter.windows[1][3]["x"] == pytest.approx(expected_boundary_x)


async def test_multi_instance_continuation_reseeds_each_instance(
    db_session, super_admin, monkeypatch
):
    """多目标: 首窗下发原始 seeds, 后续窗对每个实例各用其末帧几何 + 对应 obj_id 续种。"""
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
    seeds = [
        {"obj_id": 1, "points": [[0.5, 0.5, 1]]},
        {"obj_id": 2, "points": [[0.8, 0.5, 1]]},
    ]
    job = VideoTrackerJob(
        task_id=task.id,
        dataset_item_id=item.id,
        annotation_id=source.id,
        created_by=user.id,
        status=VideoTrackerJobStatus.QUEUED.value,
        model_key="sam3_video_interactive",
        direction="forward",
        from_frame=0,
        to_frame=3,
        prompt={"seeds": seeds},
        event_channel="video-tracker-job:test",
    )
    db_session.add(job)
    await db_session.commit()

    monkeypatch.setattr(settings, "video_tracker_sam3_window_size_frames", 2)
    adapter = _TwoInstanceCaptureAdapter()
    monkeypatch.setattr(
        "app.services.video_tracking.runner.get_tracker_adapter",
        lambda _model_key: adapter,
    )

    async def collect(_channel: str, _payload: dict) -> None:
        return None

    await run_tracker_job(db_session, job.id, publisher=collect)
    await db_session.refresh(job)
    assert job.status == "pending_review"

    # 两窗 (0,1)(2,3); 首窗下发原始点种子。
    assert [(w[0], w[1]) for w in adapter.windows] == [(0, 1), (2, 3)]
    assert adapter.windows[0][2] == seeds
    # 后续窗对每个实例各续一条种子 (obj_id 1/2), 各带其上一窗末帧 (f1) 几何。
    cont = adapter.windows[1][2]
    assert cont is not None
    by_obj = {s["obj_id"]: s for s in cont}
    assert set(by_obj) == {1, 2}
    assert by_obj[1]["geometry"]["x"] == pytest.approx(1.0)  # obj1 末帧 (f1) x=1
    assert by_obj[2]["geometry"]["x"] == pytest.approx(101.0)  # obj2 末帧 x=101


async def test_no_seeds_prompt_passes_none(db_session, super_admin, monkeypatch):
    """prompt 无 seeds → ctx.seeds 全 None (框种子/文本追踪零回归)。"""
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
        model_key="sam3_video_interactive",
        direction="forward",
        from_frame=0,
        to_frame=3,
        prompt={},
        event_channel="video-tracker-job:test",
    )
    db_session.add(job)
    await db_session.commit()

    monkeypatch.setattr(settings, "video_tracker_sam3_window_size_frames", 2)
    adapter = _CaptureAdapter()
    monkeypatch.setattr(
        "app.services.video_tracking.runner.get_tracker_adapter",
        lambda _model_key: adapter,
    )

    async def collect(_channel: str, _payload: dict) -> None:
        return None

    await run_tracker_job(db_session, job.id, publisher=collect)
    assert all(w[2] is None for w in adapter.windows)
