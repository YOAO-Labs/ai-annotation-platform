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


import logging
import uuid
from typing import NoReturn

from aap_inference import resolve_model_task
from aap_inference_contract.topology import RegistryPublicFacts

from aap_inference.secondary import (
    apply_secondary_result,
    build_secondary_context,
)
from aap_inference.roi import _box_bbox_pct, crop_inputs_from_boxes
from aap_inference_contract.execution import (
    FrozenModelOperation,
    ModelExecutionError,
    thaw_json,
)
from fastapi import HTTPException

from app.config import settings
from app.db.models.annotation import Annotation
from app.db.models.task import Task
from app.services.predictions_import import internal_geometry_to_ls_shape
from app.services import prediction_context
from app.services.secondary_inference_result import SecondaryInferenceResult


logger = logging.getLogger("app.services.secondary_inference")


def _raise_secondary_execution_error(error: ModelExecutionError) -> NoReturn:
    failure = error.failure
    if failure.code == "backend_timeout":
        detail = (
            f"推理超时 (>{settings.ml_predict_timeout}s)。backend 可能正在加载模型; "
            "若多个 GPU backend 同时驻留模型, 显存不足会让加载慢一个数量级。"
            "可先卸载暂不使用的 backend 释放显存, 再重试。"
        )
        raise HTTPException(status_code=504, detail=detail) from error
    if failure.code == "backend_unreachable":
        raise HTTPException(
            status_code=502, detail=thaw_json(failure.detail)
        ) from error
    if failure.kind == "backend" and failure.status_code is not None:
        status = failure.status_code
        raise HTTPException(
            status_code=status if status < 500 or status == 503 else 502,
            detail=f"ML backend error (HTTP {status})",
            headers=(
                {"Retry-After": failure.retry_after}
                if failure.retry_after is not None
                else None
            ),
        ) from error

    detail = thaw_json(failure.detail)
    if failure.kind == "admission":
        if not isinstance(detail, dict):
            detail = {"message": str(detail)}
        detail.setdefault("error_code", failure.code)
    status = failure.status_code or (503 if failure.kind == "unavailable" else 502)
    raise HTTPException(
        status_code=status,
        detail=detail,
        headers=(
            {"Retry-After": failure.retry_after}
            if failure.retry_after is not None
            else None
        ),
    ) from error


async def run_secondary_inference(
    db,
    *,
    annotation: Annotation,
    task: Task,
    backend: RegistryPublicFacts,
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
    visual_task: str | None = None,
    prompt_type: str | None = None,
) -> SecondaryInferenceResult:
    """Run secondary inference and return immutable output facts for labeling."""
    from app.services.inference_model_execution import model_execution_port
    from app.services.ml_backend import MLBackendService
    from app.services.storage import StorageService
    from app.services.trained_models import is_controlled

    if is_controlled(model_id):
        raise HTTPException(status_code=422, detail="受控模型当前不支持框内二次推理")

    try:
        visual_task = resolve_model_task(backend, model_id, visual_task or task_type)
        context = build_secondary_context(
            health_meta=backend.health_meta,
            prompt=prompt,
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

    requested_pool_id = await MLBackendService(db).pool_id_for_registry(
        uuid.UUID(str(backend.id))
    )
    request = FrozenModelOperation(
        request_id=uuid.uuid4(),
        operation="batch_predict",
        task_inputs=tuple(batch.inputs),
        context=context,
        project_id=uuid.UUID(str(task.project_id)),
        requested_registry_id=uuid.UUID(str(backend.id)),
        requested_pool_id=requested_pool_id,
        endpoint_registry_id=None,
        required_instance_id=None,
        route_owner=f"secondary:{annotation.id}",
        route_operation="secondary_inference",
        correlation={"annotation_id": str(annotation.id)},
    )
    try:
        operation = await model_execution_port(db).execute(request)
    except ModelExecutionError as exc:
        _raise_secondary_execution_error(exc)

    output = apply_secondary_result(
        ls_box,
        operation,
        write_target=write_target,
        model_id=model_id,
        write_keys=write_keys,
        label=label,
        transform=batch.transforms.get("0"),
    )
    return SecondaryInferenceResult(
        write_target=output.write_target,
        model_ref=output.model_ref,
        attributes=output.attributes,
        geometry_shapes=output.geometry_shapes,
        execution_provenance=operation.provenance,
    )
