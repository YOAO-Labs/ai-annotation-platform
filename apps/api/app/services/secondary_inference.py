"""v0.20.11 · 选中框单框二次推理 (Q1b)。

选中一个已落库标注 → 把它的 bbox 当 ROI 裁 crop → 同步喂某能力 backend /predict →
产物按类型归位: 属性型写回原框 (attributes_meta 标 origin=ai, 走 v0.20.10)、几何型建
子框 (parent_annotation_id=选中框, 走 v0.20.9)。

**复用 batch pipeline 下游阶段的同一套投递**: `crop_inputs_from_boxes` (裁 ROI + 上传) +
`_build_predict_context` (构造 wire) + `merge_classify_attributes` / `remap_geometry_to_image`
(产物归位)。与批量二次推理是「同一能力两个触发面」, 产物 schema / 溯源写法一致。
不走 worker (单框同步秒回), 不新建投递逻辑。
"""

from __future__ import annotations

import logging
import uuid

import httpx
from fastapi import HTTPException

from app.config import settings
from app.db.models.annotation import Annotation
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.db.models.task import Task
from app.services.content_snapshot import ContentError
from app.services.gpu_arbitration.contracts import (
    GPUDispatchContextFactory,
    GPUShadowSessionFactory,
)
from app.services.predictions_import import internal_geometry_to_ls_shape
from app.workers.roi import (
    _box_bbox_pct,
    crop_inputs_from_boxes,
    merge_classify_attributes,
    remap_geometry_to_image,
)


logger = logging.getLogger("app.services.secondary_inference")


async def _create_child_box(
    svc,
    *,
    task,
    user_id: uuid.UUID,
    annotation_type: str,
    class_name: str,
    geometry: dict,
    confidence: float | None,
    parent: Annotation,
) -> Annotation:
    """建子框; **仅** class 不在项目标签集时回落 "__unknown" 哨兵 (不丢 AI 检出框)。

    子检测产出 backend 原生类名 (NG6: 平台不做类→项目标签映射)。若该类名不在项目
    tool_bindings 允许集, create 的软校验会 422 —— 对 AI 产物不应丢框, 回落"未分类
    待补", 由用户后续归类。

    历史坑: 早版本吞掉**所有** 422, 未来若 create 加了 geometry / attribute 校验, 也会
    被静默回落 __unknown, 覆盖真正的 bug。收窄条件: 只对 detail 明确点名"类别集合内"的
    422 回落, 其它 422 (含未来新增校验) 原样抛。
    """
    try:
        return await svc.create(
            task_id=task.id,
            user_id=user_id,
            annotation_type=annotation_type,
            class_name=class_name,
            geometry=geometry,
            confidence=confidence,
            parent_annotation_id=parent.id,
            tool_unit_id=parent.tool_unit_id,
            video_segment_id=parent.video_segment_id,
        )
    except HTTPException as exc:
        # 判据: AnnotationService._assert_class_allowed 的 detail 稳定串「不在工具单位
        # '<tool_unit>' 的类别集合内」——用这个哨兵区分"class 不在标签集"vs 其它 422。
        detail = str(getattr(exc, "detail", ""))
        if exc.status_code != 422 or "类别集合内" not in detail:
            raise
        logger.info(
            "secondary_inference: 子检测类名 %r 不在项目标签集, 回落 __unknown",
            class_name,
        )
        return await svc.create(
            task_id=task.id,
            user_id=user_id,
            annotation_type=annotation_type,
            class_name="__unknown",
            geometry=geometry,
            confidence=confidence,
            parent_annotation_id=parent.id,
            tool_unit_id=parent.tool_unit_id,
            video_segment_id=parent.video_segment_id,
        )


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


def prepare_secondary_request(
    annotation: Annotation,
    backend: MLBackendRegistry,
    *,
    model_id: str | None,
    model_variants: dict | None,
    params: dict | None,
    task_type: str | None = None,
    prompt: str | None,
    class_filter: list[int] | None,
    visual_task: str | None = None,
    prompt_type: str | None = None,
) -> tuple[dict, dict]:
    """Validate model and geometry before acquiring any physical source input."""
    from app.workers.tasks import _build_predict_context
    from app.services.trained_models import is_controlled

    if is_controlled(model_id):
        raise HTTPException(status_code=422, detail="受控模型当前不支持框内二次推理")

    from app.services.ml_capabilities import resolve_model_task

    try:
        visual_task = resolve_model_task(backend, model_id, visual_task or task_type)
        context = _build_predict_context(
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
    return ls_box, context


async def _run_secondary_inference(
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
    user_id: uuid.UUID,
    shadow_session_factory: GPUShadowSessionFactory | None = None,
    dispatch_context_factory: GPUDispatchContextFactory | None = None,
    visual_task: str | None = None,
    prompt_type: str | None = None,
    original_operation=None,
    inputs=None,
    prepared_request=None,
) -> tuple[Annotation, list[Annotation]]:
    """Write attributes or child geometries from one selected annotation's ROI."""
    from app.services.ml_routing.client import RoutedMLBackendClient
    from app.services.prediction import to_internal_shape
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
    managed_image = task.file_type == "image"
    if is_reference(task.file_path):
        raise HTTPException(409, detail={"code": "reference_operation_not_supported"})
    if managed_image:
        if original_operation is None:
            raise ContentError("prediction_original_requires_new_request")
        batch = await original_operation.prepare_crop(ls_box, pad=pad)
    else:
        if original_operation is not None:
            raise ContentError("prediction_original_receipt_invalid")
        from app.workers.tasks import _load_task_image

        image = _load_task_image(task)
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
        results = await client.predict(
            batch.inputs,
            context=context,
            **(
                {"before_dispatch": original_operation.before_dispatch}
                if original_operation is not None
                else {}
            ),
        )
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

    if inputs is not None:
        # Temporary crop delivery ends before any annotation mutation.
        await inputs.close()
    if original_operation is not None:
        task, annotation = await original_operation.final()

    model_ref = {
        "backend_id": str(client.last_instance_id or backend.id),
        "model_id": model_id,
    }

    if write_target == "attributes":
        # 6a. 属性型: merge 进 (起始为空的) ls_box, 写入键即 AI 产物。
        merge_classify_attributes([ls_box], results, write_keys=write_keys, label=label)
        ai_attrs = ls_box.get("attributes") or {}
        if ai_attrs:
            # 遵守 v0.20.10 溯源规则 (annotation-module.md §属性级溯源):
            #   attributes_meta 只存 origin=ai 的键, "缺省即 human"; 人工手改会删该键 meta。
            # 故某键**已有值且 meta 不标 origin=ai** → 视为人工手改过, 二次推理不再顶回;
            # 键尚未存在 或 meta 标 origin=ai → 可安全覆盖并 (重) 标 origin=ai + 新 model_ref。
            cur_attrs = dict(annotation.attributes or {})
            cur_meta = dict(annotation.attributes_meta or {})
            for k, v in ai_attrs.items():
                if k in cur_attrs and (cur_meta.get(k) or {}).get("origin") != "ai":
                    continue
                cur_attrs[k] = v
                cur_meta[k] = {"origin": "ai", "model_ref": model_ref}
            changed = cur_attrs != dict(
                annotation.attributes or {}
            ) or cur_meta != dict(annotation.attributes_meta or {})
            if changed or not managed_image:
                annotation.attributes = cur_attrs
                annotation.attributes_meta = cur_meta
                if managed_image:
                    annotation.version = int(annotation.version or 1) + 1
                await db.flush()
        return annotation, []

    # 6b. 几何型: crop 检出几何回映回原图坐标 → 建子框, 挂原框下。
    from app.services.annotation import AnnotationService

    transform = batch.transforms.get("0")
    raw_shapes = [s for cr in results for s in (cr.result or []) if isinstance(s, dict)]
    remapped = remap_geometry_to_image(raw_shapes, transform) if transform else []

    svc = AnnotationService(db)
    children: list[Annotation] = []
    for shp in remapped:
        internal = to_internal_shape(shp)
        geometry = internal.get("geometry")
        if not geometry:
            continue
        detected_class = internal.get("class_name") or annotation.class_name
        child = await _create_child_box(
            svc,
            task=task,
            user_id=user_id,
            annotation_type=internal.get("type", "bbox"),
            class_name=detected_class,
            geometry=geometry,
            confidence=internal.get("confidence"),
            parent=annotation,
        )
        # AI 产物: 与批量二次推理一致标 prediction_based (create 默认 manual)。
        child.source = "prediction_based"
        children.append(child)
    if children:
        await db.flush()
    return annotation, children


async def run_secondary_inference(db, *, input_session_factory=None, **kwargs):
    """Keep the existing inference operation inside its temporary-input owner."""
    prepared = prepare_secondary_request(
        kwargs["annotation"],
        kwargs["backend"],
        model_id=kwargs.get("model_id"),
        model_variants=kwargs.get("model_variants"),
        params=kwargs.get("params"),
        task_type=kwargs.get("task_type"),
        prompt=kwargs.get("prompt"),
        class_filter=kwargs.get("class_filter"),
        visual_task=kwargs.get("visual_task"),
        prompt_type=kwargs.get("prompt_type"),
    )
    if kwargs["task"] is None or kwargs["task"].file_type == "image":
        return await _run_secondary_inference(db, prepared_request=prepared, **kwargs)
    from app.services.reference_keys import is_reference

    if is_reference(kwargs["task"].file_path):
        raise HTTPException(409, detail={"code": "reference_operation_not_supported"})
    if kwargs.get("original_operation") is not None:
        raise ContentError("prediction_original_receipt_invalid")
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.services import prediction_inputs, storage_access

    factory = input_session_factory or async_sessionmaker(
        storage_access.engine(db), expire_on_commit=False
    )
    async with prediction_inputs.prediction_inputs(
        factory,
        owner_id=uuid.uuid4(),
        owner_kind="secondary_inference",
        task_id=kwargs["task"].id,
    ) as inputs:
        return await _run_secondary_inference(
            db, inputs=inputs, prepared_request=prepared, **kwargs
        )
