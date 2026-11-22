"""B-combo · sam3_video_combo = multiplex 发现 → PVS 追踪 两趟编排。

seam A(改动集中在 runner, backend 零改): runner 先用 multiplex(sam3_video)在种子帧按
text 检测出 per-obj 框, 铸成 PVS 逐对象种子(无源), 再用 PVS(sam3_video_interactive)跑
窗循环逐对象记忆追踪。关键契约:
- 发现趟只在种子帧(from_frame)跑一次 multiplex, 且带 text;
- 发现框 → PVS 种子 obj_id 从 1 连续、geometry=发现框(无 source_annotation_id → 新建);
- 追踪趟首窗下发这些种子, 后续窗靠 PVS 跨窗续种;
- 无 text / 发现不到目标 → job 失败(无种子无法追踪)。
"""

import pytest

from app.config import settings
from app.db.models.video_tracker_job import VideoTrackerJob, VideoTrackerJobStatus
from app.services.video_tracking.adapters import TrackerContext
from yoao_inference.tracker import TrackerFrameResult
from app.services.video_tracking.runner import (
    _combo_seeds_from_discovery,
    run_tracker_job,
)

from tests.test_video_tracker_multi_instance import _make_video_task
from tests.tracker_input_test_ports import tracker_fake_propagation


@pytest.fixture(autouse=True)
def _tracker_input_owner_test_port(tracker_input_owner_fakes):
    tracker_input_owner_fakes.declare_tracker_backend = True
    return tracker_input_owner_fakes


def _disc(frame_index, instance_id, x):
    return TrackerFrameResult(
        frame_index=frame_index,
        geometry={"type": "bbox", "x": float(x), "y": 0.0, "w": 5.0, "h": 5.0},
        confidence=1.0,
        outside=False,
        instance_id=instance_id,
    )


def test_combo_seeds_from_discovery_mints_sequential_obj_ids():
    seeds = _combo_seeds_from_discovery(
        [_disc(0, "2", 100.0), _disc(0, "1", 10.0)], seed_frame=0
    )
    assert [s["obj_id"] for s in seeds] == [1, 2]
    assert seeds[0]["geometry"]["x"] == pytest.approx(10.0)
    assert seeds[1]["geometry"]["x"] == pytest.approx(100.0)
    assert all("source_annotation_id" not in s for s in seeds)


def test_combo_seeds_from_discovery_skips_outside_and_empty():
    outside = TrackerFrameResult(
        frame_index=0,
        geometry={"type": "bbox", "x": 1.0, "y": 0, "w": 5, "h": 5},
        instance_id="9",
        outside=True,
    )
    empty = TrackerFrameResult(frame_index=0, geometry={}, instance_id="8")
    seeds = _combo_seeds_from_discovery(
        [outside, _disc(0, "1", 10.0), empty], seed_frame=0
    )
    assert [s["obj_id"] for s in seeds] == [1]
    assert seeds[0]["geometry"]["x"] == pytest.approx(10.0)


def test_combo_seeds_from_discovery_only_seed_frame():
    seeds = _combo_seeds_from_discovery(
        [_disc(0, "1", 10.0), _disc(1, "1", 12.0), _disc(2, "2", 200.0)],
        seed_frame=0,
    )
    assert [s["obj_id"] for s in seeds] == [1]
    assert seeds[0]["geometry"]["x"] == pytest.approx(10.0)


def test_combo_seeds_from_discovery_empty():
    assert _combo_seeds_from_discovery([], seed_frame=0) == []


class _DiscoveryAdapter:
    """multiplex 发现趟: 记录调用(from/to/text), 在窗内每帧回 N 个对象框(种子帧铸种)。"""

    model_key = "sam3_video"

    def __init__(self, n_objects: int) -> None:
        self.n = n_objects
        self.calls: list[tuple[int, int, object]] = []

    async def propagate(self, ctx: TrackerContext):
        self.calls.append((ctx.from_frame, ctx.to_frame, ctx.text))
        results = []
        for f in range(ctx.from_frame, ctx.to_frame + 1):
            for k in range(self.n):
                results.append(
                    TrackerFrameResult(
                        frame_index=f,
                        geometry={
                            "type": "bbox",
                            "x": float(10 + 100 * k),
                            "y": 0.0,
                            "w": 8.0,
                            "h": 8.0,
                        },
                        confidence=1.0,
                        outside=False,
                        instance_id=str(k + 1),
                    )
                )
        return tracker_fake_propagation(ctx, results)


class _PvsCaptureAdapter:
    """PVS 追踪趟: 逐窗记录 seeds, 每帧对每个已播种对象回一条结果。"""

    model_key = "sam3_video_interactive"

    def __init__(self) -> None:
        self.windows: list[tuple[int, int, object]] = []

    async def propagate(self, ctx: TrackerContext):
        self.windows.append((ctx.from_frame, ctx.to_frame, ctx.seeds))
        seeds = ctx.seeds or [{"obj_id": 1}]
        results = []
        for f in range(ctx.from_frame, ctx.to_frame + 1):
            for s in seeds:
                oid = str(s["obj_id"])
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
                        instance_id=oid,
                        primary=(oid == "1"),
                    )
                )
        return tracker_fake_propagation(ctx, results)


def _dispatch(discovery: _DiscoveryAdapter, pvs: _PvsCaptureAdapter):
    def _get(model_key: str):
        if model_key == "sam3_video":
            return discovery
        return pvs

    return _get


async def _make_combo_job(db_session, user, *, text, from_frame=0, to_frame=3):
    task, item = await _make_video_task(db_session, user.id)
    job = VideoTrackerJob(
        task_id=task.id,
        dataset_item_id=item.id,
        annotation_id=None,
        created_by=user.id,
        status=VideoTrackerJobStatus.QUEUED.value,
        model_key="sam3_video_combo",
        direction="forward",
        from_frame=from_frame,
        to_frame=to_frame,
        prompt=(
            {"text": text, "target_class_name": "car", "target_tool_unit_id": "bbox"}
            if text is not None
            else {"target_class_name": "car", "target_tool_unit_id": "bbox"}
        ),
        event_channel="video-tracker-job:test",
    )
    db_session.add(job)
    await db_session.commit()
    return job


async def _noop_pub(_channel: str, _payload: dict) -> None:
    return None


async def test_combo_discovery_mints_pvs_seeds(db_session, super_admin, monkeypatch):
    """发现 2 对象 → 首窗下发 2 条 PVS 种子(obj_id 1/2, geometry=发现框)→ pending_review。"""
    user, _ = super_admin
    job = await _make_combo_job(db_session, user, text="car")

    monkeypatch.setattr(settings, "video_tracker_sam3_window_size_frames", 2)
    discovery = _DiscoveryAdapter(n_objects=2)
    pvs = _PvsCaptureAdapter()
    monkeypatch.setattr(
        "app.services.video_tracking.runner.get_tracker_adapter",
        _dispatch(discovery, pvs),
    )

    await run_tracker_job(db_session, job.id, publisher=_noop_pub)
    await db_session.refresh(job)
    assert job.status == "pending_review"

    assert discovery.calls == [(0, 1, "car")]
    first_seeds = pvs.windows[0][2]
    assert first_seeds is not None
    assert [s["obj_id"] for s in first_seeds] == [1, 2]
    assert first_seeds[0]["geometry"]["x"] == pytest.approx(10.0)
    assert first_seeds[1]["geometry"]["x"] == pytest.approx(110.0)
    assert all("source_annotation_id" not in s for s in first_seeds)


async def test_combo_requires_text(db_session, super_admin, monkeypatch):
    """combo 无 text → 发现无依据 → job 失败(不跑追踪)。"""
    user, _ = super_admin
    job = await _make_combo_job(db_session, user, text=None)

    discovery = _DiscoveryAdapter(n_objects=2)
    pvs = _PvsCaptureAdapter()
    monkeypatch.setattr(
        "app.services.video_tracking.runner.get_tracker_adapter",
        _dispatch(discovery, pvs),
    )

    await run_tracker_job(db_session, job.id, publisher=_noop_pub)
    await db_session.refresh(job)
    assert job.status == "failed"
    assert discovery.calls == []
    assert pvs.windows == []


async def test_combo_no_discovery_fails(db_session, super_admin, monkeypatch):
    """发现趟检测不到目标(0 框)→ 无种子无法追踪 → job 失败。"""
    user, _ = super_admin
    job = await _make_combo_job(db_session, user, text="unicorn")

    discovery = _DiscoveryAdapter(n_objects=0)
    pvs = _PvsCaptureAdapter()
    monkeypatch.setattr(
        "app.services.video_tracking.runner.get_tracker_adapter",
        _dispatch(discovery, pvs),
    )

    await run_tracker_job(db_session, job.id, publisher=_noop_pub)
    await db_session.refresh(job)
    assert job.status == "failed"
    assert discovery.calls == [(0, 3, "unicorn")]
    assert pvs.windows == []
