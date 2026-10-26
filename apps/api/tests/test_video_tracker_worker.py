import json
import pytest
import uuid
import httpx
from datetime import datetime, timezone

from app.config import settings
from app.db.models.annotation import Annotation
from app.db.models.ml_backend_registry import ProjectMLBackendPool
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.video_tracker_job import VideoTrackerJob, VideoTrackerJobStatus
from aap_inference.backend_transport import PredictionResult
from aap_inference_contract.execution import thaw_json
from app.services.video_tracking.adapters import TrackerContext
from aap_inference.tracker import TrackerFrameResult
from app.services.video_tracking.runner import accept_tracker_job, run_tracker_job
from tests.conftest import create_registry_with_pool
from tests.factory import build_tool_bindings
from tests.tracker_input_test_ports import (
    seed_staged_tracker_input,
    install_tracker_execution_fake,
    tracker_fake_propagation,
)


@pytest.fixture(autouse=True)
def _tracker_input_owner_test_port(tracker_input_owner_fakes):
    return tracker_input_owner_fakes


class _TrackerRoutingLedger:
    """Minimal ledger port; tracker routing policy stays in MLBackendRouter."""

    async def circuit_open_instances(self, _pool_id: str) -> set[str]:
        return set()

    async def aclose(self) -> None:
        return None


def _tracker_routing_scope(
    *,
    pool,
    instance_id: uuid.UUID,
    project_id: uuid.UUID | None,
    mode: str,
    owner: str,
    operation: str,
):
    from aap_inference_contract.routing import RoutingScopeView

    return RoutingScopeView(
        id=uuid.uuid4(),
        pool_id=pool.id,
        registry_id=instance_id,
        project_id=project_id,
        mode=mode,
        owner=owner,
        operation=operation,
        generation=pool.routing_generation,
        config_revision=1,
        eligibility_revision=1,
        authority_epoch=uuid.uuid4(),
        state="reserved",
        closed=False,
        heartbeat_deadline_ms=9_999_999_999_999,
        capability="a" * 64,
    )


def _install_tracker_routing_authority(
    monkeypatch,
    *,
    pool,
    instance_id: uuid.UUID,
    project_id: uuid.UUID | None,
    runtime_snapshot=None,
):
    """Stand in for the independent durable routing owner over its public port.

    Tracker jobs pin one instance per job through ``MLBackendRouter``, which only
    consumes the immutable ``RoutingScopeView`` the authority returns. Unit tests
    keep the real router/lease fencing and replace only the HTTP owner boundary.
    """
    from app.services.ml_routing.authority import RoutingAuthority

    calls: dict[str, list] = {"acquire": [], "close": [], "snapshot": []}

    async def acquire(
        _authority,
        *,
        pool_id=None,
        registry_id=None,
        project_id=None,
        mode="direct",
        owner="api",
        operation="backend_http",
        required_instance_id=None,
        execution=None,
    ):
        calls["acquire"].append(
            {
                "pool_id": pool_id,
                "registry_id": registry_id,
                "project_id": project_id,
                "mode": mode,
                "owner": owner,
                "operation": operation,
                "required_instance_id": required_instance_id,
                "execution": execution,
            }
        )
        if required_instance_id is not None and required_instance_id != instance_id:
            raise AssertionError("tracker pinned an unexpected registry instance")
        if pool_id is not None and pool_id != pool.id:
            raise AssertionError("tracker acquired an unexpected service pool")
        if registry_id is not None and registry_id != instance_id:
            raise AssertionError("tracker acquired an unexpected legacy registry")
        return _tracker_routing_scope(
            pool=pool,
            instance_id=instance_id,
            project_id=project_id,
            mode=mode,
            owner=owner,
            operation=operation,
        )

    async def close(_authority, scope, outcome="success"):
        calls["close"].append((scope, outcome))
        return {"closed": True}

    async def snapshot(_authority, registry_ids):
        calls["snapshot"].append(tuple(registry_ids))
        return runtime_snapshot

    monkeypatch.setattr(RoutingAuthority, "acquire", acquire)
    monkeypatch.setattr(RoutingAuthority, "close", close)
    monkeypatch.setattr(RoutingAuthority, "runtime_snapshot", snapshot)
    return calls


async def _make_video_task(db_session, owner_id):
    project = Project(
        display_id=f"P-VTW-{uuid.uuid4().hex[:6]}",
        name="Video Tracker Worker Project",
        type_key="video-track",
        type_label="视频 · 时序追踪",
        owner_id=owner_id,
        tool_bindings=build_tool_bindings(["car"]),
    )
    dataset = Dataset(
        display_id=f"D-VTW-{uuid.uuid4().hex[:6]}",
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
        width=3,
        height=2,
        metadata_={
            "video": {
                "duration_ms": 3000,
                "fps": 30,
                "frame_count": 90,
                "width": 3,
                "height": 2,
            }
        },
    )
    db_session.add(item)
    await db_session.flush()
    task = Task(
        project_id=project.id,
        dataset_item_id=item.id,
        display_id=f"T-VTW-{uuid.uuid4().hex[:6]}",
        file_name="clip.mp4",
        file_path="videos/clip.mp4",
        file_type="video",
        status="pending",
    )
    db_session.add(task)
    await db_session.flush()
    return task, item


async def test_tracker_worker_completes_mock_bbox_job_and_writes_video_track(
    db_session, super_admin
):
    user, _ = super_admin
    task, item = await _make_video_task(db_session, user.id)
    annotation = Annotation(
        task_id=task.id,
        project_id=task.project_id,
        user_id=user.id,
        annotation_type="bbox",
        class_name="car",
        geometry={"type": "bbox", "x": 1, "y": 2, "w": 10, "h": 12},
    )
    db_session.add(annotation)
    await db_session.flush()
    job = VideoTrackerJob(
        task_id=task.id,
        dataset_item_id=item.id,
        annotation_id=annotation.id,
        created_by=user.id,
        status=VideoTrackerJobStatus.QUEUED.value,
        model_key="mock_bbox",
        direction="forward",
        from_frame=0,
        to_frame=2,
        prompt={
            "type": "bbox",
            "geometry": annotation.geometry,
            "expected_source_versions": {str(annotation.id): int(annotation.version)},
        },
        event_channel="video-tracker-job:test",
    )
    db_session.add(job)
    await db_session.commit()
    events: list[dict] = []

    async def collect(_channel: str, payload: dict) -> None:
        # Published events cross a JSON transport boundary; consumers never see
        # the worker's frozen in-memory mappings.
        events.append(json.loads(json.dumps(payload)))

    await run_tracker_job(db_session, job.id, publisher=collect)
    await db_session.refresh(job)
    await db_session.refresh(annotation)

    # v0.21.28 · 候选流: 完成 = 暂存待审, annotation 未回填 (仍原始 bbox)。
    assert job.status == "pending_review"
    assert annotation.annotation_type == "bbox"
    assert job.staged_result and job.staged_result["results"]
    assert [event["type"] for event in events] == [
        "job_started",
        "frame_result",
        "job_progress",
        "frame_result",
        "job_progress",
        "frame_result",
        "job_progress",
        "job_completed",
    ]

    # 接受 → 落库 (主实例回填源 annotation)。
    async def _noop(_c: str, _p: dict) -> None:
        return None

    await accept_tracker_job(db_session, job.id, actor_id=user.id, publisher=_noop)
    await db_session.refresh(job)
    await db_session.refresh(annotation)
    assert job.status == "accepted"
    assert annotation.annotation_type == "video_track_bbox"
    assert annotation.geometry["type"] == "video_track_bbox"
    assert [kf["frame_index"] for kf in annotation.geometry["keyframes"]] == [0, 1, 2]
    assert annotation.geometry["keyframes"][0]["source"] == "manual"
    assert annotation.geometry["keyframes"][1]["source"] == "prediction"


@pytest.mark.parametrize("fence_event", ["busy", "cancel"])
async def test_stage_fence_retries_held_results_and_preserves_cancellation(
    db_session, super_admin, monkeypatch, tracker_input_owner_fakes, fence_event
):
    from aap_assets_video_inputs.tracker import TrackerFailure
    from app.services.video_tracking import runner

    user, _ = super_admin
    task, item = await _make_video_task(db_session, user.id)
    annotation = Annotation(
        task_id=task.id,
        project_id=task.project_id,
        user_id=user.id,
        annotation_type="bbox",
        class_name="car",
        geometry={"type": "bbox", "x": 1, "y": 2, "w": 10, "h": 12},
    )
    db_session.add(annotation)
    await db_session.flush()
    job = VideoTrackerJob(
        task_id=task.id,
        dataset_item_id=item.id,
        annotation_id=annotation.id,
        created_by=user.id,
        status=VideoTrackerJobStatus.QUEUED.value,
        model_key="mock_bbox",
        direction="forward",
        from_frame=0,
        to_frame=0,
        prompt={
            "type": "bbox",
            "geometry": annotation.geometry,
            "expected_source_versions": {str(annotation.id): int(annotation.version)},
        },
        event_channel="video-tracker-job:test",
    )
    db_session.add(job)
    await db_session.commit()

    class CountingAdapter:
        def __init__(self):
            self.calls = 0

        async def propagate(self, context):
            self.calls += 1
            return tracker_fake_propagation(
                context,
                [
                    TrackerFrameResult(
                        frame_index=context.from_frame,
                        geometry={
                            "type": "bbox",
                            "x": 2.0,
                            "y": 2.0,
                            "w": 10.0,
                            "h": 12.0,
                        },
                        confidence=0.9,
                        outside=False,
                    )
                ],
            )

    adapter = CountingAdapter()
    monkeypatch.setattr(runner, "get_tracker_adapter", lambda _capability: adapter)
    stage_attempts = 0

    async def busy_then_success():
        nonlocal stage_attempts
        stage_attempts += 1
        if fence_event == "busy" and stage_attempts == 1:
            raise TrackerFailure("source_busy")
        if fence_event == "cancel":
            job.status = VideoTrackerJobStatus.CANCELLED.value
            job.cancel_requested_at = datetime.now(timezone.utc)
            await db_session.flush()
        return tracker_input_owner_fakes.acceptance

    tracker_input_owner_fakes.stage_lock_impl = busy_then_success

    async def _noop(_channel: str, _payload: dict) -> None:
        return None

    await run_tracker_job(db_session, job.id, publisher=_noop)
    await db_session.refresh(job)

    assert job.status == ("pending_review" if fence_event == "busy" else "cancelled")
    assert adapter.calls == 1
    assert stage_attempts == (2 if fence_event == "busy" else 1)
    assert job.staged_result["tracker_input_evidence"]["input_id"] == str(
        tracker_input_owner_fakes.acceptance.evidence.input_id
    )


async def test_tracker_http_error_redacts_signed_source_url_from_logs_and_job(
    db_session, super_admin, monkeypatch, caplog
):
    import logging

    from app.services.video_tracking import runner

    user, _ = super_admin
    task, item = await _make_video_task(db_session, user.id)
    job = VideoTrackerJob(
        task_id=task.id,
        dataset_item_id=item.id,
        annotation_id=None,
        target_class_name="car",
        target_tool_unit_id="bbox",
        created_by=user.id,
        status=VideoTrackerJobStatus.QUEUED.value,
        model_key="mock_bbox",
        direction="forward",
        from_frame=0,
        to_frame=0,
        prompt={},
        event_channel="video-tracker-job:test",
    )
    db_session.add(job)
    await db_session.commit()
    signed_url = "https://source.test/clip.mp4?X-Amz-Signature=tracker-secret-123"

    class FailingAdapter:
        # TrackerAdapter.propagate is awaited and must return the propagation
        # result; an async generator would be awaited as a plain coroutine.
        async def propagate(self, _context):
            request = httpx.Request("GET", signed_url)
            response = httpx.Response(502, request=request)
            raise httpx.HTTPStatusError(
                f"model fetch failed for {signed_url}",
                request=request,
                response=response,
            )

    monkeypatch.setattr(
        runner, "get_tracker_adapter", lambda _capability: FailingAdapter()
    )
    events: list[dict] = []

    async def collect(_channel: str, payload: dict) -> None:
        events.append(payload)

    monkeypatch.setattr(runner.log, "disabled", False)
    monkeypatch.setattr(runner.log, "propagate", True)
    with caplog.at_level(logging.ERROR, logger="app.services.video_tracking.runner"):
        await run_tracker_job(db_session, job.id, publisher=collect)

    await db_session.refresh(job)
    assert job.status == "failed"
    assert "[URL redacted]" in (job.error_message or "")
    assert "tracker-secret-123" not in (job.error_message or "")
    assert "tracker-secret-123" not in str(events)
    assert "tracker-secret-123" not in caplog.text
    assert "exception_type=HTTPStatusError" in caplog.text


async def test_tracker_worker_marks_unknown_model_failed(db_session, super_admin):
    user, _ = super_admin
    task, item = await _make_video_task(db_session, user.id)
    annotation = Annotation(
        task_id=task.id,
        project_id=task.project_id,
        user_id=user.id,
        annotation_type="bbox",
        class_name="car",
        geometry={"type": "bbox", "x": 1, "y": 2, "w": 10, "h": 12},
    )
    db_session.add(annotation)
    await db_session.flush()
    job = VideoTrackerJob(
        task_id=task.id,
        dataset_item_id=item.id,
        annotation_id=annotation.id,
        created_by=user.id,
        status=VideoTrackerJobStatus.QUEUED.value,
        model_key="missing_model",
        direction="forward",
        from_frame=0,
        to_frame=2,
        prompt={},
        event_channel="video-tracker-job:test",
    )
    db_session.add(job)
    await db_session.commit()

    async def collect(_channel: str, _payload: dict) -> None:
        return None

    await run_tracker_job(db_session, job.id, publisher=collect)
    await db_session.refresh(job)
    await db_session.refresh(annotation)

    assert job.status == "failed"
    assert "Unsupported tracker model" in (job.error_message or "")
    assert annotation.geometry["type"] == "bbox"


async def test_tracker_worker_records_gpu_arbiter_failure(
    db_session, super_admin, monkeypatch
):
    from app.services.video_tracking import runner as runner
    from aap_inference.gpu.contracts import (
        GPUArbiterDispatchError,
        GPUArbiterErrorCode,
    )

    user, _ = super_admin
    task, item = await _make_video_task(db_session, user.id)
    annotation = Annotation(
        task_id=task.id,
        project_id=task.project_id,
        user_id=user.id,
        annotation_type="bbox",
        class_name="car",
        geometry={"type": "bbox", "x": 1, "y": 2, "w": 10, "h": 12},
    )
    db_session.add(annotation)
    await db_session.flush()
    job = VideoTrackerJob(
        task_id=task.id,
        dataset_item_id=item.id,
        annotation_id=annotation.id,
        created_by=user.id,
        status=VideoTrackerJobStatus.QUEUED.value,
        model_key="mock_bbox",
        direction="forward",
        from_frame=0,
        to_frame=2,
        prompt={"type": "bbox", "geometry": annotation.geometry},
        event_channel="video-tracker-job:test",
    )
    db_session.add(job)
    await db_session.commit()
    events: list[dict] = []
    recorded: list[dict] = []

    async def collect(_channel: str, payload: dict) -> None:
        events.append(payload)

    def reject(_capability):
        raise GPUArbiterDispatchError(
            GPUArbiterErrorCode.CAPACITY_UNAVAILABLE,
            message="card full",
            retry_after_s=5,
        )

    monkeypatch.setattr(runner, "get_tracker_adapter", reject)

    await run_tracker_job(
        db_session,
        job.id,
        publisher=collect,
        failure_recorder=recorded.append,
    )
    await db_session.refresh(job)

    expected = {
        "error_code": "gpu_capacity_unavailable",
        "status_code": 503,
        "retry_after_s": 5,
        "message": "card full",
    }
    assert job.status == "failed"
    assert job.error_message == "card full"
    assert recorded == [expected]
    assert events[-1]["type"] == "job_failed"
    assert events[-1]["gpu_arbiter_error"] == expected


def test_apply_tracker_results_only_backfills_grid_frames():
    """采样开启 (grid_step>1) 时只回填网格帧，off-grid 预测帧丢弃。"""
    from app.services.video_tracking.runner import apply_tracker_results

    annotation = Annotation(
        annotation_type="bbox",
        class_name="car",
        geometry={"type": "bbox", "x": 1, "y": 2, "w": 10, "h": 12},
    )
    job = VideoTrackerJob(
        status=VideoTrackerJobStatus.QUEUED.value,
        model_key="sam2_video",
        direction="forward",
        from_frame=0,
        to_frame=30,
        prompt={},
        event_channel="video-tracker-job:test",
    )
    results = [
        TrackerFrameResult(
            frame_index=i,
            geometry={"type": "bbox", "x": float(i), "y": 0.0, "w": 5.0, "h": 5.0},
            confidence=0.9,
            outside=False,
        )
        for i in range(0, 31)
    ]

    apply_tracker_results(annotation, job, results, grid_step=10)

    frames = [kf["frame_index"] for kf in annotation.geometry["keyframes"]]
    # 手动 seed 帧 0 + 网格预测帧 10/20/30；1..9/11..29 等 off-grid 帧不持久化。
    assert frames == [0, 10, 20, 30]
    assert annotation.geometry["keyframes"][0]["source"] == "manual"
    assert all(
        kf["source"] == "prediction"
        for kf in annotation.geometry["keyframes"]
        if kf["frame_index"] != 0
    )


def _polygon_track_annotation() -> Annotation:
    tri = [[0.1, 0.1], [0.3, 0.1], [0.2, 0.3]]
    return Annotation(
        annotation_type="video_track_polygon",
        class_name="car",
        geometry={
            "type": "video_track_polygon",
            "track_id": "trk_poly",
            "keyframes": [
                {"frame_index": 0, "points": tri, "source": "manual", "occluded": False}
            ],
            "outside": [],
        },
    )


def _job(from_frame=0, to_frame=3) -> VideoTrackerJob:
    return VideoTrackerJob(
        status=VideoTrackerJobStatus.QUEUED.value,
        model_key="sam2_video",
        direction="forward",
        from_frame=from_frame,
        to_frame=to_frame,
        prompt={},
        event_channel="video-tracker-job:test",
    )


def test_apply_tracker_results_writes_polygon_keyframes():
    """v0.21.20 · polygon track: 保留多边形 points 关键帧 + video_track_polygon 类型。"""
    from app.services.video_tracking.runner import apply_tracker_results

    annotation = _polygon_track_annotation()
    results = [
        TrackerFrameResult(
            frame_index=i,
            geometry={
                "type": "polygon",
                "points": [[0.1 + i * 0.01, 0.1], [0.3, 0.1], [0.2, 0.3]],
            },
            confidence=1.0,
            outside=False,
        )
        for i in range(1, 4)
    ]

    apply_tracker_results(annotation, _job(), results, grid_step=1)

    assert annotation.annotation_type == "video_track_polygon"
    assert annotation.geometry["type"] == "video_track_polygon"
    kfs = annotation.geometry["keyframes"]
    frames = [kf["frame_index"] for kf in kfs]
    assert frames == [0, 1, 2, 3]
    # seed 帧手动保留; 预测帧写 points (非 bbox)。
    assert kfs[0]["source"] == "manual"
    for kf in kfs[1:]:
        assert kf["source"] == "prediction"
        assert "points" in kf and "bbox" not in kf
        assert len(kf["points"]) >= 3


def test_apply_tracker_results_degenerate_polygon_marked_outside():
    """退化多边形(顶点<3)不写坏 schema，转 outside 帧。"""
    from app.services.video_tracking.runner import apply_tracker_results

    annotation = _polygon_track_annotation()
    results = [
        TrackerFrameResult(
            frame_index=1,
            geometry={"type": "polygon", "points": [[0.1, 0.1], [0.2, 0.2]]},
            confidence=1.0,
            outside=False,
        )
    ]

    apply_tracker_results(annotation, _job(), results, grid_step=1)

    frames = [kf["frame_index"] for kf in annotation.geometry["keyframes"]]
    assert frames == [0]  # 只剩手动 seed；退化帧未落库
    outside = annotation.geometry["outside"]
    assert any(r["from"] <= 1 <= r["to"] for r in outside)


def test_bbox_from_geometry_seeds_from_polygon_vertices():
    """SAM2 只吃 bbox seed: polygon track / 单帧 polygon 结果都取顶点外接框。"""
    from aap_inference.tracker import bbox_from_geometry

    track = {
        "type": "video_track_polygon",
        "keyframes": [
            {"frame_index": 0, "points": [[0.1, 0.2], [0.5, 0.2], [0.3, 0.6]]}
        ],
    }
    seed = bbox_from_geometry(track)
    assert seed == pytest.approx({"x": 0.1, "y": 0.2, "w": 0.4, "h": 0.4})

    single = {"type": "polygon", "points": [[0.0, 0.0], [0.4, 0.0], [0.2, 0.4]]}
    assert bbox_from_geometry(single) == pytest.approx(
        {"x": 0.0, "y": 0.0, "w": 0.4, "h": 0.4}
    )


async def test_tracker_worker_preserves_partial_results_on_cancel(
    db_session, super_admin
):
    user, _ = super_admin
    task, item = await _make_video_task(db_session, user.id)
    annotation = Annotation(
        task_id=task.id,
        project_id=task.project_id,
        user_id=user.id,
        annotation_type="video_bbox",
        class_name="car",
        geometry={
            "type": "video_bbox",
            "frame_index": 0,
            "x": 1,
            "y": 2,
            "w": 10,
            "h": 12,
        },
    )
    db_session.add(annotation)
    await db_session.flush()
    job = VideoTrackerJob(
        task_id=task.id,
        dataset_item_id=item.id,
        annotation_id=annotation.id,
        created_by=user.id,
        status=VideoTrackerJobStatus.QUEUED.value,
        model_key="mock_bbox",
        direction="forward",
        from_frame=1,
        to_frame=3,
        prompt={
            "type": "bbox",
            "geometry": annotation.geometry,
            "expected_source_versions": {str(annotation.id): int(annotation.version)},
        },
        event_channel="video-tracker-job:test",
    )
    db_session.add(job)
    await db_session.commit()

    async def cancel_after_first_result(_channel: str, payload: dict) -> None:
        if payload["type"] == "frame_result":
            job.cancel_requested_at = datetime.now(timezone.utc)
            await db_session.flush()

    await run_tracker_job(db_session, job.id, publisher=cancel_after_first_result)
    await db_session.refresh(job)
    await db_session.refresh(annotation)

    # v0.21.28 · 候选流: 取消也**暂存**部分结果 (annotation 未回填), 可 accept 部分。
    assert job.status == "cancelled"
    assert annotation.annotation_type == "video_bbox"  # 未回填 (仍原始类型)
    assert job.staged_result and job.staged_result["results"]

    async def _noop(_c: str, _p: dict) -> None:
        return None

    await accept_tracker_job(db_session, job.id, actor_id=user.id, publisher=_noop)
    await db_session.refresh(annotation)
    assert [kf["frame_index"] for kf in annotation.geometry["keyframes"]] == [0, 1]
    assert annotation.geometry["keyframes"][1]["source"] == "prediction"


async def test_tracker_worker_calls_project_ml_backend_in_windows(
    db_session, super_admin, monkeypatch
):
    user, _ = super_admin
    task, item = await _make_video_task(db_session, user.id)
    project = await db_session.get(Project, task.project_id)
    backend, pool = await create_registry_with_pool(
        db_session,
        name="SAM2 Video",
        url="http://sam2-video.test",
        state="connected",
        is_interactive=True,
        enabled_pool=True,
        extra_params={},
        # v0.21.25 阶段 R · runner 按 supported_trackers 路由, backend 须声明能力。
        health_meta={"capabilities": {"supported_trackers": ["sam2_video"]}},
    )
    project.ml_backend_pool_id = pool.id
    # v0.19.0 ADR-0044 · get_project_backend 优先 project.ml_backend_pool_id 且需项目「已启用」
    db_session.add(
        ProjectMLBackendPool(project_id=task.project_id, pool_id=pool.id, enabled=True)
    )
    await db_session.flush()
    annotation = Annotation(
        task_id=task.id,
        project_id=task.project_id,
        user_id=user.id,
        annotation_type="video_bbox",
        class_name="car",
        geometry={
            "type": "video_bbox",
            "frame_index": 0,
            "x": 1,
            "y": 2,
            "w": 10,
            "h": 12,
        },
    )
    db_session.add(annotation)
    await db_session.flush()
    job = VideoTrackerJob(
        task_id=task.id,
        dataset_item_id=item.id,
        annotation_id=annotation.id,
        created_by=user.id,
        status=VideoTrackerJobStatus.QUEUED.value,
        model_key="sam2_video",
        direction="forward",
        from_frame=0,
        to_frame=4,
        prompt={
            "type": "bbox",
            "geometry": annotation.geometry,
            "expected_source_versions": {str(annotation.id): int(annotation.version)},
        },
        event_channel="video-tracker-job:test",
    )
    db_session.add(job)
    await db_session.commit()
    monkeypatch.setattr(settings, "video_tracker_window_size_frames", 2)
    monkeypatch.setattr(settings, "ml_backend_router_heartbeat_interval_seconds", 3600)
    monkeypatch.setattr(settings, "ml_backend_router_mode", "off")
    calls: list[dict] = []

    async def fake_predict_interactive(self, task_data, context):
        calls.append(context)
        return PredictionResult(
            task_id=task_data["id"],
            result=[
                {
                    "frame_index": frame_index,
                    "geometry": {"type": "bbox", "x": 1, "y": 2, "w": 10, "h": 12},
                    "confidence": 0.9,
                }
                for frame_index in range(context["from_frame"], context["to_frame"] + 1)
            ],
        )

    install_tracker_execution_fake(monkeypatch, fake_predict_interactive)
    routing = _install_tracker_routing_authority(
        monkeypatch,
        pool=pool,
        instance_id=backend.id,
        project_id=task.project_id,
    )

    async def collect(_channel: str, _payload: dict) -> None:
        return None

    await run_tracker_job(
        db_session,
        job.id,
        publisher=collect,
    )
    await db_session.refresh(job)
    await db_session.refresh(annotation)

    assert job.status == "pending_review"
    # One job-scope route is acquired for the project-bound instance and
    # finished after publication.
    assert [call["mode"] for call in routing["acquire"]] == ["off"]
    assert routing["acquire"][0]["pool_id"] == pool.id
    assert [scope.registry_id for scope, _outcome in routing["close"]] == [backend.id]
    assert [(c["from_frame"], c["to_frame"]) for c in calls] == [(0, 1), (2, 3), (4, 4)]
    assert {c["type"] for c in calls} == {"video_tracker"}
    assert {c["model_key"] for c in calls} == {"sam2_video"}

    async def _noop(_c: str, _p: dict) -> None:
        return None

    await accept_tracker_job(db_session, job.id, actor_id=user.id, publisher=_noop)
    await db_session.refresh(annotation)
    assert [kf["frame_index"] for kf in annotation.geometry["keyframes"]] == [
        0,
        1,
        2,
        3,
        4,
    ]


async def test_tracker_worker_uses_backend_context_session(
    db_session, super_admin, monkeypatch
):
    user, _ = super_admin
    task, item = await _make_video_task(db_session, user.id)
    project = await db_session.get(Project, task.project_id)
    backend, pool = await create_registry_with_pool(
        db_session,
        name="SAM2 Session",
        url="http://sam2-session.test",
        state="connected",
        is_interactive=True,
        enabled_pool=True,
        extra_params={},
        health_meta={
            "capabilities": {
                "supported_trackers": ["sam2_video"],
                "models": [
                    {
                        "id": "sam2",
                        "supported_trackers": ["sam2_video"],
                        "tracker_context_mode": "session",
                        "max_context_frames": 10,
                    }
                ],
            }
        },
    )
    project.ml_backend_pool_id = pool.id
    db_session.add(
        ProjectMLBackendPool(project_id=task.project_id, pool_id=pool.id, enabled=True)
    )
    annotation = Annotation(
        task_id=task.id,
        project_id=task.project_id,
        user_id=user.id,
        annotation_type="video_bbox",
        class_name="car",
        geometry={
            "type": "video_bbox",
            "frame_index": 0,
            "x": 0,
            "y": 0,
            "w": 1,
            "h": 1,
        },
    )
    db_session.add(annotation)
    await db_session.flush()
    job = VideoTrackerJob(
        task_id=task.id,
        dataset_item_id=item.id,
        annotation_id=annotation.id,
        created_by=user.id,
        status=VideoTrackerJobStatus.QUEUED.value,
        model_key="sam2_video",
        direction="forward",
        from_frame=0,
        to_frame=4,
        prompt={"geometry": annotation.geometry},
        event_channel="video-tracker-job:session",
    )
    db_session.add(job)
    await db_session.commit()
    monkeypatch.setattr(settings, "video_tracker_window_size_frames", 2)
    monkeypatch.setattr(settings, "ml_backend_router_heartbeat_interval_seconds", 3600)
    monkeypatch.setattr(settings, "ml_backend_router_mode", "off")
    actions: list[str] = []

    async def fake_predict_interactive(self, task_data, context):
        control = context["tracker_context"]
        actions.append(control["action"])
        if control["action"] == "close":
            return PredictionResult(
                task_id=task_data["id"],
                result=[],
                meta={"tracker_context": {"closed": True}},
            )
        return PredictionResult(
            task_id=task_data["id"],
            result=[
                {
                    "frame_index": frame,
                    "geometry": {"type": "bbox", "x": 0, "y": 0, "w": 1, "h": 1},
                }
                for frame in range(context["from_frame"], context["to_frame"] + 1)
            ],
            meta={"tracker_context": {"mode": "session", "token": "opaque"}},
        )

    install_tracker_execution_fake(monkeypatch, fake_predict_interactive)
    _install_tracker_routing_authority(
        monkeypatch,
        pool=pool,
        instance_id=backend.id,
        project_id=task.project_id,
    )
    await run_tracker_job(db_session, job.id, publisher=_noop_pub)
    await db_session.refresh(job)
    assert job.status == "pending_review"
    assert actions == ["start", "continue", "continue", "close"]
    assert "opaque" not in str(job.staged_result)


async def test_tracker_worker_marks_low_confidence_backend_results_outside(
    db_session, super_admin, monkeypatch
):
    user, _ = super_admin
    task, item = await _make_video_task(db_session, user.id)
    project = await db_session.get(Project, task.project_id)
    backend, pool = await create_registry_with_pool(
        db_session,
        name="SAM3 Video",
        url="http://sam3-video.test",
        state="connected",
        is_interactive=True,
        enabled_pool=True,
        extra_params={},
        # v0.21.25 阶段 R · runner 按 supported_trackers 路由, backend 须声明能力。
        health_meta={"capabilities": {"supported_trackers": ["sam3_video"]}},
    )
    project.ml_backend_pool_id = pool.id
    # v0.19.0 ADR-0044 · get_project_backend 优先 project.ml_backend_pool_id 且需项目「已启用」
    db_session.add(
        ProjectMLBackendPool(project_id=task.project_id, pool_id=pool.id, enabled=True)
    )
    await db_session.flush()
    annotation = Annotation(
        task_id=task.id,
        project_id=task.project_id,
        user_id=user.id,
        annotation_type="video_track_bbox",
        class_name="car",
        geometry={
            "type": "video_track_bbox",
            "track_id": "car-1",
            "keyframes": [
                {
                    "frame_index": 0,
                    "bbox": {"x": 1, "y": 2, "w": 10, "h": 12},
                    "source": "manual",
                },
                {
                    "frame_index": 1,
                    "bbox": {"x": 1, "y": 2, "w": 10, "h": 12},
                    "source": "prediction",
                },
            ],
            "outside": [],
        },
    )
    db_session.add(annotation)
    await db_session.flush()
    job = VideoTrackerJob(
        task_id=task.id,
        dataset_item_id=item.id,
        annotation_id=annotation.id,
        created_by=user.id,
        status=VideoTrackerJobStatus.QUEUED.value,
        model_key="sam3_video",
        direction="forward",
        from_frame=1,
        to_frame=2,
        prompt={
            "type": "bbox",
            "geometry": annotation.geometry,
            "expected_source_versions": {str(annotation.id): int(annotation.version)},
        },
        event_channel="video-tracker-job:test",
    )
    db_session.add(job)
    await db_session.commit()
    monkeypatch.setattr(settings, "video_tracker_low_confidence_outside_threshold", 0.5)
    monkeypatch.setattr(settings, "ml_backend_router_heartbeat_interval_seconds", 3600)
    monkeypatch.setattr(settings, "ml_backend_router_mode", "off")

    async def fake_predict_interactive(self, task_data, context):
        return PredictionResult(
            task_id=task_data["id"],
            result=[
                {
                    "frame_index": 1,
                    "geometry": {"type": "bbox", "x": 1, "y": 2, "w": 10, "h": 12},
                    "confidence": 0.2,
                },
                {
                    "frame_index": 2,
                    "geometry": {"type": "bbox", "x": 3, "y": 4, "w": 10, "h": 12},
                    "confidence": 0.8,
                },
            ],
        )

    install_tracker_execution_fake(monkeypatch, fake_predict_interactive)
    _install_tracker_routing_authority(
        monkeypatch,
        pool=pool,
        instance_id=backend.id,
        project_id=task.project_id,
    )

    async def collect(_channel: str, _payload: dict) -> None:
        return None

    await run_tracker_job(db_session, job.id, publisher=collect)
    await db_session.refresh(job)
    await db_session.refresh(annotation)

    assert job.status == "pending_review"

    async def _noop(_c: str, _p: dict) -> None:
        return None

    await accept_tracker_job(db_session, job.id, actor_id=user.id, publisher=_noop)
    await db_session.refresh(annotation)
    assert annotation.geometry["outside"] == [
        {"from": 1, "to": 1, "source": "prediction"}
    ]
    assert [kf["frame_index"] for kf in annotation.geometry["keyframes"]] == [0, 2]


class _SeedRecordingAdapter:
    """Stub adapter that records the source_geometry it receives per window
    and emits a per-frame geometry whose x equals the frame index, so the
    seed handed to the next window is identifiable.

    Frames in ``outside_frames`` are emitted as outside (no geometry update).
    """

    model_key = "mock_bbox"

    def __init__(self, outside_frames: set[int] | None = None) -> None:
        self.seeds: list[dict] = []
        self.outside_frames = outside_frames or set()

    async def propagate(self, ctx: TrackerContext):
        self.seeds.append(dict(ctx.source_geometry))
        frames = range(ctx.from_frame, ctx.to_frame + 1)
        if ctx.direction == "backward":
            frames = range(ctx.to_frame, ctx.from_frame - 1, -1)
        results = []
        for frame_index in frames:
            if frame_index in self.outside_frames:
                results.append(
                    TrackerFrameResult(
                        frame_index=frame_index,
                        geometry={},
                        confidence=0.0,
                        outside=True,
                    )
                )
                continue
            results.append(
                TrackerFrameResult(
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
                )
            )
        return tracker_fake_propagation(ctx, results)


async def _run_seed_test(db_session, super_admin, monkeypatch, *, direction, outside):
    user, _ = super_admin
    task, item = await _make_video_task(db_session, user.id)
    annotation = Annotation(
        task_id=task.id,
        project_id=task.project_id,
        user_id=user.id,
        annotation_type="bbox",
        class_name="car",
        geometry={"type": "bbox", "x": 100, "y": 0, "w": 10, "h": 10},
    )
    db_session.add(annotation)
    await db_session.flush()
    job = VideoTrackerJob(
        task_id=task.id,
        dataset_item_id=item.id,
        annotation_id=annotation.id,
        created_by=user.id,
        status=VideoTrackerJobStatus.QUEUED.value,
        model_key="mock_bbox",
        direction=direction,
        from_frame=0,
        to_frame=5,
        prompt={},
        event_channel="video-tracker-job:test",
    )
    db_session.add(job)
    await db_session.commit()
    monkeypatch.setattr(settings, "video_tracker_window_size_frames", 2)
    adapter = _SeedRecordingAdapter(outside_frames=outside)
    monkeypatch.setattr(
        "app.services.video_tracking.runner.get_tracker_adapter",
        lambda _model_key: adapter,
    )

    async def collect(_channel: str, _payload: dict) -> None:
        return None

    await run_tracker_job(db_session, job.id, publisher=collect)
    await db_session.refresh(job)
    # v0.21.28 · 候选流: seed/续窗行为在 run 期间捕获 (adapter.seeds), 完成 = 暂存待审。
    assert job.status == "pending_review"
    return adapter, annotation.geometry


async def test_tracker_seeds_next_window_from_prev_window_end_forward(
    db_session, super_admin, monkeypatch
):
    # Windows: (0,1) (2,3) (4,5). Window 1 seeds from original (x=100);
    # each later window seeds from the previous window's last frame geometry
    # (forward => to_frame, i.e. x == 1 then x == 3).
    adapter, _ = await _run_seed_test(
        db_session, super_admin, monkeypatch, direction="forward", outside=set()
    )
    assert adapter.seeds[0] == {"type": "bbox", "x": 100, "y": 0, "w": 10, "h": 10}
    assert adapter.seeds[1] == {
        "type": "bbox",
        "x": 1.0,
        "y": 0.0,
        "w": 10.0,
        "h": 10.0,
    }
    assert adapter.seeds[2] == {
        "type": "bbox",
        "x": 3.0,
        "y": 0.0,
        "w": 10.0,
        "h": 10.0,
    }


async def test_tracker_seeds_next_window_from_prev_window_end_backward(
    db_session, super_admin, monkeypatch
):
    # Windows reversed: (4,5) (2,3) (0,1). Each window propagates high->low,
    # so the last yielded frame is from_frame (the temporally-earlier
    # boundary adjacent to the next window): seed x == 4 then x == 2.
    adapter, _ = await _run_seed_test(
        db_session, super_admin, monkeypatch, direction="backward", outside=set()
    )
    assert adapter.seeds[0] == {"type": "bbox", "x": 100, "y": 0, "w": 10, "h": 10}
    assert adapter.seeds[1] == {
        "type": "bbox",
        "x": 4.0,
        "y": 0.0,
        "w": 10.0,
        "h": 10.0,
    }
    assert adapter.seeds[2] == {
        "type": "bbox",
        "x": 2.0,
        "y": 0.0,
        "w": 10.0,
        "h": 10.0,
    }


async def test_tracker_seed_falls_back_to_last_valid_when_window_all_outside(
    db_session, super_admin, monkeypatch
):
    # Forward windows (0,1) (2,3) (4,5); make window 2 (frames 2,3) all
    # outside. Window 3 must reuse window 1's last valid geometry (x == 1),
    # not an empty seed.
    adapter, _ = await _run_seed_test(
        db_session,
        super_admin,
        monkeypatch,
        direction="forward",
        outside={2, 3},
    )
    assert adapter.seeds[1] == {
        "type": "bbox",
        "x": 1.0,
        "y": 0.0,
        "w": 10.0,
        "h": 10.0,
    }
    # window 2 produced no valid geometry, so window 3 seed stays at x == 1
    assert adapter.seeds[2] == {
        "type": "bbox",
        "x": 1.0,
        "y": 0.0,
        "w": 10.0,
        "h": 10.0,
    }


# ── v0.10.49 · worker 按专表最终状态同步 async_jobs（修双写漂移）─────────────


async def _run_worker_with_final_status(
    db_session,
    super_admin,
    monkeypatch,
    status,
    *,
    gpu_arbiter_error=None,
    defer_cleanup=False,
):
    """seed 专表 job + stub run_tracker_job 返回指定终态，跑 worker 包装层，
    返回它创建/同步的 async_job。验证 cancelled/failed 不被误标 completed。"""
    from sqlalchemy import select

    from app.db.models.async_job import AsyncJob
    from app.workers import video_tracker as worker_mod

    user, _ = super_admin
    task, item = await _make_video_task(db_session, user.id)
    annotation = Annotation(
        task_id=task.id,
        project_id=task.project_id,
        user_id=user.id,
        annotation_type="video_bbox",
        class_name="car",
        geometry={
            "type": "video_bbox",
            "frame_index": 0,
            "x": 1,
            "y": 2,
            "w": 10,
            "h": 12,
        },
    )
    db_session.add(annotation)
    await db_session.flush()
    job = VideoTrackerJob(
        task_id=task.id,
        dataset_item_id=item.id,
        annotation_id=annotation.id,
        created_by=user.id,
        status=VideoTrackerJobStatus.QUEUED.value,
        model_key="mock_bbox",
        direction="forward",
        from_frame=1,
        to_frame=3,
        prompt={"type": "bbox", "geometry": annotation.geometry},
        event_channel="video-tracker-job:test",
    )
    db_session.add(job)
    await db_session.commit()

    async def _stub_run(db, job_id, **_kw):
        assert _kw["shadow_session_factory"] is not None
        assert "dispatch_context_factory" not in _kw
        row = await db.get(VideoTrackerJob, job_id)
        row.status = status
        if status == VideoTrackerJobStatus.FAILED.value:
            row.error_message = "boom"
            if gpu_arbiter_error is not None:
                _kw["failure_recorder"](gpu_arbiter_error)
        await db.commit()
        if defer_cleanup:
            from app.services.video_tracking.execution import TrackerExecutionDeferred

            raise TrackerExecutionDeferred("scope_retirement_pending")
        return row

    monkeypatch.setattr(worker_mod, "run_tracker_job", _stub_run)

    # 让 worker 内部 create_async_engine/async_sessionmaker 复用 db_session
    class _Engine:
        async def dispose(self):
            pass

    class _Factory:
        def __init__(self, *_a, **_kw):
            pass

        def __call__(self):
            class _Ctx:
                async def __aenter__(self_i):
                    return db_session

                async def __aexit__(self_i, *a):
                    return False

            return _Ctx()

    # worker 在模块顶层 import，故 patch worker_mod 上的名字（非 sqlalchemy 模块）
    monkeypatch.setattr(worker_mod, "create_async_engine", lambda *a, **k: _Engine())
    monkeypatch.setattr(worker_mod, "async_sessionmaker", _Factory)

    job_id = job.id
    if defer_cleanup:
        from app.services.video_tracking.execution import TrackerExecutionDeferred

        with pytest.raises(TrackerExecutionDeferred, match="scope_retirement_pending"):
            await worker_mod._run_video_tracker_job(str(job_id), "celery-vt")
    else:
        await worker_mod._run_video_tracker_job(str(job_id), "celery-vt")

    aj = (
        (
            await db_session.execute(
                select(AsyncJob).where(
                    AsyncJob.kind == "video_tracker",
                    AsyncJob.payload["video_tracker_job_id"].astext == str(job_id),
                )
            )
        )
        .scalars()
        .one()
    )
    return aj


async def test_worker_syncs_async_job_cancelled(db_session, super_admin, monkeypatch):
    aj = await _run_worker_with_final_status(
        db_session, super_admin, monkeypatch, VideoTrackerJobStatus.CANCELLED.value
    )
    assert aj.status == "cancelled"  # 不再被误标 completed


async def test_worker_projects_success_while_physical_cleanup_is_retried(
    db_session, super_admin, monkeypatch
):
    summary = await _run_worker_with_final_status(
        db_session, super_admin, monkeypatch, "pending_review", defer_cleanup=True
    )
    assert summary.status == "completed"


async def test_worker_syncs_async_job_failed(db_session, super_admin, monkeypatch):
    aj = await _run_worker_with_final_status(
        db_session, super_admin, monkeypatch, VideoTrackerJobStatus.FAILED.value
    )
    assert aj.status == "failed"
    assert aj.error_message == "boom"
    assert aj.result == {}


async def test_worker_syncs_gpu_arbiter_failure_into_async_job_result(
    db_session, super_admin, monkeypatch
):
    failure = {
        "error_code": "gpu_capacity_unavailable",
        "status_code": 503,
        "retry_after_s": 5,
        "message": "card full",
    }
    aj = await _run_worker_with_final_status(
        db_session,
        super_admin,
        monkeypatch,
        VideoTrackerJobStatus.FAILED.value,
        gpu_arbiter_error=failure,
    )

    assert aj.status == "failed"
    assert aj.result == {"gpu_arbiter_error": failure}


async def test_worker_syncs_async_job_completed(db_session, super_admin, monkeypatch):
    aj = await _run_worker_with_final_status(
        db_session, super_admin, monkeypatch, VideoTrackerJobStatus.COMPLETED.value
    )
    assert aj.status == "completed"


async def test_materialize_tracker_mask_result_stores_rle_and_adds_aabb(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.services import raster_mask_storage as storage
    from app.services.video_tracking.runner import _materialize_tracker_mask_result

    rle = {"encoding": "coco_rle", "size": [2, 3], "counts": [2, 2, 2]}
    reference = storage.build_rle_reference(rle)
    events = []

    async def commit():
        events.append("commit_grace")

    db = SimpleNamespace(commit=AsyncMock(side_effect=commit))
    job_id = uuid.uuid4()
    task_id = uuid.uuid4()

    async def retain_root(actual_db, *, job_id, task_id, object_key):
        assert actual_db is db
        assert job_id == expected_job_id
        assert task_id == expected_task_id
        assert object_key == reference["object_key"]
        events.append("running_root")

    expected_job_id, expected_task_id = job_id, task_id
    monkeypatch.setattr(
        "app.services.video_tracking.runner.read_mask_write_epochs",
        AsyncMock(return_value={reference["object_key"]: 0}),
    )
    monkeypatch.setattr(
        "app.services.video_tracking.runner.retain_tracker_output_mask", retain_root
    )

    async def lock_content(actual_db, value, *, verify):
        assert actual_db is db
        assert value == reference
        assert verify is False
        events.append("content_write_lock")

    monkeypatch.setattr(storage, "lock_raster_mask_references", lock_content)

    async def _fake_store(rle):
        events.append("storage_put")
        return reference

    monkeypatch.setattr(
        "app.services.video_tracking.runner.store_coco_rle", _fake_store
    )
    result = await _materialize_tracker_mask_result(
        TrackerFrameResult(
            frame_index=3,
            geometry={
                "type": "mask",
                "rle": rle,
            },
        ),
        db=db,
        job_id=job_id,
        task_id=task_id,
    )
    # The immutable frame result exports its JSON content; compare that durable
    # geometry (not the frozen container types) with the stored reference.
    exported = thaw_json(result.geometry)
    assert exported["mask"] == reference
    assert exported["bbox"] == {"x": 1 / 3, "y": 0, "w": 1 / 3, "h": 1}
    assert events == [
        "content_write_lock",
        "storage_put",
        "running_root",
        "commit_grace",
    ]


@pytest.mark.parametrize("cancelled", [False, True])
async def test_materialize_tracker_mask_keeps_raw_result_during_fence(
    monkeypatch, cancelled
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.services import raster_mask_storage as storage
    from app.services.video_tracking import runner

    rle = {"encoding": "coco_rle", "size": [1, 1], "counts": [0, 1]}
    reference = storage.build_rle_reference(rle)
    attempts = []

    async def store(actual_db, actual_rle, *, writer):
        assert actual_db is db
        attempts.append(actual_rle)
        if len(attempts) <= 5:
            raise storage.RasterMaskContractError(
                status_code=409,
                reason="mask_content_deleting",
                message="Collection is pending",
                retryable=True,
            )
        return reference

    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    running = AsyncMock(return_value=not cancelled)
    monkeypatch.setattr(runner, "store_generated_mask_content", store)
    monkeypatch.setattr(
        runner,
        "read_mask_write_epochs",
        AsyncMock(return_value={reference["object_key"]: 0}),
    )
    monkeypatch.setattr(
        runner, "retain_tracker_output_mask", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(runner.asyncio, "sleep", AsyncMock())
    held = TrackerFrameResult(frame_index=3, geometry={"type": "mask", "rle": rle})
    if cancelled:
        with pytest.raises(runner.TrackerMaskWriteCancelled):
            await runner._materialize_tracker_mask_result(
                held,
                db=db,
                job_id=uuid.uuid4(),
                task_id=uuid.uuid4(),
                still_running=running,
            )
        assert len(attempts) == 1
        db.commit.assert_not_awaited()
        db.rollback.assert_awaited_once()
    else:
        result = await runner._materialize_tracker_mask_result(
            held,
            db=db,
            job_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            still_running=running,
        )
        assert len(attempts) == 6
        assert db.rollback.await_count == 5
        db.commit.assert_awaited_once()
        assert thaw_json(result.geometry)["mask"] == reference
    # The held raw model result still carries the inferred RLE it must retry.
    assert thaw_json(held.geometry)["rle"] == rle


def test_apply_tracker_results_converts_source_to_mask_track():
    from app.services.video_tracking.runner import apply_tracker_results

    annotation = Annotation(
        id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        annotation_type="video_track_bbox",
        tool_unit_id="bbox",
        class_name="car",
        track_id="track-1",
        geometry={
            "type": "video_track_bbox",
            "track_id": "track-1",
            "keyframes": [
                {
                    "frame_index": 0,
                    "bbox": {"x": 0, "y": 0, "w": 1, "h": 1},
                    "source": "manual",
                }
            ],
            "outside": [],
        },
    )
    reference = {
        "encoding": "coco_rle_ref",
        "size": [2, 3],
        "object_key": "raster-masks/sha256/aa/aa/" + "a" * 64 + ".json",
        "sha256": "a" * 64,
        "runs": 3,
        "bytes": 58,
    }
    apply_tracker_results(
        annotation,
        _job(),
        [
            TrackerFrameResult(
                frame_index=0,
                geometry={
                    "type": "mask",
                    "mask": reference,
                    "bbox": {"x": 1 / 3, "y": 0, "w": 1 / 3, "h": 1},
                },
            )
        ],
    )
    assert annotation.annotation_type == "video_track_mask"
    assert annotation.tool_unit_id == "region"
    assert annotation.geometry["keyframes"] == [
        {"frame_index": 0, "mask": reference, "source": "prediction", "occluded": False}
    ]


def test_apply_tracker_results_preserves_mask_type_when_all_results_are_outside():
    from app.services.video_tracking.runner import apply_tracker_results

    reference = {
        "encoding": "coco_rle_ref",
        "size": [2, 3],
        "object_key": "raster-masks/sha256/aa/aa/" + "a" * 64 + ".json",
        "sha256": "a" * 64,
        "runs": 3,
        "bytes": 58,
    }
    annotation = Annotation(
        annotation_type="video_track_mask",
        tool_unit_id="region",
        class_name="car",
        geometry={
            "type": "video_track_mask",
            "track_id": "track-1",
            "keyframes": [{"frame_index": 0, "mask": reference, "source": "manual"}],
            "outside": [],
        },
    )
    apply_tracker_results(
        annotation,
        _job(),
        [TrackerFrameResult(frame_index=1, geometry={}, outside=True)],
        output_geometry="mask",
    )

    assert annotation.annotation_type == "video_track_mask"
    assert annotation.geometry["type"] == "video_track_mask"
    assert annotation.geometry["keyframes"][0]["mask"] == reference
    assert annotation.geometry["outside"] == [
        {"from": 1, "to": 1, "source": "prediction"}
    ]


async def test_accept_mask_candidate_validates_source_dimensions_before_commit(
    db_session, super_admin
):
    user, _ = super_admin
    task, item = await _make_video_task(db_session, user.id)
    annotation = Annotation(
        task_id=task.id,
        project_id=task.project_id,
        user_id=user.id,
        annotation_type="video_track_bbox",
        tool_unit_id="bbox",
        class_name="car",
        geometry={
            "type": "video_track_bbox",
            "track_id": "track-1",
            "keyframes": [
                {
                    "frame_index": 0,
                    "bbox": {"x": 0, "y": 0, "w": 1, "h": 1},
                    "source": "manual",
                }
            ],
        },
    )
    db_session.add(annotation)
    await db_session.flush()
    bad_ref = {
        "encoding": "coco_rle_ref",
        "size": [9, 9],
        "object_key": "raster-masks/sha256/aa/aa/" + "a" * 64 + ".json",
        "sha256": "a" * 64,
        "runs": 3,
        "bytes": 58,
    }
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
        prompt={
            "output_geometry": "mask",
            "expected_source_versions": {str(annotation.id): int(annotation.version)},
        },
        event_channel="video-tracker-job:test",
        staged_result={
            "grid_step": 1,
            "output_geometry": "mask",
            "results": [
                {
                    "frame_index": 1,
                    "geometry": {
                        "type": "mask",
                        "mask": bad_ref,
                        "bbox": {"x": 0, "y": 0, "w": 1, "h": 1},
                    },
                    "outside": False,
                }
            ],
        },
    )
    db_session.add(job)
    await db_session.flush()
    seed_staged_tracker_input(job, task, {"1": annotation.id})
    await db_session.commit()

    async def _noop(_channel: str, _payload: dict) -> None:
        return None

    with pytest.raises(ValueError, match="mask size must match source video"):
        await accept_tracker_job(db_session, job.id, actor_id=user.id, publisher=_noop)
    await db_session.refresh(annotation)
    await db_session.refresh(job)
    assert annotation.geometry["type"] == "video_track_bbox"
    assert job.status == VideoTrackerJobStatus.PENDING_REVIEW.value


# ── v0.23.3 ADR-0050 §11 · tracker job-scope pin + would-select evidence ──────


@pytest.mark.asyncio
async def test_tracker_observe_mode_records_would_select_evidence(
    db_session, super_admin, monkeypatch
):
    """observe: tracker job runs against legacy instance (unchanged); would-select
    evidence is recorded in logs (non-gating). Verifies the router.acquire path is
    exercised for tracker without changing dispatch."""
    user, _ = super_admin
    task, item = await _make_video_task(db_session, user.id)
    project = await db_session.get(Project, task.project_id)
    backend, pool = await create_registry_with_pool(
        db_session,
        name="SAM2 Video",
        url="http://sam2-obs.test",
        state="connected",
        is_interactive=True,
        enabled_pool=True,
        health_meta={"capabilities": {"supported_trackers": ["sam2_video"]}},
    )
    project.ml_backend_pool_id = pool.id
    db_session.add(
        ProjectMLBackendPool(project_id=task.project_id, pool_id=pool.id, enabled=True)
    )
    await db_session.flush()
    annotation = Annotation(
        task_id=task.id,
        project_id=task.project_id,
        user_id=user.id,
        annotation_type="video_bbox",
        class_name="car",
        geometry={
            "type": "video_bbox",
            "frame_index": 0,
            "x": 1,
            "y": 2,
            "w": 10,
            "h": 12,
        },
    )
    db_session.add(annotation)
    await db_session.flush()
    job = VideoTrackerJob(
        task_id=task.id,
        dataset_item_id=item.id,
        annotation_id=annotation.id,
        created_by=user.id,
        status=VideoTrackerJobStatus.QUEUED.value,
        model_key="sam2_video",
        direction="forward",
        from_frame=0,
        to_frame=2,
        prompt={"type": "bbox", "geometry": annotation.geometry},
        event_channel="video-tracker-job:obs",
    )
    db_session.add(job)
    await db_session.commit()
    monkeypatch.setattr(settings, "video_tracker_window_size_frames", 2)
    monkeypatch.setattr(settings, "ml_backend_router_heartbeat_interval_seconds", 3600)
    monkeypatch.setattr(settings, "ml_backend_router_mode", "observe")

    dispatched: list[str] = []

    async def fake_predict_interactive(self, task_data, context):
        dispatched.append(str(self.backend_id))
        return PredictionResult(
            task_id=task_data["id"],
            result=[
                {
                    "frame_index": fi,
                    "geometry": {"type": "bbox", "x": 1, "y": 2, "w": 10, "h": 12},
                    "confidence": 0.9,
                }
                for fi in range(context["from_frame"], context["to_frame"] + 1)
            ],
        )

    install_tracker_execution_fake(monkeypatch, fake_predict_interactive)
    monkeypatch.setattr(
        "app.services.ml_routing.router.make_ledger_from_settings",
        lambda: _TrackerRoutingLedger(),
    )
    routing = _install_tracker_routing_authority(
        monkeypatch,
        pool=pool,
        instance_id=backend.id,
        project_id=task.project_id,
    )

    await run_tracker_job(db_session, job.id, publisher=_noop_pub)
    await db_session.refresh(job)
    # observe: the route is acquired for evidence but never gates dispatch, so the
    # job completes against the legacy capability-selected instance.
    assert job.status == VideoTrackerJobStatus.PENDING_REVIEW.value
    assert [call["mode"] for call in routing["acquire"]] == ["observe"]
    assert [call["pool_id"] for call in routing["acquire"]] == [pool.id]
    assert set(dispatched) == {str(backend.id)}


@pytest.mark.asyncio
async def test_tracker_enforce_mode_acquires_and_finishes_route_lease(
    db_session, super_admin, monkeypatch
):
    """enforce: tracker job acquires a job-scope route lease, pins the instance,
    and finishes the lease on success (no lease leak)."""
    user, _ = super_admin
    task, item = await _make_video_task(db_session, user.id)
    project = await db_session.get(Project, task.project_id)
    backend, pool = await create_registry_with_pool(
        db_session,
        name="SAM2 Video Enf",
        url="http://sam2-enf.test",
        state="connected",
        is_interactive=True,
        health_meta={"capabilities": {"supported_trackers": ["sam2_video"]}},
        last_checked_at=datetime.now(timezone.utc),
    )
    project.ml_backend_pool_id = pool.id
    pool.enabled = True  # pool must be enabled for enforce acquire
    db_session.add(
        ProjectMLBackendPool(project_id=task.project_id, pool_id=pool.id, enabled=True)
    )
    await db_session.flush()
    annotation = Annotation(
        task_id=task.id,
        project_id=task.project_id,
        user_id=user.id,
        annotation_type="video_bbox",
        class_name="car",
        geometry={
            "type": "video_bbox",
            "frame_index": 0,
            "x": 1,
            "y": 2,
            "w": 10,
            "h": 12,
        },
    )
    db_session.add(annotation)
    await db_session.flush()
    job = VideoTrackerJob(
        task_id=task.id,
        dataset_item_id=item.id,
        annotation_id=annotation.id,
        created_by=user.id,
        status=VideoTrackerJobStatus.QUEUED.value,
        model_key="sam2_video",
        direction="forward",
        from_frame=0,
        to_frame=2,
        prompt={"type": "bbox", "geometry": annotation.geometry},
        event_channel="video-tracker-job:enf",
    )
    db_session.add(job)
    await db_session.commit()
    monkeypatch.setattr(settings, "video_tracker_window_size_frames", 2)
    monkeypatch.setattr(settings, "ml_backend_router_heartbeat_interval_seconds", 3600)
    monkeypatch.setattr(settings, "ml_backend_router_mode", "enforce")

    async def fake_predict_interactive(self, task_data, context):
        return PredictionResult(
            task_id=task_data["id"],
            result=[
                {
                    "frame_index": fi,
                    "geometry": {"type": "bbox", "x": 1, "y": 2, "w": 10, "h": 12},
                    "confidence": 0.9,
                }
                for fi in range(context["from_frame"], context["to_frame"] + 1)
            ],
        )

    install_tracker_execution_fake(monkeypatch, fake_predict_interactive)
    monkeypatch.setattr(
        "app.services.ml_routing.router.make_ledger_from_settings",
        lambda: _TrackerRoutingLedger(),
    )
    routing = _install_tracker_routing_authority(
        monkeypatch,
        pool=pool,
        instance_id=backend.id,
        project_id=task.project_id,
    )

    await run_tracker_job(db_session, job.id, publisher=_noop_pub)
    await db_session.refresh(job)
    # enforce: job completes; one job-scope lease was reserved and finished
    # (successful close, no lease leak) for the pinned instance.
    assert job.status == VideoTrackerJobStatus.PENDING_REVIEW.value
    assert [call["mode"] for call in routing["acquire"]] == ["enforce"]
    assert routing["acquire"][0]["owner"] == f"tracker:{job.id}"
    assert routing["acquire"][0]["pool_id"] == pool.id
    assert [(scope.registry_id, outcome) for scope, outcome in routing["close"]] == [
        (backend.id, "success")
    ]


async def _make_multi_member_tracker_job(db_session, user_id):
    from app.db.models.ml_backend_pool import MLBackendPoolMember
    from app.db.models.ml_backend_registry import MLBackendRegistry
    from aap_inference.pool_capability import (
        canonicalize_capability,
        capability_fingerprint,
    )

    task, item = await _make_video_task(db_session, user_id)
    project = await db_session.get(Project, task.project_id)
    capabilities = {
        "supported_trackers": ["sam2_video"],
        "models": [
            {
                "id": "sam2",
                "task": "video_segment",
                "modalities": ["video"],
                "supported_trackers": ["sam2_video"],
            }
        ],
    }
    legacy, pool = await create_registry_with_pool(
        db_session,
        name="tracker-legacy",
        url="http://tracker-legacy.test",
        state="connected",
        is_interactive=True,
        enabled_pool=True,
        health_meta={"capabilities": capabilities},
        last_checked_at=datetime.now(timezone.utc),
    )
    snapshot = canonicalize_capability(capabilities)
    pool.capability_snapshot = snapshot
    pool.capability_fingerprint = capability_fingerprint(snapshot)
    selected = MLBackendRegistry(
        name="tracker-selected",
        url="http://tracker-selected.test",
        state="connected",
        is_interactive=True,
        source="manual",
        health_meta={"capabilities": capabilities},
        last_checked_at=datetime.now(timezone.utc),
    )
    db_session.add(selected)
    await db_session.flush()
    db_session.add(
        MLBackendPoolMember(
            pool_id=pool.id,
            registry_id=selected.id,
            traffic_state="active",
            weight=1,
        )
    )
    project.ml_backend_pool_id = pool.id
    db_session.add(
        ProjectMLBackendPool(project_id=task.project_id, pool_id=pool.id, enabled=True)
    )
    annotation = Annotation(
        task_id=task.id,
        project_id=task.project_id,
        user_id=user_id,
        annotation_type="video_bbox",
        class_name="car",
        geometry={
            "type": "video_bbox",
            "frame_index": 0,
            "x": 1,
            "y": 2,
            "w": 10,
            "h": 12,
        },
    )
    db_session.add(annotation)
    await db_session.flush()
    job = VideoTrackerJob(
        task_id=task.id,
        dataset_item_id=item.id,
        annotation_id=annotation.id,
        created_by=user_id,
        status=VideoTrackerJobStatus.QUEUED.value,
        model_key="sam2_video",
        direction="forward",
        from_frame=0,
        to_frame=1,
        prompt={"type": "bbox", "geometry": annotation.geometry},
        event_channel="video-tracker-job:selected",
    )
    db_session.add(job)
    await db_session.commit()
    return job, legacy, selected, pool


@pytest.mark.asyncio
async def test_tracker_enforce_dispatches_to_router_selected_instance(
    db_session, super_admin, monkeypatch
):
    from app.services.ml_routing.authority import RoutingAuthority
    from app.services.ml_routing.contracts import (
        RouteLease,
        RouteOutcome,
        RouteSelection,
    )
    from app.services.ml_routing.router import MLBackendRouter

    class FakeLedger:
        async def aclose(self) -> None:
            return None

    user, _ = super_admin
    job, legacy, selected, pool = await _make_multi_member_tracker_job(
        db_session, user.id
    )
    legacy_id, selected_id = legacy.id, selected.id
    scope = _tracker_routing_scope(
        pool=pool,
        instance_id=selected.id,
        project_id=None,
        mode="enforce",
        owner=f"tracker:{job.id}",
        operation="tracker",
    )
    lease = RouteLease(
        lease_id=str(scope.id),
        pool_id=pool.id,
        instance_id=selected.id,
        owner=f"tracker:{job.id}",
        operation="tracker",
        generation=pool.routing_generation,
        expires_at_ms=scope.heartbeat_deadline_ms,
        scope=scope,
    )
    finished: list[tuple[uuid.UUID, RouteOutcome]] = []
    cancelled: list[uuid.UUID] = []

    async def fake_acquire(_self, _pool_id, **_kwargs):
        return RouteSelection(
            lease=lease,
            instance_id=selected.id,
            rejection=None,
        )

    async def fake_finish(_self, route_lease, outcome, **_kwargs):
        finished.append((route_lease.instance_id, outcome))

    async def fake_cancel(_self, route_lease):
        cancelled.append(route_lease.instance_id)

    used_backend_ids: list[str] = []

    async def fake_predict_interactive(self, task_data, context):
        used_backend_ids.append(self.backend_id)
        return PredictionResult(
            task_id=task_data["id"],
            result=[
                {
                    "frame_index": frame,
                    "geometry": {
                        "type": "bbox",
                        "x": 1,
                        "y": 2,
                        "w": 10,
                        "h": 12,
                    },
                    "confidence": 0.9,
                }
                for frame in range(context["from_frame"], context["to_frame"] + 1)
            ],
        )

    monkeypatch.setattr(settings, "ml_backend_router_mode", "enforce")
    monkeypatch.setattr(settings, "ml_backend_router_heartbeat_interval_seconds", 3600)
    monkeypatch.setattr(
        "app.services.ml_routing.router.make_ledger_from_settings", lambda: FakeLedger()
    )
    monkeypatch.setattr(MLBackendRouter, "acquire", fake_acquire)
    monkeypatch.setattr(MLBackendRouter, "finish", fake_finish)
    monkeypatch.setattr(MLBackendRouter, "cancel", fake_cancel)
    runtime_snapshots: list[tuple[uuid.UUID, ...]] = []

    async def fake_runtime_snapshot(_authority, registry_ids):
        runtime_snapshots.append(tuple(registry_ids))
        return None

    monkeypatch.setattr(RoutingAuthority, "runtime_snapshot", fake_runtime_snapshot)
    install_tracker_execution_fake(monkeypatch, fake_predict_interactive)

    await run_tracker_job(db_session, job.id, publisher=_noop_pub)
    await db_session.refresh(job)
    assert runtime_snapshots == [(selected_id,)]
    assert legacy_id != selected_id
    assert job.status == VideoTrackerJobStatus.PENDING_REVIEW.value, job.error_message
    assert [str(value) for value in used_backend_ids] == [str(selected_id)]
    assert finished == [(selected_id, RouteOutcome.SUCCESS)]
    assert cancelled == []


@pytest.mark.asyncio
async def test_tracker_runtime_identity_outage_defers_without_failing_the_job(
    db_session, super_admin, monkeypatch
):
    from unittest.mock import AsyncMock
    from aap_inference_contract.routing import RoutingUnavailable
    from app.services.ml_routing.authority import RoutingAuthority
    from app.services.video_tracking.execution import TrackerExecutionDeferred

    user, _ = super_admin
    job, _, selected, pool = await _make_multi_member_tracker_job(db_session, user.id)
    job_id = job.id
    monkeypatch.setattr(settings, "ml_backend_router_mode", "enforce")
    monkeypatch.setattr(settings, "ml_backend_router_heartbeat_interval_seconds", 3600)
    monkeypatch.setattr(
        "app.services.ml_routing.router.make_ledger_from_settings",
        lambda: _TrackerRoutingLedger(),
    )
    _install_tracker_routing_authority(
        monkeypatch,
        pool=pool,
        instance_id=selected.id,
        project_id=None,
    )
    snapshot = AsyncMock(side_effect=RoutingUnavailable())
    monkeypatch.setattr(RoutingAuthority, "runtime_snapshot", snapshot)
    with pytest.raises(TrackerExecutionDeferred, match="routing_unavailable"):
        await run_tracker_job(db_session, job_id, publisher=_noop_pub)
    await db_session.refresh(job)
    # The owner outage is retryable: no canonical failure is written and no
    # dispatch happens without the pinned runtime identity.
    assert job.status == "running" and job.error_message is None
    snapshot.assert_awaited_once()


@pytest.mark.asyncio
async def test_tracker_route_rejection_fails_without_dispatch(
    db_session, super_admin, monkeypatch
):
    from app.services.ml_routing.contracts import RejectionReason, RouteSelection
    from app.services.ml_routing.router import MLBackendRouter

    class FakeLedger:
        async def aclose(self) -> None:
            return None

    user, _ = super_admin
    job, _, _, _ = await _make_multi_member_tracker_job(db_session, user.id)
    dispatched = False

    async def fake_acquire(_self, _pool_id, **_kwargs):
        return RouteSelection(
            lease=None,
            instance_id=None,
            rejection=RejectionReason.POOL_SATURATED,
        )

    async def fake_predict_interactive(self, task_data, context):
        nonlocal dispatched
        dispatched = True
        return PredictionResult(task_id=task_data["id"], result=[])

    monkeypatch.setattr(settings, "ml_backend_router_mode", "enforce")
    monkeypatch.setattr(
        "app.services.ml_routing.router.make_ledger_from_settings", lambda: FakeLedger()
    )
    monkeypatch.setattr(MLBackendRouter, "acquire", fake_acquire)
    install_tracker_execution_fake(monkeypatch, fake_predict_interactive)

    await run_tracker_job(db_session, job.id, publisher=_noop_pub)
    await db_session.refresh(job)
    assert dispatched is False
    assert job.status == VideoTrackerJobStatus.FAILED.value
    assert "ml_backend_pool_saturated" in job.error_message


async def _noop_pub(_channel: str, _payload: dict) -> None:
    return None


def test_context_spans_group_same_direction_windows() -> None:
    from app.services.video_tracking.runner import _context_spans

    windows = [
        (0, 3, "forward", True),
        (4, 7, "forward", False),
        (8, 11, "forward", False),
        (0, 3, "backward", True),
    ]
    assert _context_spans(windows, 8) == {
        0: (0, 7),
        1: (0, 7),
        2: (8, 11),
        3: (0, 3),
    }


async def test_mask_write_cancellation_retains_completed_frames_in_current_window(
    db_session, super_admin, monkeypatch
):
    from app.services.video_tracking import runner

    user, _ = super_admin
    task, item = await _make_video_task(db_session, user.id)
    job = VideoTrackerJob(
        task_id=task.id,
        dataset_item_id=item.id,
        created_by=user.id,
        target_class_name="car",
        target_tool_unit_id="bbox",
        status="queued",
        model_key="mock_bbox",
        direction="forward",
        from_frame=0,
        to_frame=1,
        prompt={},
        event_channel="video-tracker-job:cancel-mask-write",
    )
    db_session.add(job)
    await db_session.commit()
    job_id = job.id
    original = runner._materialize_tracker_mask_result

    async def cancel_during_second_write(result, **kwargs):
        if result.frame_index == 1:
            current = await db_session.get(VideoTrackerJob, job_id)
            current.status = "cancelled"
            current.cancel_requested_at = datetime.now(timezone.utc)
            await db_session.flush()
            raise runner.TrackerMaskWriteCancelled()
        return await original(result, **kwargs)

    monkeypatch.setattr(
        runner, "_materialize_tracker_mask_result", cancel_during_second_write
    )

    async def silent(*_args):
        pass

    result = await run_tracker_job(db_session, job_id, publisher=silent)
    assert result.status == "cancelled"
    assert [frame["frame_index"] for frame in result.staged_result["results"]] == [0]
