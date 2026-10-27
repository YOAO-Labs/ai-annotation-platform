"""v0.20.11 · 选中框单框二次推理 service 层覆盖 (Q1b).

mock 掉图像加载 / 对象存储 / 模型执行端口, 只验编排 + 产物归位:
- attributes 型: 属性 union 回原框, attributes_meta 标 origin=ai
- geometry 型: crop 检出几何回映后建子框 (parent=选中框)
- crop 被守卫跳过 (框太小) → 无产物
"""

from __future__ import annotations

import types
import uuid

import pytest
from fastapi import HTTPException
from PIL import Image

from aap_inference.backend_transport import PredictionResult
import app.services.inference_model_execution as model_execution_mod
import app.services.prediction_context as prediction_context
import app.services.storage as storage_mod
from aap_inference_contract.execution import (
    ExecutionFailure,
    ExecutionProvenance,
    ModelExecutionError,
    ModelExecutionObservation,
    ModelOperationResult,
    ModelTaskResult,
)
from app.services.annotation import AnnotationService
from app.services.labeling_write_scope import capture_labeling_write_scope
from app.services.secondary_inference import (
    run_secondary_inference as _run_secondary_inference,
)
from tests.factory import create_project, create_task


class _FakeManagedOperation:
    """Minimal managed-image owner: prepare an unpersisted crop batch."""

    def __init__(self, task):
        self.task = task
        self.closed = False

    async def prepare_crop(self, ls_box, *, pad):
        from aap_inference.roi import crop_inputs_from_boxes

        batch = crop_inputs_from_boxes(
            Image.new("RGB", (200, 200)),
            [dict(ls_box)],
            pad=pad,
            delivery="deferred",
            min_crop_side_px=32,
        )
        for item in batch.inputs:
            item["file_path"] = f"http://fake-managed/{item['id']}"
        return batch

    async def before_dispatch(self):
        return None

    async def final(self):
        return self.task, None


async def run_secondary_inference(db, *, task, annotation, **kwargs):
    """Exercise the managed-original owner for every image-task case."""
    if (
        task is not None
        and task.file_type == "image"
        and kwargs.get("original_operation") is None
    ):
        kwargs["original_operation"] = _FakeManagedOperation(task)
    return await _run_secondary_inference(
        db, task=task, annotation=annotation, **kwargs
    )


class _FakeStorage:
    def upload_crop_bytes(self, jpeg_bytes: bytes, key: str) -> str:
        return f"http://fake-minio/{key}"


def _fake_backend(models: list[dict] | None = None):
    health_meta = {"capabilities": {"models": models}} if models is not None else None
    return types.SimpleNamespace(id=uuid.uuid4(), health_meta=health_meta)


@pytest.fixture
def _patch_io(monkeypatch):
    """mock 图像加载 + 对象存储 (crop 投递不落真实 IO)。"""
    monkeypatch.setattr(
        prediction_context, "load_task_image", lambda task: Image.new("RGB", (200, 200))
    )
    monkeypatch.setattr(storage_mod, "StorageService", _FakeStorage)


def _patch_predict(monkeypatch, results, *, seen=None, failure_factory=None):
    class _FakePort:
        async def execute(self, operation):
            if seen is not None:
                seen["context"] = operation.context
            if failure_factory is not None:
                raise failure_factory(operation)
            backend_id = operation.requested_registry_id
            result_rows = tuple(
                ModelTaskResult(
                    task_id=result.task_id,
                    result=result.result,
                    score=result.score,
                    model_version=result.model_version,
                    inference_time_ms=result.inference_time_ms,
                    meta=result.meta,
                )
                for result in results
            )
            provenance = ExecutionProvenance(
                requested_registry_id=backend_id,
                requested_pool_id=operation.requested_pool_id,
                registry_id=backend_id,
                pool_id=operation.requested_pool_id,
                instance_id=backend_id,
                route_scope_id=uuid.uuid4(),
                attempt_id=operation.request_id,
            )
            return ModelOperationResult(
                request_id=operation.request_id,
                operation=operation.operation,
                results=result_rows,
                provenance=provenance,
                observation=ModelExecutionObservation(
                    backend_id=str(backend_id),
                    operation="predict",
                    outcome="success",
                    elapsed_seconds=0.0,
                ),
                wall_time_ms=0,
            )

    monkeypatch.setattr(
        model_execution_mod,
        "model_execution_port",
        lambda *_args, **_kwargs: _FakePort(),
    )


async def _mk_box(db, task_id, user_id, **kw):
    svc = AnnotationService(db)
    return await svc.create(
        task_id=task_id,
        user_id=user_id,
        annotation_type="bbox",
        class_name=kw.get("class_name", "car"),
        geometry={"type": "bbox", "x": 0.2, "y": 0.2, "w": 0.3, "h": 0.3},
        tool_unit_id="bbox",
        attributes=kw.get("attributes"),
    )


async def _run_and_apply_secondary_result(
    db, *, task, annotation, user_id, **inference_args
):
    write_scope = capture_labeling_write_scope(
        task,
        actor_id=user_id,
        actor_known=True,
        operation="secondary_inference",
        execution_scope=None,
        execution_scope_known=True,
        job_id=None,
        job_id_known=True,
        source_annotation=annotation,
    )
    facts = await run_secondary_inference(
        db, task=task, annotation=annotation, **inference_args
    )
    return await AnnotationService(db)._apply_secondary_inference(
        task=task,
        annotation=annotation,
        result=facts,
        write_scope=write_scope,
    )


@pytest.mark.asyncio
async def test_attributes_written_back_with_ai_origin(
    db_session, super_admin, monkeypatch, _patch_io
):
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="image-det")
    task = await create_task(db_session, project_id=proj.id)
    await db_session.flush()
    box = await _mk_box(db_session, task.id, user.id)
    await db_session.flush()

    # 下游分类返回 color=blue (crop 输入 id="0" → cr.task_id="0")
    _patch_predict(
        monkeypatch,
        [
            PredictionResult(
                task_id="0",
                result=[{"attributes": {"color": "blue"}, "score": 0.9}],
            )
        ],
    )

    write_scope = capture_labeling_write_scope(
        task,
        actor_id=user.id,
        actor_known=True,
        operation="secondary_inference",
        execution_scope=None,
        execution_scope_known=True,
        job_id=None,
        job_id_known=True,
        source_annotation=box,
    )
    inference_result = await run_secondary_inference(
        db_session,
        annotation=box,
        task=task,
        backend=_fake_backend(),
        write_target="attributes",
        write_keys=None,
        label=None,
        model_id="cls",
        model_variants=None,
        params=None,
        task_type="classification",
        prompt=None,
        class_filter=None,
        pad=0.08,
    )
    assert box.attributes == {}
    updated, children = await AnnotationService(db_session)._apply_secondary_inference(
        task=task,
        annotation=box,
        result=inference_result,
        write_scope=write_scope,
    )

    assert children == []
    assert updated.attributes["color"] == "blue"
    assert updated.attributes_meta["color"]["origin"] == "ai"
    assert updated.attributes_meta["color"]["model_ref"]["model_id"] == "cls"


@pytest.mark.asyncio
async def test_label_prefix_on_written_keys(
    db_session, super_admin, monkeypatch, _patch_io
):
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="image-det")
    task = await create_task(db_session, project_id=proj.id)
    await db_session.flush()
    box = await _mk_box(db_session, task.id, user.id)
    await db_session.flush()

    _patch_predict(
        monkeypatch,
        [
            PredictionResult(
                task_id="0", result=[{"attributes": {"color": "red"}, "score": 0.9}]
            )
        ],
    )

    updated, _ = await _run_and_apply_secondary_result(
        db_session,
        annotation=box,
        task=task,
        backend=_fake_backend(),
        write_target="attributes",
        write_keys=["color"],
        label="plate",
        model_id="cls",
        model_variants=None,
        params=None,
        task_type="classification",
        prompt=None,
        class_filter=None,
        pad=0.08,
        user_id=user.id,
    )

    assert updated.attributes["plate_color"] == "red"
    assert updated.attributes_meta["plate_color"]["origin"] == "ai"
    assert "color" not in updated.attributes  # 原始键被前缀命名空间化


@pytest.mark.asyncio
async def test_attributes_respects_human_edits_and_reuses_ai_slots(
    db_session, super_admin, monkeypatch, _patch_io
):
    """守护 v0.20.10 溯源规则: 人工手改过的属性键 (缺省即 human) 二次推理不再顶回;
    上次 AI 写的键 (origin=ai) 可被再次 AI 覆盖并刷新 model_ref。"""
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="image-det")
    task = await create_task(db_session, project_id=proj.id)
    await db_session.flush()

    # 预置一个框: color=red 是人工手改 (attributes_meta 无该键 → 隐式 human);
    # brand=BMW 是上次 AI 写的 (origin=ai, model_ref 指向旧模型)。
    box = await _mk_box(db_session, task.id, user.id)
    box.attributes = {"color": "red", "brand": "BMW"}
    box.attributes_meta = {
        "brand": {
            "origin": "ai",
            "model_ref": {"backend_id": "old", "model_id": "old-cls"},
        }
    }
    await db_session.flush()

    # 本次 AI 返回 color=blue (想改人工) 与 brand=Audi (想改 AI 上次值)。
    _patch_predict(
        monkeypatch,
        [
            PredictionResult(
                task_id="0",
                result=[
                    {"attributes": {"color": "blue", "brand": "Audi"}, "score": 0.9}
                ],
            )
        ],
    )

    updated, children = await _run_and_apply_secondary_result(
        db_session,
        annotation=box,
        task=task,
        backend=_fake_backend(),
        write_target="attributes",
        write_keys=None,
        label=None,
        model_id="new-cls",
        model_variants=None,
        params=None,
        task_type="classification",
        prompt=None,
        class_filter=None,
        pad=0.08,
        user_id=user.id,
    )

    assert children == []
    # 人工手改的 color 保住原值, meta 仍无该键。
    assert updated.attributes["color"] == "red"
    assert "color" not in updated.attributes_meta
    # AI 上次写的 brand 被本次 AI 覆盖, model_ref 刷新到新模型。
    assert updated.attributes["brand"] == "Audi"
    assert updated.attributes_meta["brand"]["origin"] == "ai"
    assert updated.attributes_meta["brand"]["model_ref"]["model_id"] == "new-cls"


@pytest.mark.asyncio
async def test_geometry_creates_child_boxes(
    db_session, super_admin, monkeypatch, _patch_io
):
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="image-det")
    # 项目 bbox 标签集含 plate → 子检出类名保留 (否则回落 __unknown)
    proj.tool_bindings = {
        "bbox": {
            "enabled": True,
            "classes": [
                {"name": "car", "order": 0},
                {"name": "plate", "order": 1},
            ],
            "attribute_schema": {"fields": []},
        }
    }
    task = await create_task(db_session, project_id=proj.id)
    await db_session.flush()
    box = await _mk_box(db_session, task.id, user.id)
    await db_session.flush()

    # crop 上检出一个子物 (crop 百分比坐标) → 回映 → 建子框
    _patch_predict(
        monkeypatch,
        [
            PredictionResult(
                task_id="0",
                result=[
                    {
                        "type": "rectanglelabels",
                        "value": {
                            "x": 10.0,
                            "y": 10.0,
                            "width": 20.0,
                            "height": 20.0,
                            "rectanglelabels": ["plate"],
                        },
                        "score": 0.8,
                    }
                ],
            )
        ],
    )

    updated, children = await _run_and_apply_secondary_result(
        db_session,
        annotation=box,
        task=task,
        backend=_fake_backend(),
        write_target="geometry",
        write_keys=None,
        label=None,
        model_id="det",
        model_variants={},
        params=None,
        task_type="detection",
        prompt=None,
        class_filter=None,
        pad=0.08,
        user_id=user.id,
    )

    assert len(children) == 1
    child = children[0]
    assert child.parent_annotation_id == box.id
    assert child.source == "prediction_based"
    assert child.class_name == "plate"
    # 回映后落在原图归一化范围内
    assert 0.0 <= child.geometry["x"] <= 1.0
    assert 0.0 <= child.geometry["y"] <= 1.0


@pytest.mark.asyncio
async def test_geometry_unknown_class_falls_back(
    db_session, super_admin, monkeypatch, _patch_io
):
    """子检出类名不在项目标签集 → 回落 __unknown, 不丢框。"""
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="image-det")
    proj.tool_bindings = {
        "bbox": {
            "enabled": True,
            "classes": [{"name": "car", "order": 0}],  # 无 plate
            "attribute_schema": {"fields": []},
        }
    }
    task = await create_task(db_session, project_id=proj.id)
    await db_session.flush()
    box = await _mk_box(db_session, task.id, user.id)
    await db_session.flush()

    _patch_predict(
        monkeypatch,
        [
            PredictionResult(
                task_id="0",
                result=[
                    {
                        "type": "rectanglelabels",
                        "value": {
                            "x": 10.0,
                            "y": 10.0,
                            "width": 20.0,
                            "height": 20.0,
                            "rectanglelabels": ["plate"],
                        },
                        "score": 0.8,
                    }
                ],
            )
        ],
    )

    _, children = await _run_and_apply_secondary_result(
        db_session,
        annotation=box,
        task=task,
        backend=_fake_backend(),
        write_target="geometry",
        write_keys=None,
        label=None,
        model_id="det",
        model_variants={},
        params=None,
        task_type="detection",
        prompt=None,
        class_filter=None,
        pad=0.08,
        user_id=user.id,
    )
    assert len(children) == 1
    assert children[0].class_name == "__unknown"
    assert children[0].parent_annotation_id == box.id


@pytest.mark.asyncio
async def test_tiny_crop_raises_422(db_session, super_admin, monkeypatch, _patch_io):
    """v0.20.21 · 极小框 (退化 ROI) 不再静默返回空, 改明确 422 (不伪装成没结果)。"""
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="image-det")
    task = await create_task(db_session, project_id=proj.id)
    await db_session.flush()
    # 极小框 (200px 图上 1% 宽高 = 2px < min_crop_side_px=32) → crop 守卫跳过
    svc = AnnotationService(db_session)
    box = await svc.create(
        task_id=task.id,
        user_id=user.id,
        annotation_type="bbox",
        class_name="car",
        geometry={"type": "bbox", "x": 0.5, "y": 0.5, "w": 0.01, "h": 0.01},
        tool_unit_id="bbox",
    )
    await db_session.flush()

    _patch_predict(monkeypatch, [])  # 不应被调用

    with pytest.raises(HTTPException) as exc:
        await run_secondary_inference(
            db_session,
            annotation=box,
            task=task,
            backend=_fake_backend(),
            write_target="attributes",
            write_keys=None,
            label=None,
            model_id="cls",
            model_variants=None,
            params=None,
            task_type="classification",
            prompt=None,
            class_filter=None,
            pad=0.08,
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_non_croppable_geometry_raises_400(db_session, super_admin, monkeypatch):
    """v0.20.21 · 门1 能转 LS shape 但门2 取不到外接框的几何 (如 polyline) → 明确 400。"""
    # polyline 门1 → polylinelabels, 门2 _box_bbox_pct 非 rectangle/polygon → None → 400。
    # 400 在读图 / 裁 crop 之前触发, 用 fake annotation 即可 (不落库)。
    _patch_predict(monkeypatch, [])  # 不应被调用
    fake_ann = types.SimpleNamespace(
        id="ann-0001",
        geometry={"type": "polyline", "points": [[0.2, 0.2], [0.5, 0.5], [0.7, 0.3]]},
        class_name="lane",
        confidence=None,
    )

    with pytest.raises(HTTPException) as exc:
        await run_secondary_inference(
            db_session,
            annotation=fake_ann,
            task=types.SimpleNamespace(id="task-0001"),
            backend=_fake_backend(),
            write_target="attributes",
            write_keys=None,
            label=None,
            model_id="cls",
            model_variants=None,
            params=None,
            task_type="classification",
            prompt=None,
            class_filter=None,
            pad=0.08,
        )
    assert exc.value.status_code == 400
    assert "polyline" in exc.value.detail


@pytest.mark.parametrize(
    "geo_type,geometry",
    [
        (
            "rotated_bbox",
            {
                "type": "rotated_bbox",
                "cx": 0.5,
                "cy": 0.5,
                "w": 0.2,
                "h": 0.1,
                "angle": 30,
            },
        ),
        (
            "keypoint",
            {
                "type": "keypoint",
                "points": [
                    {"x": 0.3, "y": 0.3, "v": 2, "id": "n0"},
                    {"x": 0.5, "y": 0.4, "v": 2, "id": "n1"},
                ],
            },
        ),
        (
            "multi_polygon",
            {
                "type": "multi_polygon",
                "polygons": [
                    {
                        "type": "polygon",
                        "points": [[0.1, 0.1], [0.2, 0.1], [0.15, 0.2]],
                    },
                    {
                        "type": "polygon",
                        "points": [[0.5, 0.5], [0.6, 0.5], [0.55, 0.6]],
                    },
                ],
            },
        ),
    ],
)
@pytest.mark.asyncio
async def test_non_croppable_geometry_variants_raise_400(
    db_session, super_admin, monkeypatch, geo_type, geometry
):
    """v0.20.21 · 门2 兜底覆盖: 除 polyline 外, rotated_bbox / keypoint / multi_polygon
    (走 _box_bbox_pct → None 路径) 都应提前 400, 不静默返回空。"""
    _patch_predict(monkeypatch, [])  # 不应被调用
    fake_ann = types.SimpleNamespace(
        id=f"ann-{geo_type}",
        geometry=geometry,
        class_name="cls",
        confidence=None,
    )

    with pytest.raises(HTTPException) as exc:
        await run_secondary_inference(
            db_session,
            annotation=fake_ann,
            task=types.SimpleNamespace(id="task-0001"),
            backend=_fake_backend(),
            write_target="attributes",
            write_keys=None,
            label=None,
            model_id="cls",
            model_variants=None,
            params=None,
            task_type="classification",
            prompt=None,
            class_filter=None,
            pad=0.08,
        )
    assert exc.value.status_code == 400
    assert geo_type in exc.value.detail


@pytest.mark.parametrize(
    "text_outputs,expected",
    [
        # 检测型 (yolo detect-yoloe / gsam2-detection): 自报 ["box"] → 出子框。
        (["box"], "box"),
        # 分割型 (segment-yoloe / *-segmentation): 不支持 box → 取首个受支持值。
        (["mask", "both"], "mask"),
        # 错配 backend 自报非法值 (如 polygon): 过滤掉 → 回落协议默认 mask。
        (["polygon"], "mask"),
        # 非法值混在合法值前: 跳过非法, 取受支持的 both。
        (["polygon", "both"], "both"),
        # 模型未自报 → 协议默认 mask (老 backend 兼容路径)。
        ([], "mask"),
    ],
)
def test_text_output_mode_follows_model_capability(text_outputs, expected):
    """context.output 必须落在协议受控词表 box|mask|both 内, 且随模型自报能力。

    回归守卫: 曾硬编码 "polygon" (非法值), 令一切带 prompt 的二次推理必挂 —— yolo
    backend 严格 pydantic 校验暴露成 500, gsam2/sam3 显式 422。
    """
    from app.services.secondary_inference import _resolve_text_output_mode

    backend = _fake_backend([{"id": "m-1", "supported_text_outputs": text_outputs}])
    mode = _resolve_text_output_mode(backend, "m-1")
    assert mode == expected
    assert mode in ("box", "mask", "both")


def test_text_output_mode_defaults_without_capability_snapshot():
    """能力快照缺失 / model_id 不匹配 → 回落协议默认, 而非发出非法值。"""
    from app.services.secondary_inference import _resolve_text_output_mode

    assert _resolve_text_output_mode(_fake_backend(), "m-1") == "mask"
    assert (
        _resolve_text_output_mode(
            _fake_backend([{"id": "other", "supported_text_outputs": ["box"]}]), "m-1"
        )
        == "mask"
    )


@pytest.mark.asyncio
async def test_prompt_path_sends_legal_output_in_context(
    db_session, super_admin, monkeypatch, _patch_io
):
    """端到端: prompt 非空的开集文本检测, 发往 backend 的 context.output 是 "box"。"""
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="image-det")
    task = await create_task(db_session, project_id=proj.id)
    await db_session.flush()
    box = await _mk_box(db_session, task.id, user.id)
    await db_session.flush()

    seen: dict = {}

    _patch_predict(
        monkeypatch,
        [PredictionResult(task_id="0", result=[])],
        seen=seen,
    )

    await run_secondary_inference(
        db_session,
        annotation=box,
        task=task,
        backend=_fake_backend(
            [{"id": "detect-yoloe", "supported_text_outputs": ["box"]}]
        ),
        write_target="geometry",
        write_keys=None,
        label=None,
        model_id="detect-yoloe",
        model_variants={"series": "yoloe-26", "size": "xlarge"},
        params=None,
        task_type="detection",
        prompt="hook",
        class_filter=None,
        pad=0.08,
    )

    assert seen["context"]["type"] == "text"
    assert seen["context"]["output"] == "box"


@pytest.mark.asyncio
async def test_backend_timeout_surfaces_as_504_not_500(
    db_session, super_admin, monkeypatch, _patch_io
):
    """回归守卫: backend 空闲卸载后按需重载 (SAM 3 冷启动 ~160s) 会超过 ml_predict_timeout。

    批量 wire 的 predict() 原样抛 httpx.ReadTimeout —— 同步的二次推理端点若不翻译, 用户
    只看到含糊的 500。必须是 504 + 可操作文案 (重试即可), 与 predict_interactive 语义一致。
    """

    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="image-det")
    task = await create_task(db_session, project_id=proj.id)
    await db_session.flush()
    box = await _mk_box(db_session, task.id, user.id)
    await db_session.flush()

    _patch_predict(
        monkeypatch,
        [],
        failure_factory=lambda operation: ModelExecutionError(
            ExecutionFailure(
                request_id=operation.request_id,
                kind="transport",
                code="backend_timeout",
                status_code=504,
                detail={"message": "timed out"},
            )
        ),
    )

    with pytest.raises(HTTPException) as exc:
        await run_secondary_inference(
            db_session,
            annotation=box,
            task=task,
            backend=_fake_backend(),
            write_target="geometry",
            write_keys=None,
            label=None,
            model_id="sam3-detection",
            model_variants=None,
            params=None,
            task_type="detection",
            prompt="crane hook",
            class_filter=None,
            pad=0.08,
        )
    assert exc.value.status_code == 504
    assert "重试" in exc.value.detail


async def test_backend_5xx_surfaces_as_502_not_500(
    db_session, super_admin, monkeypatch, _patch_io
):
    """回归守卫: client.predict() 用裸 raise_for_status(), 上游 500 (权重 OOM / 模型内部错)
    抛 httpx.HTTPStatusError。同步二次推理端点必须翻成 502 (backend 故障), 而非冒泡成含糊的
    平台 500 —— 正是本轮修复的老症状, 与 predict_interactive 的 5xx→502 语义一致。"""

    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="image-det")
    task = await create_task(db_session, project_id=proj.id)
    await db_session.flush()
    box = await _mk_box(db_session, task.id, user.id)
    await db_session.flush()

    _patch_predict(
        monkeypatch,
        [],
        failure_factory=lambda operation: ModelExecutionError(
            ExecutionFailure(
                request_id=operation.request_id,
                kind="backend",
                code="backend_upstream_error",
                status_code=500,
                detail={"message": "boom"},
            )
        ),
    )

    with pytest.raises(HTTPException) as exc:
        await run_secondary_inference(
            db_session,
            annotation=box,
            task=task,
            backend=_fake_backend(),
            write_target="geometry",
            write_keys=None,
            label=None,
            model_id="sam3-detection",
            model_variants=None,
            params=None,
            task_type="detection",
            prompt="crane hook",
            class_filter=None,
            pad=0.08,
        )
    assert exc.value.status_code == 502


@pytest.mark.asyncio
async def test_task_model_conflict_rejected_before_image_or_storage(monkeypatch):
    from fastapi import HTTPException

    def forbidden(*args, **kwargs):
        pytest.fail("invalid task must fail before loading or uploading a crop")

    monkeypatch.setattr(prediction_context, "load_task_image", forbidden)
    monkeypatch.setattr(storage_mod, "StorageService", forbidden)
    with pytest.raises(HTTPException) as error:
        await run_secondary_inference(
            None,
            annotation=None,
            task=None,
            backend=_fake_backend([{"id": "vehicle-attr", "task": "classify"}]),
            write_target="attributes",
            write_keys=None,
            label=None,
            model_id="vehicle-attr",
            model_variants=None,
            params=None,
            visual_task="detect",
            prompt=None,
            class_filter=None,
            pad=0.08,
        )
    assert error.value.status_code == 422
