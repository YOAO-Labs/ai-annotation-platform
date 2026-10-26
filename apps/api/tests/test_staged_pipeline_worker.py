"""Portable multistage prediction pipeline behavior tests.

Test-only adapters exercise the shared execution pipeline through its frozen port.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
import uuid

import pytest
from PIL import Image

from aap_inference.execution_pipeline import (
    compute_stage_depths,
    execute_task_pipeline,
    resolve_input_mode,
    resolve_pad,
    stage_totals_snapshot,
)
from aap_inference_contract.execution import (
    ExecutionProvenance,
    FrozenModelOperation,
    ModelExecutionObservation,
    ModelOperationResult,
    ModelTaskResult,
    thaw_json,
)
from aap_inference.gpu.contracts import (
    GPUArbiterDispatchError,
    GPUArbiterErrorCode,
)
from app.services import prediction_context


@dataclass
class _Result:
    task_id: str
    result: list
    score: float = 0.9
    model_version: str | None = "v1"
    inference_time_ms: int | None = 5
    meta: dict | None = None


@dataclass
class _FakeClient:
    """按调用顺序吐 canned 响应; 记录收到的 inputs 供断言。"""

    responses: list
    calls: list = field(default_factory=list)

    async def predict(self, inputs, context=None):
        self.calls.append(inputs)
        return self.responses.pop(0)


@dataclass
class _Task:
    id: str = "task-1"
    file_path: str = "key/img.jpg"
    dataset_item_id: str | None = None


class _FakeExecutionPort:
    def __init__(self, client):
        self.client = client

    async def execute(self, request: FrozenModelOperation) -> ModelOperationResult:
        inputs = [thaw_json(item) for item in request.task_inputs]
        context = None if request.context is None else thaw_json(request.context)
        raw_results = await self.client.predict(inputs, context=context)
        results = tuple(
            ModelTaskResult(
                task_id=raw.task_id,
                result=raw.result,
                score=raw.score,
                model_version=raw.model_version,
                inference_time_ms=raw.inference_time_ms,
                cache_hit=getattr(raw, "cache_hit", None),
                model_load_ms=getattr(raw, "model_load_ms", None),
                meta=raw.meta,
                mask_input_next=getattr(raw, "mask_input_next", None),
                diagnostic=getattr(raw, "diagnostic", None),
            )
            for raw in raw_results
        )
        instance_id = (
            request.required_instance_id
            or request.endpoint_registry_id
            or request.requested_registry_id
            or uuid.UUID(int=1)
        )
        return ModelOperationResult(
            request_id=request.request_id,
            operation=request.operation,
            results=results,
            provenance=ExecutionProvenance(
                requested_registry_id=request.requested_registry_id,
                requested_pool_id=request.requested_pool_id,
                registry_id=instance_id,
                pool_id=request.selected_pool_id,
                instance_id=instance_id,
                route_scope_id=uuid.UUID(int=2),
                attempt_id=request.request_id,
                topology_generation=request.topology_generation,
                member_id=request.selected_member_id,
            ),
            observation=ModelExecutionObservation(
                backend_id=str(instance_id),
                operation="predict",
                outcome="success",
                elapsed_seconds=0,
            ),
            wall_time_ms=0,
        )


async def _run_task_pipeline(
    task,
    stages,
    clients,
    stage_contexts,
    *,
    resolve_url,
    input_image_path=None,
    stage_modes=None,
    upload_crop=None,
):
    modes = tuple(stage_modes or (resolve_input_mode(stage) for stage in stages))
    source_image = None
    if any(mode == "crop" for mode in modes[1:]):
        if input_image_path is not None:
            with Image.open(input_image_path) as image:
                source_image = image.copy()
        else:
            source_image = prediction_context.load_task_image(task)

    crop_publisher = None
    if upload_crop is not None:

        class _Publisher:
            def publish(self, _task_id, box_index, jpeg_bytes):
                return upload_crop(task, box_index, jpeg_bytes)

        crop_publisher = _Publisher()

    result = await execute_task_pipeline(
        task.id,
        stages,
        [_FakeExecutionPort(client) for client in clients],
        stage_contexts,
        source_url=resolve_url(task),
        project_id=None,
        owner_prefix="test",
        route_operation="test",
        stage_modes=modes,
        source_image=source_image,
        crop_publisher=crop_publisher,
    )
    stats = (
        None
        if result.stage_stats is None
        else {stage: dict(values) for stage, values in result.stage_stats.items()}
    )
    return result.results, thaw_json(result.extra), stats


worker_tasks = SimpleNamespace(
    _run_task_pipeline=_run_task_pipeline,
    _stage_totals_snapshot=stage_totals_snapshot,
    _resolve_input_mode=resolve_input_mode,
    _resolve_pad=resolve_pad,
    _compute_stage_depths=compute_stage_depths,
)


def _bbox(x, y, w, h, cls="car"):
    return {
        "type": "rectanglelabels",
        "value": {"x": x, "y": y, "width": w, "height": h, "rectanglelabels": [cls]},
    }


@pytest.mark.asyncio
async def test_single_stage_passthrough():
    task = _Task()
    boxes = [_bbox(10, 10, 20, 20)]
    client = _FakeClient(responses=[[_Result(task.id, boxes)]])
    stages = [{"stage": 0, "parent_stage": None, "roi": None, "write": None}]

    results, extra, stats = await worker_tasks._run_task_pipeline(
        task, stages, [client], [None], resolve_url=lambda t: "http://x/img.jpg"
    )
    assert extra is None
    assert stats is None
    assert thaw_json(results[0].result) == boxes
    assert isinstance(results[0].result, tuple)
    with pytest.raises(TypeError):
        results[0].result[0]["type"] = "polygonlabels"
    # 单阶段不应触碰下游, 只调一次
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_version_bound_input_url_is_sent_to_the_root_model():
    task = _Task()
    client = _FakeClient(responses=[[_Result(task.id, [_bbox(10, 10, 20, 20)])]])
    version_url = "https://inputs.test/accepted/source?versionId=version-7"

    await worker_tasks._run_task_pipeline(
        task,
        [{"stage": 9, "parent_stage": None, "roi": None, "write": None}],
        [client],
        [None],
        resolve_url=lambda _task: version_url,
    )

    assert client.calls[0][0]["file_path"] == version_url


@pytest.mark.asyncio
async def test_roi_uses_private_input_after_root(monkeypatch, tmp_path):
    task = _Task()
    local_input = tmp_path / "accepted-source"
    Image.new("RGB", (100, 100), (18, 42, 93)).save(local_input, format="PNG")
    boxes = [_bbox(10, 10, 50, 50)]
    detect_client = _FakeClient(responses=[[_Result(task.id, boxes)]])
    classify_client = _FakeClient(
        responses=[[_Result("0", [{"score": 0.98, "attributes": {"color": "blue"}}])]]
    )

    monkeypatch.setattr(
        prediction_context,
        "load_task_image",
        lambda _task: pytest.fail("accepted-input ROI must not reread the Task URL"),
    )
    await worker_tasks._run_task_pipeline(
        task,
        [
            {"stage": 3, "parent_stage": None, "roi": None, "write": None},
            {
                "stage": 11,
                "parent_stage": 3,
                "roi": {"mode": "crop", "pad": 0},
                "write": {"target": "attributes", "keys": ["color"]},
            },
        ],
        [detect_client, classify_client],
        [None, None],
        resolve_url=lambda _task: "https://inputs.test/source?versionId=version-7",
        input_image_path=local_input,
        stage_modes=["crop", "crop"],
    )

    assert detect_client.calls[0][0]["file_path"].endswith("versionId=version-7")
    assert classify_client.calls
    assert classify_client.calls[0][0]["file_path"].startswith(
        "data:image/jpeg;base64,"
    )


@pytest.mark.asyncio
async def test_two_stage_enriches_attributes(monkeypatch):
    task = _Task()
    # 源阶段产 2 个车框
    boxes = [_bbox(10, 10, 20, 20), _bbox(50, 50, 10, 10)]
    detect_client = _FakeClient(responses=[[_Result(task.id, boxes)]])
    # 下游对每个 crop 返回带 attributes 的结果 (id 即父框下标)
    classify_client = _FakeClient(
        responses=[
            [
                _Result(
                    "0",
                    [
                        {
                            "score": 0.9,
                            "attributes": {"color": "blue", "vehicle_type": "bus"},
                        }
                    ],
                ),
                _Result(
                    "1",
                    [
                        {
                            "score": 0.8,
                            "attributes": {"color": "red", "vehicle_type": "car"},
                        }
                    ],
                ),
            ]
        ]
    )
    # 避免真去对象存储取图
    monkeypatch.setattr(
        prediction_context,
        "load_task_image",
        lambda t: Image.new("RGB", (1000, 1000), (1, 2, 3)),
    )

    stages = [
        {"stage": 0, "parent_stage": None, "roi": None, "write": None},
        {
            "stage": 1,
            "parent_stage": 0,
            "roi": {"mode": "crop", "pad": 0.05},
            "write": {"target": "attributes", "keys": ["color", "vehicle_type"]},
        },
    ]
    results, extra, stats = await worker_tasks._run_task_pipeline(
        task,
        stages,
        [detect_client, classify_client],
        [None, None],
        resolve_url=lambda t: "http://x/img.jpg",
    )

    assert stats[0]["detected"] == 2
    assert stats[1]["ok"] == 2 and stats[1]["failed"] == 0
    enriched = results[0].result
    assert enriched[0]["attributes"] == {"color": "blue", "vehicle_type": "bus"}
    assert enriched[1]["attributes"] == {"color": "red", "vehicle_type": "car"}
    # 下游收到 2 个 crop input (每个父框一个)
    assert len(classify_client.calls[0]) == 2
    assert extra["pipeline"]["stage_count"] == 2
    assert extra["pipeline"]["enriched_attr_keys"] == ["color", "vehicle_type"]


@pytest.mark.asyncio
async def test_parent_class_filter_routes_by_class(monkeypatch):
    task = _Task()
    # 源产 car + person 各一; 下游只对 car 跑
    boxes = [_bbox(10, 10, 20, 20, cls="car"), _bbox(50, 50, 10, 10, cls="person")]
    detect_client = _FakeClient(responses=[[_Result(task.id, boxes)]])
    classify_client = _FakeClient(
        responses=[[_Result("0", [{"score": 0.9, "attributes": {"color": "blue"}}])]]
    )
    monkeypatch.setattr(
        prediction_context,
        "load_task_image",
        lambda t: Image.new("RGB", (1000, 1000), (1, 2, 3)),
    )
    stages = [
        {"stage": 0, "parent_stage": None},
        {"stage": 1, "parent_stage": 0, "parent_class_filter": ["car"]},
    ]
    results, _extra, stats = await worker_tasks._run_task_pipeline(
        task,
        stages,
        [detect_client, classify_client],
        [None, None],
        resolve_url=lambda t: "http://x/img.jpg",
    )
    # 只对 car (idx0) 裁 crop; person 保持纯检测框 (无 attributes)
    assert len(classify_client.calls[0]) == 1
    assert classify_client.calls[0][0]["id"] == "0"
    assert results[0].result[0].get("attributes") == {"color": "blue"}
    assert "attributes" not in results[0].result[1] or not results[0].result[1].get(
        "attributes"
    )
    assert stats[1]["targeted"] == 1 and stats[1]["ok"] == 1


@pytest.mark.asyncio
async def test_on_failure_keep_parent_vs_drop_box(monkeypatch):
    monkeypatch.setattr(
        prediction_context,
        "load_task_image",
        lambda t: Image.new("RGB", (1000, 1000), (1, 2, 3)),
    )

    class _BoomClient:
        async def predict(self, inputs, context=None):
            raise RuntimeError("backend down")

    # keep_parent: 下游炸 → 保留父框 (属性空), task 不整体失败
    task = _Task()
    boxes = [_bbox(10, 10, 20, 20)]
    detect = _FakeClient(responses=[[_Result(task.id, boxes)]])
    stages_keep = [
        {"stage": 0, "parent_stage": None},
        {"stage": 1, "parent_stage": 0, "on_failure": "keep_parent"},
    ]
    results, _e, stats = await worker_tasks._run_task_pipeline(
        task,
        stages_keep,
        [detect, _BoomClient()],
        [None, None],
        resolve_url=lambda t: "http://x/img.jpg",
    )
    assert len(results[0].result) == 1  # 框保留
    assert stats[1]["failed"] == 1

    # drop_box: 下游炸 → 丢父框
    detect2 = _FakeClient(responses=[[_Result(task.id, [_bbox(10, 10, 20, 20)])]])
    stages_drop = [
        {"stage": 0, "parent_stage": None},
        {"stage": 1, "parent_stage": 0, "on_failure": "drop_box"},
    ]
    results2, _e2, _s2 = await worker_tasks._run_task_pipeline(
        task,
        stages_drop,
        [detect2, _BoomClient()],
        [None, None],
        resolve_url=lambda t: "http://x/img.jpg",
    )
    assert not results2[0].result


@pytest.mark.asyncio
async def test_geometry_stage_appends_polygons(monkeypatch):
    """v0.18.12 · geometry 模式: 下游收全图 URL + 父框列表, 出 polygon 追加进预测 (不裁图)。"""

    # geometry 模式纯坐标换算, 不应加载原图。
    def _boom(_t):
        raise AssertionError("geometry 模式不应加载原图")

    monkeypatch.setattr(prediction_context, "load_task_image", _boom)
    task = _Task()
    boxes = [_bbox(10, 10, 20, 20, cls="car"), _bbox(50, 50, 10, 10, cls="car")]
    detect = _FakeClient(responses=[[_Result(task.id, boxes)]])
    # box-seg 单图返回一条 result, result[] 里每 polygon 带 parent_box_idx。
    box_seg = _FakeClient(
        responses=[
            [
                _Result(
                    task.id,
                    [
                        {
                            "type": "polygonlabels",
                            "value": {
                                "points": [[1, 1], [2, 2], [3, 1]],
                                "polygonlabels": ["object"],
                            },
                            "parent_box_idx": 0,
                        },
                        {
                            "type": "polygonlabels",
                            "value": {
                                "points": [[5, 5], [6, 6], [7, 5]],
                                "polygonlabels": ["object"],
                            },
                            "parent_box_idx": 1,
                        },
                    ],
                )
            ]
        ]
    )
    stages = [
        {"stage": 0, "parent_stage": None},
        {
            "stage": 1,
            "parent_stage": 0,
            "roi": {"mode": "geometry"},
            "write": {"target": "geometry"},
        },
    ]
    results, extra, stats = await worker_tasks._run_task_pipeline(
        task,
        stages,
        [detect, box_seg],
        [None, None],
        resolve_url=lambda t: "http://x/img.jpg",
        stage_modes=["crop", "geometry"],
    )
    # 下游收全图 URL + prompts (非逐 crop)。
    sent = box_seg.calls[0]
    assert len(sent) == 1
    assert sent[0]["file_path"] == "http://x/img.jpg"
    assert [p["parent_box_idx"] for p in sent[0]["prompts"]] == [0, 1]
    # 框坐标归一化: x=10%→0.1, x2=(10+20)%→0.3。
    assert sent[0]["prompts"][0]["box"] == pytest.approx([0.1, 0.1, 0.3, 0.3])
    # 2 个 polygon 追加到原 2 框之后 (原框保留)。
    out = results[0].result
    assert len(out) == 4
    assert out[0]["type"] == "rectanglelabels" and out[1]["type"] == "rectanglelabels"
    assert out[2]["type"] == "polygonlabels" and out[3]["type"] == "polygonlabels"
    assert stats[1]["targeted"] == 2 and stats[1]["ok"] == 2 and stats[1]["failed"] == 0
    assert extra["pipeline"]["stage_count"] == 2


@pytest.mark.asyncio
async def test_geometry_stage_class_filter_and_failure(monkeypatch):
    """v0.18.12 · geometry 模式遵守 parent_class_filter; 下游炸 → 不追加 polygon, 计 failed。"""
    monkeypatch.setattr(
        prediction_context,
        "load_task_image",
        lambda t: (_ for _ in ()).throw(AssertionError()),
    )
    task = _Task()
    boxes = [_bbox(10, 10, 20, 20, cls="car"), _bbox(50, 50, 10, 10, cls="person")]
    detect = _FakeClient(responses=[[_Result(task.id, boxes)]])

    class _BoomClient:
        calls: list = []

        async def predict(self, inputs, context=None):
            type(self).calls.append(inputs)
            raise RuntimeError("box-seg down")

    boom = _BoomClient()
    stages = [
        {"stage": 0, "parent_stage": None},
        {"stage": 1, "parent_stage": 0, "parent_class_filter": ["car"]},
    ]
    results, _extra, stats = await worker_tasks._run_task_pipeline(
        task,
        stages,
        [detect, boom],
        [None, None],
        resolve_url=lambda t: "http://x/img.jpg",
        stage_modes=["crop", "geometry"],
    )
    # 只对 car 发 1 个 prompt。
    assert len(boom.calls[0][0]["prompts"]) == 1
    assert boom.calls[0][0]["prompts"][0]["parent_box_idx"] == 0
    # 下游炸 → 原 2 框保留、无 polygon 追加。
    assert len(results[0].result) == 2
    assert stats[1]["targeted"] == 1 and stats[1]["failed"] == 1


@pytest.mark.asyncio
async def test_geometry_stage_records_gpu_arbiter_failure_without_reraising():
    task = _Task()
    boxes = [_bbox(10, 10, 20, 20, cls="car")]
    detect = _FakeClient(responses=[[_Result(task.id, boxes)]])

    class _RejectedClient:
        async def predict(self, inputs, context=None):
            raise GPUArbiterDispatchError(
                GPUArbiterErrorCode.CAPACITY_UNAVAILABLE,
                message="card full",
                retry_after_s=3,
            )

    stages = [
        {"stage": 0, "parent_stage": None, "ml_backend_id": "root"},
        {
            "stage": 1,
            "parent_stage": 0,
            "ml_backend_id": "segmenter",
            "parent_class_filter": ["car"],
        },
    ]
    results, extra, stats = await worker_tasks._run_task_pipeline(
        task,
        stages,
        [detect, _RejectedClient()],
        [None, None],
        resolve_url=lambda t: "http://x/img.jpg",
        stage_modes=["crop", "geometry"],
    )

    assert thaw_json(results[0].result) == boxes
    assert stats[1]["failed"] == 1
    assert extra["pipeline"]["gpu_arbiter_failures"] == [
        {
            "error_code": "gpu_capacity_unavailable",
            "status_code": 503,
            "retry_after_s": 3,
            "message": "card full",
            "stage": 1,
            "ml_backend_id": "segmenter",
            "count": 1,
        }
    ]


@pytest.mark.asyncio
async def test_crop_stage_records_gpu_arbiter_failure_and_keeps_parent(monkeypatch):
    monkeypatch.setattr(
        prediction_context,
        "load_task_image",
        lambda task: Image.new("RGB", (1000, 1000), (1, 2, 3)),
    )
    task = _Task()
    boxes = [_bbox(10, 10, 20, 20)]
    detect = _FakeClient(responses=[[_Result(task.id, boxes)]])

    class _RejectedClient:
        async def predict(self, inputs, context=None):
            raise GPUArbiterDispatchError(
                GPUArbiterErrorCode.NOT_READY,
                message="ledger rebuilding",
            )

    stages = [
        {"stage": 0, "parent_stage": None, "ml_backend_id": "root"},
        {
            "stage": 2,
            "parent_stage": 0,
            "ml_backend_id": "classifier",
            "write": {"target": "attributes"},
            "on_failure": "keep_parent",
        },
    ]
    results, extra, stats = await worker_tasks._run_task_pipeline(
        task,
        stages,
        [detect, _RejectedClient()],
        [None, None],
        resolve_url=lambda t: "http://x/img.jpg",
        stage_modes=["crop", "crop"],
    )

    assert thaw_json(results[0].result) == boxes
    assert stats[2]["failed"] == 1
    assert extra["pipeline"]["gpu_arbiter_failures"][0] == {
        "error_code": "gpu_arbiter_not_ready",
        "status_code": 503,
        "retry_after_s": None,
        "message": "ledger rebuilding",
        "stage": 2,
        "ml_backend_id": "classifier",
        "count": 1,
    }


def test_stage_totals_snapshot_shape():
    """v0.18.6 · 逐阶段累加器 → 升序拍平 list, 与终态 result.pipeline_stages 同形态。"""
    totals = {
        1: {"targeted": 5, "ok": 3, "failed": 1, "skipped_geometry": 1},
        0: {"detected": 7},
    }
    snap = worker_tasks._stage_totals_snapshot(totals)
    assert snap == [
        {"stage": 0, "detected": 7},
        {"stage": 1, "targeted": 5, "ok": 3, "failed": 1, "skipped_geometry": 1},
    ]


@pytest.mark.asyncio
async def test_presigned_delivery_wires_upload_crop(monkeypatch):
    """v0.18.4 · upload_crop 非 None → crop 走 presigned URL 投递 (非 data URI)。"""
    monkeypatch.setattr(
        prediction_context,
        "load_task_image",
        lambda t: Image.new("RGB", (1000, 1000), (1, 2, 3)),
    )
    task = _Task()
    boxes = [_bbox(10, 10, 20, 20), _bbox(50, 50, 10, 10)]
    detect = _FakeClient(responses=[[_Result(task.id, boxes)]])
    classify = _FakeClient(
        responses=[
            [
                _Result("0", [{"score": 0.9, "attributes": {"color": "blue"}}]),
                _Result("1", [{"score": 0.8, "attributes": {"color": "red"}}]),
            ]
        ]
    )
    uploaded: list[tuple] = []

    def upload_crop(t, box_idx, jpeg_bytes):
        uploaded.append((box_idx, len(jpeg_bytes)))
        return f"http://store/{t.id}/{box_idx}.jpg"

    stages = [
        {"stage": 0, "parent_stage": None},
        {"stage": 1, "parent_stage": 0},
    ]
    await worker_tasks._run_task_pipeline(
        task,
        stages,
        [detect, classify],
        [None, None],
        resolve_url=lambda t: "http://x/img.jpg",
        upload_crop=upload_crop,
    )
    # 下游收到的是 presigned URL (非 data URI)
    sent = classify.calls[0]
    assert [c["file_path"] for c in sent] == [
        "http://store/task-1/0.jpg",
        "http://store/task-1/1.jpg",
    ]
    assert {u[0] for u in uploaded} == {0, 1}


@pytest.mark.asyncio
async def test_crop_cache_reused_across_parallel_siblings(monkeypatch):
    """v0.18.4 · 两个并行兄弟阶段 target 同一批父框 → crop 只裁/上传一次 (按 box_idx+pad 复用)。"""
    monkeypatch.setattr(
        prediction_context,
        "load_task_image",
        lambda t: Image.new("RGB", (1000, 1000), (1, 2, 3)),
    )
    task = _Task()
    boxes = [_bbox(10, 10, 20, 20), _bbox(50, 50, 10, 10)]
    detect = _FakeClient(responses=[[_Result(task.id, boxes)]])
    # 两个兄弟阶段各自对同一批框跑分类 (同 pad), 各回不同属性键
    sib_a = _FakeClient(
        responses=[
            [
                _Result("0", [{"score": 0.9, "attributes": {"color": "blue"}}]),
                _Result("1", [{"score": 0.9, "attributes": {"color": "red"}}]),
            ]
        ]
    )
    sib_b = _FakeClient(
        responses=[
            [
                _Result("0", [{"score": 0.9, "attributes": {"brand": "x"}}]),
                _Result("1", [{"score": 0.9, "attributes": {"brand": "y"}}]),
            ]
        ]
    )
    upload_count: list[int] = []

    def upload_crop(t, box_idx, jpeg_bytes):
        upload_count.append(box_idx)
        return f"http://store/{box_idx}.jpg"

    stages = [
        {"stage": 0, "parent_stage": None},
        {"stage": 1, "parent_stage": 0, "roi": {"pad": 0.05}},
        {"stage": 2, "parent_stage": 0, "roi": {"pad": 0.05}},
    ]
    await worker_tasks._run_task_pipeline(
        task,
        stages,
        [detect, sib_a, sib_b],
        [None, None, None],
        resolve_url=lambda t: "http://x/img.jpg",
        upload_crop=upload_crop,
    )
    # 2 框 × 1 次 (第二个兄弟阶段全部命中缓存), 而非 2 框 × 2 阶段 = 4 次
    assert sorted(upload_count) == [0, 1]


@pytest.mark.asyncio
async def test_skipped_geometry_counted_and_topology_in_extra(monkeypatch):
    """v0.18.4 · 旋转框计入 stats[si].skipped_geometry; extra.pipeline.stages 记拓扑。"""
    monkeypatch.setattr(
        prediction_context,
        "load_task_image",
        lambda t: Image.new("RGB", (1000, 1000), (1, 2, 3)),
    )
    task = _Task()
    boxes = [
        _bbox(10, 10, 20, 20),  # idx0: 有效 car
        {
            "type": "rectanglelabels",
            "value": {
                "x": 50,
                "y": 50,
                "width": 10,
                "height": 10,
                "rotation": 30,
                "rectanglelabels": ["car"],
            },
        },  # idx1: car 但旋转 → 命中路由后几何跳过
    ]
    detect = _FakeClient(responses=[[_Result(task.id, boxes)]])
    classify = _FakeClient(
        responses=[[_Result("0", [{"score": 0.9, "attributes": {"color": "blue"}}])]]
    )
    stages = [
        {"stage": 0, "parent_stage": None, "ml_backend_id": "be-0", "model_id": "det"},
        {
            "stage": 1,
            "parent_stage": 0,
            "ml_backend_id": "be-1",
            "model_id": "cls",
            "parent_class_filter": ["car"],
            "write": {"keys": ["color"]},
        },
    ]
    _r, extra, stats = await worker_tasks._run_task_pipeline(
        task,
        stages,
        [detect, classify],
        [None, None],
        resolve_url=lambda t: "http://x/img.jpg",
    )
    assert stats[1]["skipped_geometry"] == 1
    assert stats[1]["ok"] == 1
    topo = extra["pipeline"]["stages"]
    assert topo[1] == {
        "stage": 1,
        "parent_stage": 0,
        "depth": 2,
        "label": None,
        "ml_backend_id": "be-1",
        "model_id": "cls",
        "parent_class_filter": ["car"],
        "write_keys": ["color"],
        "write_target": None,
        "target_stage": "root",
    }
    assert extra["pipeline"]["max_depth"] == 2


@pytest.mark.asyncio
async def test_drop_box_on_one_sibling_keeps_other_sibling_boxes(monkeypatch):
    """v0.18.4 · 并行扇出: person 阶段 drop_box 失败 → 丢 person 框; car 阶段成功 → car 框保留富集。"""
    monkeypatch.setattr(
        prediction_context,
        "load_task_image",
        lambda t: Image.new("RGB", (1000, 1000), (1, 2, 3)),
    )
    task = _Task()
    boxes = [_bbox(10, 10, 20, 20, cls="car"), _bbox(50, 50, 10, 10, cls="person")]
    detect = _FakeClient(responses=[[_Result(task.id, boxes)]])
    car_cls = _FakeClient(
        responses=[[_Result("0", [{"score": 0.9, "attributes": {"color": "blue"}}])]]
    )

    class _BoomClient:
        async def predict(self, inputs, context=None):
            raise RuntimeError("person backend down")

    stages = [
        {"stage": 0, "parent_stage": None},
        {
            "stage": 1,
            "parent_stage": 0,
            "parent_class_filter": ["car"],
            "on_failure": "keep_parent",
            "write": {"keys": ["color"]},
        },
        {
            "stage": 2,
            "parent_stage": 0,
            "parent_class_filter": ["person"],
            "on_failure": "drop_box",
        },
    ]
    results, _e, stats = await worker_tasks._run_task_pipeline(
        task,
        stages,
        [detect, car_cls, _BoomClient()],
        [None, None, None],
        resolve_url=lambda t: "http://x/img.jpg",
    )
    surviving = results[0].result
    # person 框被丢, car 框保留且带 color
    assert len(surviving) == 1
    assert surviving[0]["value"]["rectanglelabels"] == ["car"]
    assert surviving[0]["attributes"] == {"color": "blue"}
    assert stats[2]["failed"] == 1


def test_resolve_input_mode_by_write_target():
    """v0.18.14 · 投递模式按 write.target 推断, input.mode 可覆盖。"""
    assert (
        worker_tasks._resolve_input_mode({"write": {"target": "attributes"}}) == "crop"
    )
    assert (
        worker_tasks._resolve_input_mode({"write": {"target": "geometry"}})
        == "geometry"
    )
    assert (
        worker_tasks._resolve_input_mode({"write": {"target": "intermediate"}})
        == "geometry"
    )
    assert worker_tasks._resolve_input_mode({}) == "crop"  # 缺省 attributes → crop
    # 显式 input.mode 覆盖 write.target 推断
    assert (
        worker_tasks._resolve_input_mode(
            {"input": {"mode": "geometry"}, "write": {"target": "attributes"}}
        )
        == "geometry"
    )


def test_resolve_pad_by_depth():
    """v0.18.14 · pad: 显式 roi.pad 优先, 缺省按深度取默认。"""
    assert worker_tasks._resolve_pad({"roi": {"pad": 0.2}}, 2) == 0.2  # 显式优先
    assert worker_tasks._resolve_pad({}, 1) == 0.05
    assert worker_tasks._resolve_pad({}, 2) == 0.08
    assert worker_tasks._resolve_pad({}, 3) == 0.12
    assert worker_tasks._resolve_pad({}, 9) == 0.05  # 未知深度回落


def test_compute_stage_depths():
    """v0.18.14 · 按 parent_stage 链派生深度 (root=1)。"""
    stages = [
        {"stage": 0, "parent_stage": None},
        {"stage": 1, "parent_stage": 0},
        {"stage": 2, "parent_stage": 1},
        {"stage": 3, "parent_stage": 0},
    ]
    assert worker_tasks._compute_stage_depths(stages) == {0: 1, 1: 2, 2: 3, 3: 2}


@pytest.mark.asyncio
async def test_depth_three_attribute_chain(monkeypatch):
    """v0.18.14 · depth-3 attribute 链: root → classify(color) → classify(label=sub, vtype)。

    两个分类阶段都裁同一批向下透传的 root 框, 属性累加到 root; 带 label 的阶段加前缀;
    max_depth=3; enriched_attr_keys 含前缀后的最终键。
    """
    monkeypatch.setattr(
        prediction_context,
        "load_task_image",
        lambda t: Image.new("RGB", (1000, 1000), (1, 2, 3)),
    )
    task = _Task()
    boxes = [_bbox(10, 10, 30, 30)]
    detect = _FakeClient(responses=[[_Result(task.id, boxes)]])
    clf_a = _FakeClient(
        responses=[[_Result("0", [{"score": 0.9, "attributes": {"color": "blue"}}])]]
    )
    clf_b = _FakeClient(
        responses=[[_Result("0", [{"score": 0.9, "attributes": {"vtype": "bus"}}])]]
    )
    stages = [
        {"stage": 0, "parent_stage": None},
        {
            "stage": 1,
            "parent_stage": 0,
            "roi": {"mode": "crop"},
            "write": {"target": "attributes", "keys": ["color"]},
        },
        {
            "stage": 2,
            "parent_stage": 1,
            "label": "sub",
            "roi": {"mode": "crop"},
            "write": {"target": "attributes", "keys": ["vtype"]},
        },
    ]
    results, extra, stats = await worker_tasks._run_task_pipeline(
        task,
        stages,
        [detect, clf_a, clf_b],
        [None, None, None],
        resolve_url=lambda t: "http://x/img.jpg",
        stage_modes=["crop", "crop", "crop"],
    )
    out = results[0].result
    assert out[0]["attributes"] == {"color": "blue", "sub_vtype": "bus"}
    assert extra["pipeline"]["max_depth"] == 3
    assert extra["pipeline"]["enriched_attr_keys"] == ["color", "sub_vtype"]
    assert stats[1]["ok"] == 1 and stats[2]["ok"] == 1


@pytest.mark.asyncio
async def test_intermediate_target_not_appended(monkeypatch):
    """v0.18.14 · write.target=intermediate: 产几何供下游消费但不落库为候选 (不追加进结果)。"""
    task = _Task()
    boxes = [_bbox(10, 10, 30, 30)]
    detect = _FakeClient(responses=[[_Result(task.id, boxes)]])
    seg = _FakeClient(
        responses=[
            [
                _Result(
                    task.id,
                    [
                        {
                            "type": "polygonlabels",
                            "parent_box_idx": 0,
                            "value": {"points": [[0, 0], [1, 1], [2, 0]]},
                        }
                    ],
                )
            ]
        ]
    )
    stages = [
        {"stage": 0, "parent_stage": None},
        {
            "stage": 1,
            "parent_stage": 0,
            "roi": {"mode": "geometry"},
            "write": {"target": "intermediate"},
        },
    ]
    results, _extra, stats = await worker_tasks._run_task_pipeline(
        task,
        stages,
        [detect, seg],
        [None, None],
        resolve_url=lambda t: "http://x/img.jpg",
        stage_modes=["crop", "geometry"],
    )
    out = results[0].result
    # intermediate → polygon 不追加进结果 (仅内部供下游消费); 原框保留。
    assert len(out) == 1
    assert stats[1]["ok"] == 1


@pytest.mark.asyncio
async def test_crop_detect_geometry_remaps_to_image(monkeypatch):
    """v0.18.15 · crop 投递 + target=geometry: 下游普通检测器在 crop 上检出, 回映回原图坐标。

    单层 person → 在 person crop 上检测 hat (普通检测器), hat 框回映回原图并落在 person 内, 追加进结果。
    """
    monkeypatch.setattr(
        prediction_context,
        "load_task_image",
        lambda t: Image.new("RGB", (1000, 1000), (1, 2, 3)),
    )
    task = _Task()
    # person 像素 (200,200)-(600,600) → transform ox=oy=0.2, sx=sy=0.4 (pad=0)
    person = [_bbox(20, 20, 40, 40, cls="person")]
    detect = _FakeClient(responses=[[_Result(task.id, person)]])
    # hat-detect 在 crop 上返回 crop-pct 坐标的 hat 框 (居中偏上)
    hat_detect = _FakeClient(
        responses=[
            [
                _Result(
                    "0",
                    [
                        {
                            "type": "rectanglelabels",
                            "value": {
                                "x": 25,
                                "y": 10,
                                "width": 50,
                                "height": 30,
                                "rectanglelabels": ["hat"],
                            },
                        }
                    ],
                )
            ]
        ]
    )
    stages = [
        {"stage": 0, "parent_stage": None},
        {
            "stage": 1,
            "parent_stage": 0,
            "roi": {"mode": "crop", "pad": 0},
            "input": {"mode": "crop"},
            "write": {"target": "geometry"},
        },
    ]
    results, extra, stats = await worker_tasks._run_task_pipeline(
        task,
        stages,
        [detect, hat_detect],
        [None, None],
        resolve_url=lambda t: "http://x/img.jpg",
        stage_modes=["crop", "crop"],
    )
    out = results[0].result
    # person 原框保留 + hat 新框追加
    assert len(out) == 2
    hat = out[1]
    assert hat["type"] == "rectanglelabels"
    assert hat["parent_box_idx"] == 0
    # 回映: x=(0.2+0.25*0.4)*100=30, y=(0.2+0.10*0.4)*100=24, w=50*0.4=20, h=30*0.4=12
    v = hat["value"]
    assert v["x"] == pytest.approx(30) and v["y"] == pytest.approx(24)
    assert v["width"] == pytest.approx(20) and v["height"] == pytest.approx(12)
    # hat 落在 person 框 (20~60) 内部
    assert 20 <= v["x"] and v["x"] + v["width"] <= 60
    assert 20 <= v["y"] and v["y"] + v["height"] <= 60
    assert stats[1]["ok"] == 1


@pytest.mark.asyncio
async def test_depth_three_geometry_then_attribute(monkeypatch):
    """v0.18.15 · 几何 depth-3: person → 检测 hat (crop-detect) → hat 的 color (crop 分类)。

    hat 框回映回原图, color 阶段裁 hat 框跑分类, 加 hat_ 前缀写回 hat 框; max_depth==3。
    """
    monkeypatch.setattr(
        prediction_context,
        "load_task_image",
        lambda t: Image.new("RGB", (1000, 1000), (1, 2, 3)),
    )
    task = _Task()
    person = [_bbox(20, 20, 40, 40, cls="person")]
    detect = _FakeClient(responses=[[_Result(task.id, person)]])
    hat_detect = _FakeClient(
        responses=[
            [
                _Result(
                    "0",
                    [
                        {
                            "type": "rectanglelabels",
                            "value": {"x": 25, "y": 10, "width": 50, "height": 30},
                        }
                    ],
                )
            ]
        ]
    )
    # color 阶段裁 hat 框 (stage_outputs[1] 的下标 0) → 返回 color
    color_clf = _FakeClient(
        responses=[[_Result("0", [{"score": 0.9, "attributes": {"color": "red"}}])]]
    )
    stages = [
        {"stage": 0, "parent_stage": None},
        {
            "stage": 1,
            "parent_stage": 0,
            "roi": {"mode": "crop", "pad": 0},
            "input": {"mode": "crop"},
            "write": {"target": "geometry"},
        },
        {
            "stage": 2,
            "parent_stage": 1,
            "label": "hat",
            "roi": {"mode": "crop", "pad": 0},
            "write": {"target": "attributes", "keys": ["color"]},
        },
    ]
    results, extra, stats = await worker_tasks._run_task_pipeline(
        task,
        stages,
        [detect, hat_detect, color_clf],
        [None, None, None],
        resolve_url=lambda t: "http://x/img.jpg",
        stage_modes=["crop", "crop", "crop"],
    )
    out = results[0].result
    assert len(out) == 2  # person + hat
    hat = out[1]
    # color 写回 hat 框, 带 hat_ 前缀
    assert hat["attributes"] == {"hat_color": "red"}
    assert extra["pipeline"]["max_depth"] == 3
    assert extra["pipeline"]["enriched_attr_keys"] == ["hat_color"]
    assert stats[1]["ok"] == 1 and stats[2]["ok"] == 1


@pytest.mark.asyncio
async def test_depth_three_drop_box_does_not_delete_root(monkeypatch):
    """issue 0001 · 深层 (父=中间阶段) 的 on_failure=drop_box 不得误删无关 root 框。

    person → crop-detect hat (geometry, 落库) → 裁 hat 框跑 color 分类 (drop_box) 但下游炸。
    drop 发生在 new_shapes 追加前, root_boxes 此刻只有 [person]; 旧代码会用 hat 在
    stage_outputs[1] 的下标 {0} 去删 root_boxes → 误删 person。兜底 gate (父!=root 不删)
    保住 person, 退化为 keep_parent。
    """
    monkeypatch.setattr(
        prediction_context,
        "load_task_image",
        lambda t: Image.new("RGB", (1000, 1000), (1, 2, 3)),
    )
    task = _Task()
    person = [_bbox(20, 20, 40, 40, cls="person")]
    detect = _FakeClient(responses=[[_Result(task.id, person)]])
    hat_detect = _FakeClient(
        responses=[
            [
                _Result(
                    "0",
                    [
                        {
                            "type": "rectanglelabels",
                            "value": {"x": 25, "y": 10, "width": 50, "height": 30},
                        }
                    ],
                )
            ]
        ]
    )

    class _BoomClient:
        async def predict(self, inputs, context=None):
            raise RuntimeError("color clf down")

    stages = [
        {"stage": 0, "parent_stage": None},
        {
            "stage": 1,
            "parent_stage": 0,
            "roi": {"mode": "crop", "pad": 0},
            "input": {"mode": "crop"},
            "write": {"target": "geometry"},
        },
        {
            "stage": 2,
            "parent_stage": 1,
            "on_failure": "drop_box",
            "roi": {"mode": "crop", "pad": 0},
            "write": {"target": "attributes", "keys": ["color"]},
        },
    ]
    results, _extra, stats = await worker_tasks._run_task_pipeline(
        task,
        stages,
        [detect, hat_detect, _BoomClient()],
        [None, None, None],
        resolve_url=lambda t: "http://x/img.jpg",
        stage_modes=["crop", "crop", "crop"],
    )
    out = results[0].result
    # person (root) + hat (stage1 落库) 都在; person 未被深层 drop_box 误删。
    assert len(out) == 2
    assert out[0]["value"]["rectanglelabels"] == ["person"]
    assert stats[2]["failed"] == 1
