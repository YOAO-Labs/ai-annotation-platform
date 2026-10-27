"""v0.20.11 · 选中框单框二次推理 (Q1b)。

选中一个已落库标注 → 把它的 bbox 当 ROI 裁 crop → 同步喂某能力 backend /predict →
产物按类型归位: 属性型写回原框 (attributes_meta 标 origin=ai, 走 v0.20.10)、几何型建
子框 (parent_annotation_id=选中框, 走 v0.20.9)。

**复用批量 pipeline 下游阶段的同一套投递**: `crop_inputs_from_boxes` (裁 ROI + 投递) +
`build_secondary_context` (构造 wire) + `apply_secondary_result` (产物归位事实)。与批量
二次推理是「同一能力两个触发面」, 产物 schema / 溯源写法一致。
不走 worker (单框同步秒回), 不新建投递逻辑。

图片 task 的原图读取由 ``managed_secondary_operation`` 独占: 冻结原图/父框事实、把原图
快照写入带不可变目标的临时输入租约, 并在最终写事务里重新校验。其它 task 的 crop 投递
走 ``prediction_inputs`` 临时租约。两者都不会把业务锁带进远程推理。
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


def prepare_secondary_request(
    annotation: Annotation,
    backend: RegistryPublicFacts,
    *,
    model_id: str | None,
    model_variants: dict | None,
    params: dict | None,
    task_type: str | None = None,
    prompt: str | None,
    class_filter: list[int] | None,
    visual_task: str | None = None,
    prompt_type: str | None = None,
) -> tuple[dict, dict | None]:
    """Validate model and geometry before acquiring any physical source input."""
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
    return ls_box, context


async def _temporary_crop_batch(task: Task, ls_box: dict, *, pad: float, inputs):
    """Encode one ROI and deliver it through its temporary input owner."""
    image = prediction_context.load_task_image(task)
    try:
        batch = crop_inputs_from_boxes(
            image,
            [ls_box],
            pad=pad,
            delivery="deferred",
            min_crop_side_px=32,
        )
    finally:
        image.close()
    for item in batch.inputs:
        index = str(item["id"])
        item["file_path"] = await inputs.upload_crop(
            task, int(index), batch.contents.pop(index)
        )
    return batch


async def _execute_secondary(
    db,
    *,
    annotation: Annotation,
    task: Task,
    backend: RegistryPublicFacts,
    write_target: str,
    write_keys: list[str] | None,
    label: str | None,
    model_id: str | None,
    ls_box: dict,
    context: dict | None,
    batch,
    original_operation,
) -> SecondaryInferenceResult:
    """Dispatch one frozen operation and return immutable secondary facts."""
    from app.services.inference_model_execution import model_execution_port
    from app.services.ml_backend import MLBackendService

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
    if original_operation is not None:
        # Recheck the frozen source immediately before transport; the final
        # fence below re-validates it under the caller's write locks.
        await original_operation.before_dispatch()
    try:
        operation = await model_execution_port(db).execute(request)
    except ModelExecutionError as exc:
        _raise_secondary_execution_error(exc)
    if original_operation is not None:
        await original_operation.final()

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
    input_session_factory=None,
    original_operation=None,
    prepared_request: tuple[dict, dict | None] | None = None,
) -> SecondaryInferenceResult:
    """Run secondary inference and return immutable output facts for labeling.

    Image tasks must supply their ``managed_secondary_operation`` so the frozen
    original and parent facts are checked in the final caller transaction.
    """
    from app.services.prediction_inputs import prediction_inputs
    from app.services.reference_keys import is_reference

    ls_box, context = prepared_request or prepare_secondary_request(
        annotation,
        backend,
        model_id=model_id,
        model_variants=model_variants,
        params=params,
        task_type=task_type,
        prompt=prompt,
        class_filter=class_filter,
        visual_task=visual_task,
        prompt_type=prompt_type,
    )

    dispatch = dict(
        annotation=annotation,
        task=task,
        backend=backend,
        write_target=write_target,
        write_keys=write_keys,
        label=label,
        model_id=model_id,
        ls_box=ls_box,
        context=context,
    )

    if task.file_type == "image":
        if original_operation is None:
            raise HTTPException(
                status_code=409,
                detail={"code": "prediction_original_requires_new_request"},
            )
        batch = await original_operation.prepare_crop(ls_box, pad=pad)
        if not batch.inputs:
            raise HTTPException(
                status_code=422,
                detail="选中框过小或贴边, 裁出的 ROI 退化, 无法二次推理 (可放大框或改选更大的对象)",
            )
        return await _execute_secondary(
            db, **dispatch, batch=batch, original_operation=original_operation
        )

    if original_operation is not None:
        raise HTTPException(
            status_code=409,
            detail={"code": "prediction_original_receipt_invalid"},
        )
    # Reference originals have no in-place crop contract for non-image tasks.
    if is_reference(task.file_path):
        raise HTTPException(
            status_code=409, detail={"code": "reference_operation_not_supported"}
        )
    if input_session_factory is None:
        raise HTTPException(
            status_code=409, detail={"code": "prediction_input_required"}
        )
    async with prediction_inputs(
        input_session_factory,
        owner_id=uuid.uuid4(),
        owner_kind="secondary_inference",
        task_id=task.id,
    ) as inputs:
        batch = await _temporary_crop_batch(task, ls_box, pad=pad, inputs=inputs)
        if not batch.inputs:
            # v0.20.21 · 几何已判可裁 (上面门2 通过), 此处 inputs 空只可能是 crop 退化:
            #   框太小 (短边 < min_crop_side_px=32) 或贴边裁出零面积。
            raise HTTPException(
                status_code=422,
                detail="选中框过小或贴边, 裁出的 ROI 退化, 无法二次推理 (可放大框或改选更大的对象)",
            )
        return await _execute_secondary(
            db, **dispatch, batch=batch, original_operation=None
        )
