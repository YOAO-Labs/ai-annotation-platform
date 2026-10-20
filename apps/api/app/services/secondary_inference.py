"""v0.20.11 · 选中框单框二次推理 (Q1b)。

选中一个已落库标注 → 把它的 bbox 当 ROI 裁 crop → 同步喂某能力 backend /predict →
产物按类型归位: 属性型写回原框 (attributes_meta 标 origin=ai, 走 v0.20.10)、几何型建
子框 (parent_annotation_id=选中框, 走 v0.20.9)。

**复用 batch pipeline 下游阶段的同一套投递**: `crop_inputs_from_boxes` (裁 ROI + 上传) +
`build_predict_context` (construct the wire context) + `merge_classify_attributes` / `remap_geometry_to_image`
(产物归位)。与批量二次推理是「同一能力两个触发面」, 产物 schema / 溯源写法一致。
不走 worker (单框同步秒回), 不新建投递逻辑。
"""

from __future__ import annotations

from aap_inference import resolve_model_task

import logging

import httpx
from fastapi import HTTPException

from app.config import settings
from app.db.models.annotation import Annotation
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.db.models.task import Task
from app.services.gpu_arbitration.contracts import (
    GPUDispatchContextFactory,
    GPUShadowSessionFactory,
)
from app.services.predictions_import import internal_geometry_to_ls_shape
from app.services import prediction_context
from app.services.prediction_roi import (
    _box_bbox_pct,
    crop_inputs_from_boxes,
    merge_classify_attributes,
    remap_geometry_to_image,
)
from app.services.secondary_inference_result import (
    SecondaryInferenceResult,
    freeze_json,
)


logger = logging.getLogger("app.services.secondary_inference")


def _resolve_text_output_mode(backend: MLBackendRegistry, model_id: str | None) -> str:
    """按所选模型自报的 `supported_text_outputs` 定 `context.output` (仅 prompt 路径生效)。

    协议只认 box|mask|both。检测型模型自报 ["box"], 分割型自报 ["mask","both"] ——
    优先 box (二次推理产子框), 否则取模型支持的首个; 无自报时按协议默认 "mask"。
    """
    caps = (backend.health_meta or {}).get("capabilities") or {}
    for m in caps.get("models") or []:
        if m.get("id") != model_id:
            continue
        outs = m.get("supported_text_outputs") or []
        if "box" in outs:
            return "box"
        # 协议只认 box|mask|both: 错配 backend 自报 ["polygon"] 之类时不原样透传, 回落默认,
        # 避免把非法 output 送进 context 重现最初的 output 非法值问题。
        for candidate in outs:
            if candidate in {"box", "mask", "both"}:
                return candidate
        break
    return "mask"


async def run_secondary_inference(
    db,
    *,
    annotation: Annotation,
    task: Task,
    backend: MLBackendRegistry,
    write_target: str,
    write_keys: list[str] | None,
    label: str | None,
    model_id: str | None,
    model_variants: dict | None,
    params: dict | None,
    task_type: str | None = None,
    prompt: str | None,
    class_filter: list[int] | None,
    pad: float,
    shadow_session_factory: GPUShadowSessionFactory | None = None,
    dispatch_context_factory: GPUDispatchContextFactory | None = None,
    visual_task: str | None = None,
    prompt_type: str | None = None,
) -> SecondaryInferenceResult:
    """Run secondary inference and return immutable output facts for labeling."""
    from app.services.ml_routing.client import RoutedMLBackendClient
    from app.services.storage import StorageService
    from app.services.trained_models import is_controlled

    if is_controlled(model_id):
        raise HTTPException(status_code=422, detail="受控模型当前不支持框内二次推理")

    try:
        visual_task = resolve_model_task(backend, model_id, visual_task or task_type)
        context = prediction_context.build_predict_context(
            prompt=prompt,
            output_mode=_resolve_text_output_mode(backend, model_id),
            params=params,
            model_id=model_id,
            task=visual_task,
            task_type=task_type,
            prompt_type=prompt_type,
            model_variants=model_variants,
            class_filter=class_filter,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # 1. 内部几何 (0-1) → LS 标准 box (百分比 0-100), 供 crop 复用批量投递。
    #    起始不带 attributes: merge 后 box["attributes"] 里的键即全为 AI 写入 (精确标 origin)。
    ls_box = internal_geometry_to_ls_shape(
        annotation.geometry, annotation.class_name, annotation.confidence
    )
    if ls_box is None:
        raise HTTPException(
            status_code=400,
            detail="selected annotation geometry is not croppable (need bbox/polygon)",
        )

    # v0.20.21 · 门2 几何判据: 旋转框 / polyline / keypoint / multi_polygon 能转 LS shape
    #   (门1 过), 但 _box_bbox_pct 取不到轴对齐外接框 → 无法作 ROI。此前静默返回空被前端
    #   伪装成「没结果」, 这里提前明确报错并告知实际几何类型 (与批量链路 skipped_geometry 对称)。
    if _box_bbox_pct(ls_box) is None:
        geo_type = (annotation.geometry or {}).get("type", "unknown")
        raise HTTPException(
            status_code=400,
            detail=(
                f"几何类型 {geo_type!r} 无法作二次推理 ROI (仅支持轴对齐 bbox / 多边形; "
                "旋转框 / 线 / 点 / 多连通多边形不支持)"
            ),
        )

    # 2. 读原图 (RGB) 供裁 ROI。
    image = prediction_context.load_task_image(task)

    # 3. 裁 crop + 上传 (presigned, 对所有走 httpx.get 的 backend 通用)。
    #    自建 2 参 upload_fn (crop_inputs_from_boxes 调 upload_fn(idx, bytes));
    #    key 复用 batch 的 roi-crops/ 前缀 → 同享 import 桶 7 天 lifecycle 自清。
    storage = StorageService()

    def upload_fn(box_idx: int, jpeg_bytes: bytes) -> str:
        key = f"roi-crops/secondary/{annotation.id}/{box_idx}.jpg"
        return storage.upload_crop_bytes(jpeg_bytes, key)

    batch = crop_inputs_from_boxes(
        image,
        [ls_box],
        pad=pad,
        delivery="presigned",
        upload_fn=upload_fn,
        min_crop_side_px=32,
    )
    if not batch.inputs:
        # v0.20.21 · 几何已判可裁 (上面门2 通过), 此处 inputs 空只可能是 crop 退化:
        #   框太小 (短边 < min_crop_side_px=32) 或贴边裁出零面积。明确报错, 不再静默返回空。
        raise HTTPException(
            status_code=422,
            detail="选中框过小或贴边, 裁出的 ROI 退化, 无法二次推理 (可放大框或改选更大的对象)",
        )

    # 4. 构造发往 backend 的 context (与批量下游同一纯函数)。

    # 5. 同步推理。批量 wire 的 predict() 不翻译传输层异常 (worker 靠原始异常分类失败原因),
    #    但二次推理是同步用户请求 —— 冒泡的 ReadTimeout 会变成含糊的 500。这里按
    #    predict_interactive 的同款语义翻译成 504。
    #    典型诱因: backend 空闲卸载后按需重载 (冷启动本身只需 ~10s), 但多个 GPU backend 同时
    #    驻留模型挤爆显存时, 加载会退化一个数量级 (实测 8s → 160s) 而超时。故文案指向卸载
    #    其它 backend, 而非调大 ML_PREDICT_TIMEOUT (后者只是盖住症状)。
    client = RoutedMLBackendClient(
        db,
        backend,
        project_id=task.project_id,
        owner=f"secondary:{annotation.id}",
        operation="secondary_inference",
        shadow_session_factory=shadow_session_factory,
        dispatch_context_factory=dispatch_context_factory,
    )
    try:
        results = await client.predict(batch.inputs, context=context)
    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=504,
            detail=(
                f"推理超时 (>{settings.ml_predict_timeout}s)。backend 可能正在加载模型; "
                "若多个 GPU backend 同时驻留模型, 显存不足会让加载慢一个数量级。"
                "可先卸载暂不使用的 backend 释放显存, 再重试。"
            ),
        ) from exc
    except httpx.ConnectError as exc:
        raise HTTPException(
            status_code=502, detail=f"ML backend unreachable: {exc}"
        ) from exc
    except httpx.HTTPStatusError as exc:
        # client.predict() 用裸 raise_for_status() (worker 靠原始异常分类失败原因); 同步
        # 二次推理这里对齐 predict_interactive 的 _raise_for_backend_status: 上游 4xx 透传、
        # 5xx (权重加载 OOM / 模型内部错) → 502, 别让它冒泡成含糊 500。
        status = exc.response.status_code
        raise HTTPException(
            status_code=status if (status < 500 or status == 503) else 502,
            detail=f"ML backend error (HTTP {status})",
        ) from exc

    model_ref = {
        "backend_id": str(client.last_instance_id or backend.id),
        "model_id": model_id,
    }

    if write_target == "attributes":
        merge_classify_attributes([ls_box], results, write_keys=write_keys, label=label)
        ai_attrs = ls_box.get("attributes") or {}
        return SecondaryInferenceResult(
            write_target="attributes",
            model_ref=tuple(model_ref.items()),
            attributes=tuple(
                (key, freeze_json(value)) for key, value in ai_attrs.items()
            ),
            geometry_shapes=(),
        )

    # 6b. Remap crop output to original-image coordinates; labeling creates children.
    transform = batch.transforms.get("0")
    raw_shapes = [s for cr in results for s in (cr.result or []) if isinstance(s, dict)]
    remapped = remap_geometry_to_image(raw_shapes, transform) if transform else []
    return SecondaryInferenceResult(
        write_target="geometry",
        model_ref=tuple(model_ref.items()),
        attributes=(),
        geometry_shapes=tuple(freeze_json(shape) for shape in remapped),
    )
