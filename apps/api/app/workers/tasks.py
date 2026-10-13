import asyncio
import json
import logging
import time
import uuid

import redis

from app.config import settings
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)
_MAX_FAILED_PREDICTION_CONTEXT_BYTES = 8 * 1024


async def _preannotation_actor_revoked(db, actor, project_uuid) -> bool:
    """Fresh-state authority check for the initiating preannotation actor."""

    from app.db.models.project import Project
    from app.services.project_access import is_privileged_for_project

    await db.refresh(actor)
    fresh_project = await db.get(Project, project_uuid, populate_existing=True)
    return (
        not actor.is_active
        or fresh_project is None
        or not is_privileged_for_project(actor, fresh_project)
    )


async def _hold_preannotation_authority(db, actor, project_uuid) -> bool:
    """Re-check actor/project authority under bounded locks and hold it.

    Inference is long: the initiating account may be disabled or the project
    ownership transferred while it runs, after the pre-inference check passed.
    Account -> project share locks (matching the lifecycle lock order) make a
    concurrent disable or ownership transfer conflict with this transaction,
    so the authority verified here cannot be revoked between this check and
    the caller's final-write ``commit()`` which releases the locks.
    """

    from fastapi import HTTPException

    from app.services.project_write_guard import lock_actor_project_authority

    try:
        await lock_actor_project_authority(db, actor.id, project_uuid, nowait=False)
    except HTTPException:
        return False
    return not await _preannotation_actor_revoked(db, actor, project_uuid)


def _retryable_request_context(context: dict | None) -> dict | None:
    if context is None:
        return None
    payload = json.dumps(context, ensure_ascii=False, separators=(",", ":")).encode()
    return context if len(payload) <= _MAX_FAILED_PREDICTION_CONTEXT_BYTES else None


def _stage_totals_snapshot(totals: dict[int, dict]) -> list[dict]:
    """v0.18.6 · 把逐阶段累加器拍平成 ``[{stage, detected/targeted/ok/failed/...}]`` (按 stage 升序)。

    实时推送 (运行中快照) 与终态写库 (result.pipeline_stages) 共用同一形态, 前端可无缝切换。
    """
    return [{"stage": sidx, **totals[sidx]} for sidx in sorted(totals)]


def _publish_progress(
    project_id: str,
    current: int,
    total: int,
    status: str = "running",
    error: str | None = None,
    job_meta: dict | None = None,
    pipeline_stages: list[dict] | None = None,
):
    """v0.9.5: 单项目预标进度. v0.9.8: 同时发到全局 channel `global:prediction-jobs`,
    让 Topbar 徽章 / 切项目 toast 可跨项目订阅. job_meta 仅在开始/结束/失败 3 时点发,
    避免高频中间帧塞爆全局通道.

    v0.18.6: pipeline_stages (运行中逐阶段累加快照) 随项目通道发, 让前端跑批过程中实时更新
    卡上徽标 (不再等 job 终态); 阶段数少 (单层扇出), 仍按 5% 步长节流由调用方控制。"""
    payload = {
        "current": current,
        "total": total,
        "status": status,
        "error": error,
    }
    if pipeline_stages is not None:
        payload["pipeline_stages"] = pipeline_stages
    r = redis.from_url(settings.redis_url)
    try:
        r.publish(f"project:{project_id}:preannotate", json.dumps(payload))
        if job_meta is not None:
            global_payload = {**payload, "project_id": project_id, **job_meta}
            r.publish("global:prediction-jobs", json.dumps(global_payload))
    finally:
        r.close()


def _build_predict_context(
    *,
    prompt: str | None,
    output_mode: str,
    params: dict | None,
    model_id: str | None,
    task_type: str | None,
    model_variants: dict | None,
    class_filter: list[int] | None = None,
    box_threshold: float | None = None,
    text_threshold: float | None = None,
) -> dict | None:
    """构造发往 backend /predict 的 context (纯函数, 便于单测).

    三条互斥路径 (按优先序):
    - **文本 prompt 扁平路径** (gsam2 / sam3 开放词表): 顶层 `text` / `output` / 阈值 / params,
      v0.18.12 起**统一 wire**额外带 `model_id` + `model_variants`——后端据 model_id 路由
      (detection→box / segmentation→output)。prompt 非空即此路径 (优先于 v2): 几何 backend 不发
      prompt, 故不会误入。老 wire (无 model_id) 仍兼容。
    - **协议 v2 结构化** (model_variants 非 None 且无 prompt, 几何 backend yolo/onnxtools): backend 要
      `model_variants` dict + nested `params` + `type=<几何 task>` + `classes` 白名单。判定用
      `is not None` 而非真值: variant 轴未就位时为空 dict `{}` 仍须走 v2 才能透传 class_filter。
    - **老扁平路径** (OCR / doc_layout: 仅 model_id + task_type, 无 prompt 无 variants)。
    """
    # ── 文本 prompt 扁平路径 (gsam2 / sam3): 顶层 params + model_id + model_variants 统一 wire。 ──
    if prompt:
        context: dict = {"type": "text", "text": prompt, "output": output_mode}
        if box_threshold is not None:
            context["box_threshold"] = box_threshold
            context["text_threshold"] = text_threshold
        if model_id:
            context["model_id"] = model_id
        if model_variants:
            context["model_variants"] = model_variants
        _reserved = {"type", "text", "output", "model_id", "model_variants"}
        if params:
            context.update(
                {
                    k: v
                    for k, v in params.items()
                    if v is not None and k not in _reserved
                }
            )
        return context

    # ── 协议 v2 结构化 (几何 backend, 无 prompt)。 ──
    if model_variants is not None:
        ctx: dict = {
            "type": task_type or "detection",
            "model_variants": model_variants,
            "params": params or {},
        }
        if model_id:
            ctx["model_id"] = model_id
        if class_filter:
            # v0.14.17 · 类别白名单 (模型原生类别 index 子集); yolo /predict 用 model.predict(classes=) 过滤.
            ctx["classes"] = class_filter
        return ctx

    # ── 老扁平 (OCR / doc_layout / 纯分类下游: model_id + task_type, 无 prompt 无 variants)。 ──
    context2: dict | None = None
    if task_type or model_id:
        context2 = {}
        if task_type:
            context2["type"] = task_type
        if model_id:
            context2["model_id"] = model_id
        # 透传 params: 多阶段下游 (如 onnxtools 纯分类) 经 StageCard 带 params, 早期 flat 路径
        # 漏了透传; 顶层保留键 (type/model_id) 不被 params 覆盖, 与文本路径一致。
        if params:
            _reserved2 = {"type", "model_id"}
            context2.update(
                {
                    k: v
                    for k, v in params.items()
                    if v is not None and k not in _reserved2
                }
            )
    return context2


def _model_label(model_variants: dict | None) -> str | None:
    """从 model_variants 派生展示串, 与 backend 回传的 model_version 一致 (series+size, 如 yolov8l).

    variant 轴未就位时 model_variants 为空 dict {} → 返回 None (不展示误导标签, backend 走默认)。
    """
    if not model_variants:
        return None
    series = model_variants.get("series")
    size = model_variants.get("size")
    if series and size:
        return f"{series}{size}"
    vals = [str(v) for v in model_variants.values() if v]
    return "".join(vals) or None


def _load_task_image(task):
    """v0.18.1 · 从对象存储读 task 原图为 PIL Image (RGB), 供平台裁 ROI crop。

    直接走 StorageService boto3 client (内网 endpoint), 不经 presigned URL / host 重写,
    避免 worker 容器无法解析 ml_backend_storage_host 的问题。
    """
    import io

    from PIL import Image

    from app.services.storage import StorageService

    storage = StorageService()
    bucket = storage.datasets_bucket if task.dataset_item_id else storage.bucket
    obj = storage.client.get_object(Bucket=bucket, Key=task.file_path)
    raw = obj["Body"].read()
    return Image.open(io.BytesIO(raw)).convert("RGB")


def _make_crop_uploader(storage, job_id: str):
    """v0.18.4 · 返回 ``upload_crop(task, box_idx, jpeg_bytes) -> presigned URL``。

    crop 上传 import 桶 (7 天 lifecycle 自动清), key=``roi-crops/{job_id}/{task_id}/{box_idx}.jpg``,
    经 ml_backend host 重写返回容器可拉取 URL——对所有走 ``httpx.get`` 的下游后端通用
    (gsam2/sam3 不支持 ``data:`` URI)。
    """

    def upload_crop(task, box_idx: int, jpeg_bytes: bytes) -> str:
        key = f"roi-crops/{job_id}/{task.id}/{box_idx}.jpg"
        return storage.upload_crop_bytes(jpeg_bytes, key)

    return upload_crop


_PAD_BY_DEPTH: dict[int, float] = {1: 0.05, 2: 0.08, 3: 0.12}


def _resolve_input_mode(stage: dict) -> str:
    """v0.18.14 / v0.18.15 · 下游阶段投递模式 (crop|geometry): 显式 input.mode 覆盖, 否则按 write.target 推断。

    投递模式是「产物形态」(write.target) 与「投递方式」(supported_inputs) 的二维结果:
    端点 (projects.py) 按子模型 supported_inputs 解析投递方式并烘焙进 ``input.mode`` —
    产几何的子若是 box-prompt seg → ``geometry`` (整图+父框列表); 若是普通检测器 (supported_inputs
    含 crop) → ``crop`` (裁父框 ROI, 检出几何回映回原图)。本函数只读已烘焙的 input.mode,
    缺省 (旧 payload / 端点未烘焙) 回落 write.target 启发式: 产几何→geometry, 产属性→crop。
    """
    explicit = (stage.get("input") or {}).get("mode")
    if explicit in {"crop", "geometry"}:
        return explicit
    target = (stage.get("write") or {}).get("target", "attributes")
    if target in {"geometry", "intermediate"}:
        return "geometry"
    return "crop"


def _resolve_pad(stage: dict, depth: int) -> float:
    """v0.18.14 · crop pad: 显式 roi.pad 优先, 缺省按深度取默认 (越深裁得越松, 抗画质衰减)。"""
    pad = (stage.get("roi") or {}).get("pad")
    if pad is not None:
        return float(pad)
    return _PAD_BY_DEPTH.get(depth, 0.05)


def _compute_stage_depths(stages: list[dict]) -> dict[int, int]:
    """v0.18.14 · 按 parent_stage 链派生每阶段深度 (root=1)。受限树形保证 parent 序号更小。"""
    depth: dict[int, int] = {}
    for s in sorted(stages, key=lambda x: x["stage"]):
        p = s.get("parent_stage")
        depth[s["stage"]] = 1 if p is None else depth.get(p, 1) + 1
    return depth


def _pipeline_topology(stages: list[dict], depths: dict[int, int]) -> list[dict]:
    """v0.18.4 / v0.18.14 · 从 stages 配置派生可审计的 pipeline 拓扑, 落 PredictionMeta.extra。

    每项 ``{stage, parent_stage, depth, label, ml_backend_id, model_id, parent_class_filter,
    write_keys, write_target, target_stage}``, 让「这框的某属性来自哪个 backend/model/阶段」
    及流水线树形结构可追溯。纯派生 (跨 task 不变), 由 _run_task_pipeline 落每条预测。
    """
    topo: list[dict] = []
    for s in stages:
        write = s.get("write") or {}
        topo.append(
            {
                "stage": s.get("stage"),
                "parent_stage": s.get("parent_stage"),
                "depth": depths.get(s.get("stage")),
                "label": s.get("label"),
                "ml_backend_id": s.get("ml_backend_id"),
                "model_id": s.get("model_id"),
                "parent_class_filter": s.get("parent_class_filter") or None,
                "write_keys": write.get("keys") or None,
                "write_target": write.get("target"),
                "target_stage": write.get("target_stage", "root"),
            }
        )
    return topo


async def _run_task_pipeline(
    task,
    stages,
    stage_clients,
    stage_contexts,
    *,
    resolve_url,
    upload_crop=None,
    stage_modes=None,
):
    """v0.18.1 ~ v0.18.14 · 对单个 task 按受限树形拓扑跑各阶段, 返回 (pred_results, pipeline_extra, stage_stats)。

    - 单阶段 (len==1): 等价于原 ``client.predict``, extra=None, stats=None (逐字回归路径)。
    - 多阶段 (v0.18.14 受限树形, max depth 3): root 产框 → 按 ``stage`` 号建 ``stage_outputs`` map,
      每个下游阶段从 ``parent_stage`` 取上游输出 (不再恒为 root), 按投递模式分流:
      - **crop 模式** (write.target=attributes): 加载原图, 按 parent_class_filter + 按深度 pad 裁父框
        ROI 喂下游分类 → label 前缀合并 attributes 进父框 (原地改); 该阶段对孙子暴露 (已富集的) 父框几何。
      - **geometry 模式** (write.target ∈ {geometry, intermediate}): 全图 URL + 父框归一化列表喂下游
        box-seg → 出 polygon (带 parent_box_idx, 原图坐标)。geometry → 追加进预测; intermediate → 仅
        供下游消费不落库。该阶段对下游暴露新几何。
    - v0.18.2: 类别路由 (parent_class_filter)、阶段级失败策略 (on_failure)、逐阶段统计。
    - v0.18.4: crop 经 upload_crop presigned 投递; 并行兄弟同框 crop 复用。
    - v0.18.14: crop min_crop_side_px=32 守卫 (短边过小跳过); extra 落 depth/label/max_depth/target_stage。

    注: 真正的几何 depth-3 (crop 内检测新子物体 + 坐标回映) 不在本版, 见 0.18.15 计划。

    upload_crop: ``(task, box_idx, jpeg_bytes) -> url``; 非 None → presigned 投递, None → data URI。
    stage_modes: ``list[str]`` 与 stages 等长 (按列表位置), 每项 ``"crop"|"geometry"``; None → 全 crop。
    stage_stats: ``{stage号: {...}}`` —— 源阶段 {detected}; 下游 {targeted, ok, failed, skipped_geometry}。
    """
    from app.workers.roi import (
        collect_geometry_shapes,
        crop_inputs_from_boxes,
        geometry_prompts_from_boxes,
        merge_classify_attributes,
        remap_geometry_to_image,
    )
    from app.services.gpu_arbitration.contracts import gpu_arbiter_failure_record

    url = resolve_url(task)
    results = await stage_clients[0].predict(
        [{"id": str(task.id), "file_path": url}], context=stage_contexts[0]
    )
    if len(stages) == 1:
        return results, None, None

    modes = stage_modes or ["crop"] * len(stages)
    depths = _compute_stage_depths(stages)
    # geometry 模式纯坐标换算无需原图; 仅当存在 crop 下游阶段时才加载 (省去无谓全图拉取)。
    needs_image = any(modes[si] == "crop" for si in range(1, len(stages)))
    image = _load_task_image(task) if needs_image else None
    delivery = "presigned" if upload_crop is not None else "data_uri"
    enriched_keys: set[str] = set()
    gpu_arbiter_failures: list[dict] = []
    root_stage = stages[0]["stage"]
    # stats / stage_outputs 均按真实 stage 号键入 (与 meta 对齐, 支持前端按 depth 缩进)。
    stats: dict[int, dict] = {root_stage: {"detected": 0}}
    for si in range(1, len(stages)):
        stats[stages[si]["stage"]] = {
            "targeted": 0,
            "ok": 0,
            "failed": 0,
            "skipped_geometry": 0,
        }

    for pred_result in results:
        root_boxes = pred_result.result
        if not isinstance(root_boxes, list) or not root_boxes:
            continue
        stats[root_stage]["detected"] += len(root_boxes)
        # 每阶段对下游暴露的几何 (按 stage 号): root 暴露检测框, 下游按 parent_stage 取上游输出。
        stage_outputs: dict[int, list] = {root_stage: root_boxes}
        # v0.18.4 · 并行兄弟阶段 target 同一批父框时按 (box_idx, pad) 复用已裁/已上传 crop;
        # depth-3 阶段的子下标语义与 root_boxes 不同 (是中间几何), 按 parent_stage 分桶
        # 避免 (idx, pad) 撞键喂错图 (claude[bot] P1)。
        crop_cache_by_parent: dict[int | None, dict] = {}
        upload_fn = (
            (lambda idx, buf: upload_crop(task, idx, buf))
            if upload_crop is not None
            else None
        )
        dropped: set[int] = set()
        new_shapes: list[
            dict
        ] = []  # geometry-target 阶段产出的 shape, 阶段循环后追加进预测
        for si in range(1, len(stages)):
            stage = stages[si]
            snum = stage["stage"]
            parent_boxes = stage_outputs.get(stage.get("parent_stage"))
            if parent_boxes is None:
                continue  # 父阶段未产出 (上游跳过/失败) → 本阶段无输入, 跳过
            write = stage.get("write") or {}
            target = write.get("target", "attributes")
            write_keys = write.get("keys") or None
            label = stage.get("label")
            pcf = stage.get("parent_class_filter") or None
            if modes[si] == "geometry":
                # geometry 投递: 全图 URL + 父框归一化列表, 下游 box-seg 出 polygon。
                geo = geometry_prompts_from_boxes(parent_boxes, parent_class_filter=pcf)
                stats[snum]["skipped_geometry"] += geo.skipped_geometry
                if not geo.prompts:
                    stage_outputs[snum] = []
                    continue
                stats[snum]["targeted"] += len(geo.prompts)
                try:
                    seg_results = await stage_clients[si].predict(
                        [
                            {
                                "id": str(task.id),
                                "file_path": url,
                                "prompts": geo.prompts,
                            }
                        ],
                        context=stage_contexts[si],
                    )
                    shapes = collect_geometry_shapes(seg_results, parent_boxes)
                    stats[snum]["ok"] += len(shapes)
                    stage_outputs[snum] = shapes
                    # geometry → 落库为候选 shape; intermediate → 仅内部供下游消费, 不落库。
                    if target == "geometry":
                        new_shapes.extend(shapes)
                except Exception as exc:  # noqa: BLE001
                    stats[snum]["failed"] += len(geo.prompts)
                    stage_outputs[snum] = []
                    failure = gpu_arbiter_failure_record(exc)
                    if failure is not None:
                        backend_id = stage.get("ml_backend_id")
                        gpu_arbiter_failures.append(
                            {
                                **failure,
                                "stage": snum,
                                "ml_backend_id": (
                                    str(backend_id) if backend_id is not None else None
                                ),
                                "count": 1,
                            }
                        )
                    logger.warning(
                        "[ai-pre] stage %s 下游 box-seg 失败: %s: %s",
                        snum,
                        type(exc).__name__,
                        exc,
                    )
                continue
            # ── crop 投递 (平台逐父框裁 ROI 喂下游) ──
            # attributes: 下游回属性, 合并进父框。geometry/intermediate: 下游在 crop 上检出
            # 新子物体 (普通检测器, supported_inputs 含 crop), 检出几何回映回原图坐标 (v0.18.15)。
            if image is None:  # crop 阶段必有原图 (needs_image 已保证); 防御性跳过。
                stage_outputs[snum] = parent_boxes
                continue
            pad = _resolve_pad(stage, depths.get(snum, 2))
            parent_key = stage.get("parent_stage")
            crop_cache = crop_cache_by_parent.setdefault(parent_key, {})
            batch = crop_inputs_from_boxes(
                image,
                parent_boxes,
                pad=pad,
                parent_class_filter=pcf,
                delivery=delivery,
                upload_fn=upload_fn,
                cache=crop_cache,
                min_crop_side_px=32,
            )
            stats[snum]["skipped_geometry"] += batch.skipped_geometry
            crop_inputs = batch.inputs
            # 默认 (attributes): 对孙子暴露 (可能已富集属性的) 父框几何。
            # geometry/intermediate: 下面用回映后的检出几何改写 stage_outputs[snum]。
            stage_outputs[snum] = parent_boxes
            if not crop_inputs:
                if target != "attributes":
                    stage_outputs[snum] = []
                continue  # 无符合类别/几何的父框 → 本阶段对本图降级跳过
            targeted = {int(ci["id"]) for ci in crop_inputs}
            stats[snum]["targeted"] += len(targeted)
            try:
                ds_results = await stage_clients[si].predict(
                    crop_inputs, context=stage_contexts[si]
                )
                if target == "attributes":
                    merged = merge_classify_attributes(
                        parent_boxes, ds_results, write_keys=write_keys, label=label
                    )
                    stats[snum]["ok"] += merged
                    stats[snum]["failed"] += len(targeted) - merged
                    if write_keys:
                        prefix = f"{label}_" if label else ""
                        enriched_keys.update(f"{prefix}{k}" for k in write_keys)
                else:
                    # crop-detect: 每个 crop 的检出几何按其 transform 回映回原图坐标
                    # (每个 crop 都裁自原图, transform 即相对原图, 无需链式 compose)。
                    produced: list[dict] = []
                    for cr in ds_results:
                        transform = batch.transforms.get(str(cr.task_id))
                        if transform is None:
                            continue
                        remapped = remap_geometry_to_image(cr.result or [], transform)
                        try:
                            pidx = int(cr.task_id)
                        except (TypeError, ValueError):
                            pidx = None
                        for s in remapped:
                            if pidx is not None:
                                s["parent_box_idx"] = pidx
                        produced.extend(remapped)
                    stats[snum]["ok"] += len(produced)
                    stage_outputs[snum] = produced
                    # geometry → 落库为候选 shape; intermediate → 仅供下游消费, 不落库。
                    if target == "geometry":
                        new_shapes.extend(produced)
            except Exception as exc:  # noqa: BLE001
                # 阶段级失败: keep_parent=保留上游框属性留空; drop_box=丢这些父框。
                stats[snum]["failed"] += len(targeted)
                failure = gpu_arbiter_failure_record(exc)
                if failure is not None:
                    backend_id = stage.get("ml_backend_id")
                    gpu_arbiter_failures.append(
                        {
                            **failure,
                            "stage": snum,
                            "ml_backend_id": (
                                str(backend_id) if backend_id is not None else None
                            ),
                            "count": 1,
                        }
                    )
                if target != "attributes":
                    stage_outputs[snum] = []
                # drop_box 的 targeted 是本阶段 parent_boxes 的下标, 而 dropped 恒作用于
                # root_boxes。仅当父阶段就是 root 时两者下标才对齐; 深层父阶段的 parent_boxes
                # 是中间几何 (与 root_boxes 下标语义不同), 误用会删掉无关的 root 框。校验层已
                # 禁止非-root-父阶段设 drop_box, 此处兜底: 父非 root 时不删 (退化为 keep_parent)。
                if (
                    stage.get("on_failure") == "drop_box"
                    and stage.get("parent_stage") == root_stage
                ):
                    dropped |= targeted
                logger.warning(
                    "[ai-pre] stage %s 下游 crop 投递失败 (on_failure=%s): %s: %s",
                    snum,
                    stage.get("on_failure", "keep_parent"),
                    type(exc).__name__,
                    exc,
                )
        if dropped:
            root_boxes[:] = [b for i, b in enumerate(root_boxes) if i not in dropped]
        if new_shapes:
            root_boxes.extend(new_shapes)
    extra = {
        "pipeline": {
            "stage_count": len(stages),
            "max_depth": max(depths.values()) if depths else 1,
            "enriched_attr_keys": sorted(enriched_keys) or None,
            "stages": _pipeline_topology(stages, depths),
        }
    }
    if gpu_arbiter_failures:
        extra["pipeline"]["gpu_arbiter_failures"] = gpu_arbiter_failures
    return results, extra, stats


async def _run_batch(
    project_id: str,
    ml_backend_id: str,
    task_ids: list[str] | None,
    prompt: str | None = None,
    output_mode: str = "mask",
    batch_id: str | None = None,
    celery_task_id: str | None = None,
    user_id: str | None = None,
    params: dict | None = None,
    predict_mode: str = "skip_predicted",
    model_id: str | None = None,
    task_type: str | None = None,
    model_variants: dict | None = None,
    class_filter: list[int] | None = None,
    pipeline_stages: list[dict] | None = None,
    execution_unit: str | None = None,
    execution_scope: str = "bulk",
    async_job_id: str | None = None,
):
    """v0.9.5 · 批量预标 worker.

    新增参数：
    - prompt: 文本批量预标 prompt（None 时走老的 image-only 批量行为）。
    - output_mode: text 模式输出形态（box / mask / both），仅 prompt 非空生效。
    - batch_id: 跑完后 active 批次自动转 PRE_ANNOTATED；draft 批次 (issue #124)
      保持 draft 不隐式推进，None 则不动状态。
    - celery_task_id (v0.9.8): 用于 _BatchPredictTask.on_failure 回查 async_jobs 行.
    - user_id (v0.10.45): 写 async_jobs owner, 供 /async-jobs owner-scope 列表可见.
    - model_id (v0.14.9): 协议 v2 多模型路由, 非空时写 context["model_id"]。
    - task_type (v0.14.9): 协议 v2 task 别名 ("ocr"/"doc_layout"/"text"), 非空时写
      context["type"]; 让 OCR / 版面分析等非纯文本 task 也能走批量预标。

    v0.10.49 · async_jobs 收敛：async_jobs 升为单一真值，prediction_jobs 专表已删。
    domain 字段（batch_id / prompt / 统计）进 payload/result JSONB。
    """
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    from app.db.enums import BatchStatus, WORKBENCH_AI_EDITABLE_TASK_STATUSES
    from app.db.models.async_job import AsyncJob, AsyncJobStatus
    from app.db.models.ml_backend_registry import MLBackendRegistry as MLBackend
    from app.db.models.project import Project
    from app.db.models.task import Task
    from app.db.models.task_batch import TaskBatch
    from app.db.models.user import User
    from app.services import async_job as async_job_svc
    from app.services.async_job_notify import notify_job_terminal
    from app.services.gpu_arbitration.dispatch import (
        build_gpu_dispatch_context_factory,
    )
    from app.services.gpu_arbitration.contracts import (
        gpu_arbiter_failure_record,
        summarize_gpu_arbiter_failures,
    )
    from app.services.ml_backend import MLBackendService
    from app.services.ml_routing.client import RoutedMLBackendClient
    from app.services.prediction import PredictionService
    from app.services.project_access import is_privileged_for_project
    from app.services.task_lock import TaskLockService
    from app.services.batch_permissions import allows_bulk_preannotation

    engine = create_async_engine(settings.database_url, echo=False)
    SessionLocal = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    started_perf = time.perf_counter()
    success_count = 0
    failed_count = 0
    failed_prediction_ids: list[str] = []
    gpu_arbiter_failures: list[dict] = []
    project_name: str | None = None
    # v0.9.11 · 累加每条 prediction.meta.total_cost; job 完成时写 async_job.result.total_cost.
    # grounded-sam2-backend 当前不返回 cost (留 0.0), LLM-backed backend (sam3 / future) 自然到位.
    running_total_cost = 0.0

    async with SessionLocal() as db:
        backend = await db.get(MLBackend, uuid.UUID(ml_backend_id))
        if not backend:
            if async_job_id is not None:
                try:
                    failed_job_id = uuid.UUID(async_job_id)
                except (TypeError, ValueError):
                    failed_job_id = None
                if failed_job_id is not None:
                    await async_job_svc.mark_failed(
                        db, failed_job_id, error="ML Backend not found"
                    )
                    await notify_job_terminal(db, job_id=failed_job_id)
                    await db.commit()
            _publish_progress(
                project_id, 0, 0, status="error", error="ML Backend not found"
            )
            return
        # registry id 先解析为 requested pool；实际预测再由 RoutedMLBackendClient
        # 在池内选物理实例，Prediction / FailedPrediction 同时保留逻辑池与执行实例。
        source_pool_id = await MLBackendService(db).pool_id_for_registry(backend.id)

        # v0.9.5 / v0.14.9 / v0.14.17 · 构造 /predict context (扁平文本路径 vs v2 结构化路径,
        # 见 _build_predict_context). DINO 阈值取项目级 override.
        project = await db.get(Project, uuid.UUID(project_id))
        if project is not None:
            project_name = project.name
        project_uuid = uuid.UUID(project_id)
        actor: User | None = None
        if user_id:
            actor = await db.get(User, uuid.UUID(user_id))
            if (
                actor is None
                or not actor.is_active
                or project is None
                or not is_privileged_for_project(actor, project)
            ):
                raise ValueError("preannotation actor is unavailable")
        elif async_job_id is not None:
            raise ValueError("preannotation actor is required for a scoped job")

        requested_task_ids = (
            sorted({uuid.UUID(task_id) for task_id in task_ids}, key=str)
            if task_ids is not None
            else None
        )
        precreated_job: AsyncJob | None = None
        if async_job_id is not None:
            try:
                precreated_job_id = uuid.UUID(async_job_id)
            except (TypeError, ValueError) as exc:
                raise ValueError("preannotation async job id is invalid") from exc
            precreated_job = (
                await db.execute(
                    select(AsyncJob)
                    .where(AsyncJob.id == precreated_job_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if (
                precreated_job is None
                or precreated_job.project_id != project_uuid
                or (
                    user_id is not None and precreated_job.user_id != uuid.UUID(user_id)
                )
            ):
                raise ValueError("preannotation async job scope is unavailable")
            # A redelivery/retry for a job that already started must not run the
            # selected tasks a second time. The row lock serializes this check.
            if precreated_job.status != AsyncJobStatus.PENDING.value:
                return
            precreated_job.celery_task_id = celery_task_id
            await async_job_svc.mark_running(
                db, precreated_job.id, celery_task_id=celery_task_id
            )
            await db.commit()
        if batch_id:
            selected_batch = await db.get(TaskBatch, uuid.UUID(batch_id))
            if (
                selected_batch is None
                or selected_batch.project_id != project_uuid
                # issue #124 · 与 API 同源准入: active 或未分派人员的 draft。
                or not allows_bulk_preannotation(selected_batch)
                or selected_batch.admin_locked
            ):
                raise ValueError("preannotation batch scope is unavailable")
        # issue #121 · 工作台单题交互执行 (execution_scope=workbench) 允许 in_progress / draft,
        # 但批次管理员锁、他人编辑锁、终态任务与越权仍在此兜底; bulk 保持原批量前置条件。
        workbench_scope = execution_scope == "workbench"
        if requested_task_ids is not None:
            scope_rows = (
                await db.execute(
                    select(Task, TaskBatch)
                    .outerjoin(TaskBatch, TaskBatch.id == Task.batch_id)
                    .where(
                        Task.project_id == project_uuid,
                        Task.id.in_(requested_task_ids),
                    )
                )
            ).all()
            if len(scope_rows) != len(requested_task_ids):
                raise ValueError("preannotation task scope is unavailable")
            if batch_id and any(
                task.batch_id != uuid.UUID(batch_id) for task, _ in scope_rows
            ):
                raise ValueError("preannotation task scope does not match batch")
            if any(batch is not None and batch.admin_locked for _, batch in scope_rows):
                raise ValueError("preannotation task scope is admin-locked")
            if not workbench_scope:
                if any(task.status != "pending" for task, _ in scope_rows):
                    raise ValueError("preannotation task scope is no longer pending")
                # issue #124 · 与 API 同源准入: active 或未分派人员的 draft;
                # 已分派 draft / 已进入人工流程的批次仍拒绝。
                if any(
                    batch is not None and not allows_bulk_preannotation(batch)
                    for _, batch in scope_rows
                ):
                    raise ValueError(
                        "preannotation task scope does not belong to "
                        "active or unassigned draft batches"
                    )
            elif any(
                task.status not in WORKBENCH_AI_EDITABLE_TASK_STATUSES
                for task, _ in scope_rows
            ):
                # 白名单 (pending / in_progress / rejected), 与 API 同源, 排除 uploading
                # 与终态 (issue #121 review)。
                raise ValueError("preannotation workbench task scope is not editable")
            lock_service = TaskLockService(db)
            actor_id = actor.id if actor is not None else None
            for task, _ in scope_rows:
                # 同一 task 可能有多行残留锁, 必须逐行判断 (active_lock 只看最新一行;
                # issue #121 review)。工作台运行者本人持锁是交互式执行的正常状态。
                locks = await lock_service.active_locks(task.id)
                if workbench_scope and actor_id is not None:
                    locked_by_other = any(lock.user_id != actor_id for lock in locks)
                else:
                    locked_by_other = bool(locks)
                if locked_by_other:
                    raise ValueError("preannotation task scope is locked for editing")
        context = _build_predict_context(
            prompt=prompt,
            output_mode=output_mode,
            params=params,
            model_id=model_id,
            task_type=task_type,
            model_variants=model_variants,
            class_filter=class_filter,
            box_threshold=float(project.box_threshold) if project is not None else None,
            text_threshold=float(project.text_threshold)
            if project is not None
            else None,
        )

        # v0.11.24 · 幂等：skip_predicted 排除已预标 task；append/overwrite 不排除。
        skip_predicted = predict_mode == "skip_predicted"
        # base_conds 不含预标过滤, 供 total==0 时回数候选区分「批次本就空」vs「全已预标被跳过」。
        if requested_task_ids is not None:
            base_conds = [
                Task.project_id == project_uuid,
                Task.id.in_(requested_task_ids),
            ]
            # 工作台单题交互可对 in_progress 任务运行; bulk 仍只捞 pending。
            if not workbench_scope:
                base_conds.append(Task.status == "pending")
        elif batch_id:
            # v0.9.5 · 指定 batch 时仅捞 batch 内 pending tasks
            base_conds = [
                Task.batch_id == uuid.UUID(batch_id),
                Task.status == "pending",
            ]
        else:
            base_conds = [
                Task.project_id == uuid.UUID(project_id),
                Task.status == "pending",
            ]
        sel = select(Task).where(*base_conds)
        if skip_predicted:
            sel = sel.where(Task.total_predictions == 0)
        result = await db.execute(sel)
        tasks = list(result.scalars().all())
        total = len(tasks)

        # v0.11.24 · overwrite：预标前清掉这批 task 的旧预测（保留人工标注），避免叠加。
        # 不重置 task 状态（与删批次不同）——预标只换预测内容、不动流程。
        if predict_mode == "overwrite" and tasks:
            from app.services.batch import BatchService

            overwrite_actor_id = uuid.UUID(user_id) if user_id else None
            await BatchService(db).clean_task_predictions(
                [t.id for t in tasks], actor_id=overwrite_actor_id
            )
            await db.flush()

        # v0.10.49 · async_jobs 单一真值：建 batch_predict 行 (status=running)。
        # domain 字段（batch_id / ml_backend / prompt / total_tasks）进 payload。
        batch = await db.get(TaskBatch, uuid.UUID(batch_id)) if batch_id else None
        # v0.14.18 · payload 记实际模型路由 (溯源 + 任务页展示用了哪个模型)。
        # output_mode 只在它真正被消费的文本 prompt 路径才记 (见 _build_predict_context 的
        # `if prompt:` 分支); 几何/yolo 路径不读 output_mode, 记了会误导成「用了 mask」。
        job_payload: dict = {
            "batch_id": batch_id,
            "batch_display_id": batch.display_id if batch else None,
            "ml_backend_id": ml_backend_id,
            # job payload 保留请求入口 registry 与逻辑池；逐条 Prediction 另记实际执行实例。
            "ml_backend_pool_id": str(source_pool_id) if source_pool_id else None,
            "total_tasks": total,
            "prompt": (prompt or "")[:200],
            "project_display_id": project.display_id if project else None,
            "project_name": project.name if project else None,
            "ml_backend_name": backend.name,
        }
        if model_id:
            job_payload["model_id"] = model_id
        if task_type:
            job_payload["task_type"] = task_type
        if model_variants is not None:
            job_payload["model_variants"] = model_variants
            label = _model_label(model_variants)
            if label:
                job_payload["model_label"] = label
        if prompt:
            job_payload["output_mode"] = output_mode
        if requested_task_ids is not None:
            job_payload["scope"] = {
                "task_ids": [str(task_id) for task_id in requested_task_ids]
            }
            job_payload["task_ids"] = [str(task_id) for task_id in requested_task_ids]
        if precreated_job is None:
            aj = await async_job_svc.create_job(
                db,
                kind="batch_predict",
                user_id=uuid.UUID(user_id) if user_id else None,
                project_id=uuid.UUID(project_id),
                payload=job_payload,
                celery_task_id=celery_task_id,
            )
            await async_job_svc.mark_running(db, aj.id, celery_task_id=celery_task_id)
        else:
            aj = precreated_job
            aj.payload = {**(aj.payload or {}), **job_payload}
        await db.commit()
        async_job_id = aj.id

        job_meta_base = {
            "job_id": str(async_job_id),
            "project_name": project_name,
            "batch_id": batch_id,
        }
        # 开始时点 → 全局通道发 running
        _publish_progress(
            project_id,
            0,
            total,
            status="running",
            job_meta=job_meta_base,
        )

        async def _cancel_requested() -> bool:
            job_for_cancel = await db.get(AsyncJob, async_job_id)
            if job_for_cancel is None:
                return False
            await db.refresh(job_for_cancel)
            if job_for_cancel.status == AsyncJobStatus.CANCELLED.value:
                return True
            return bool((job_for_cancel.payload or {}).get("cancel_requested"))

        async def _finish_cancelled(cancelled_at_index: int) -> None:
            processed_count = success_count + failed_count
            skipped_count = max(0, total - processed_count)
            duration_ms = int((time.perf_counter() - started_perf) * 1000)
            result = {
                "success_count": success_count,
                "failed_count": failed_count,
                "failed_prediction_ids": failed_prediction_ids,
                "done_count": processed_count,
                "skipped_count": skipped_count,
                "cancelled_at_index": cancelled_at_index,
                "duration_ms": duration_ms,
                "total_cost": f"{running_total_cost:.4f}",
            }
            if gpu_arbiter_failures:
                result["gpu_arbiter_failures"] = summarize_gpu_arbiter_failures(
                    gpu_arbiter_failures
                )
            if total > 0:
                await async_job_svc.update_progress(
                    db, async_job_id, int((processed_count / total) * 100)
                )
            await async_job_svc.mark_cancelled(
                db,
                async_job_id,
                result=result,
            )
            await notify_job_terminal(db, job_id=async_job_id)
            await db.commit()
            _publish_progress(
                project_id,
                processed_count,
                total,
                status="cancelled",
                job_meta={
                    **job_meta_base,
                    "success_count": success_count,
                    "failed_count": failed_count,
                    "done_count": processed_count,
                    "skipped_count": skipped_count,
                    "duration_ms": duration_ms,
                },
            )

        if total == 0:
            duration_ms = int((time.perf_counter() - started_perf) * 1000)
            result_payload: dict = {
                "success_count": 0,
                "failed_count": 0,
                "duration_ms": duration_ms,
                "total_cost": "0.0000",
            }
            # skip_predicted 下 total==0 可能是「候选 task 全部已预标被跳过」而非批次本就空。
            # 回数不含预标过滤的候选, >0 即全被跳过 → 标 reason 供前端给明确文案。
            if skip_predicted:
                from sqlalchemy import func

                cnt = await db.execute(
                    select(func.count()).select_from(Task).where(*base_conds)
                )
                skipped_n = int(cnt.scalar_one() or 0)
                if skipped_n > 0:
                    result_payload["skipped_count"] = skipped_n
                    result_payload["reason"] = "all_predicted"
            await async_job_svc.mark_complete(
                db,
                async_job_id,
                result=result_payload,
            )
            await notify_job_terminal(db, job_id=async_job_id)
            await db.commit()
            _publish_progress(
                project_id,
                0,
                0,
                status="completed",
                job_meta=job_meta_base,
            )
            await engine.dispose()
            return

        pred_svc = PredictionService(db)

        from app.services.storage import resolve_task_url as _resolve_task_url

        # v0.18.1 · 阶段化: 归一化 stages (缺省=单阶段, 由平铺参数合成, 与现状逐字等价)。
        # 为每个阶段构造 client + context: 源阶段 (parent_stage=None) 复用上面的 context;
        # 下游阶段不带 prompt (吃 crop 跑分类), 各用本阶段的 model/variant/params。
        if pipeline_stages:
            stages = sorted(pipeline_stages, key=lambda s: s["stage"])
        else:
            stages = [
                {
                    "stage": 0,
                    "ml_backend_id": ml_backend_id,
                    "model_id": model_id,
                    "task_type": task_type,
                    "model_variants": model_variants,
                    "params": params,
                    "class_filter": class_filter,
                    "parent_stage": None,
                    "roi": None,
                    "write": None,
                }
            ]

        # v0.21.7 · 逐帧执行单位: 视频 task 逐帧跑图像 backend (二级 Celery fan-out chord)。
        # 首刀 = 源**单阶段**检测; 多阶段逐帧不在范围 (frame × depth 组合爆炸, 见 plan)。
        # 段任务 + finalize_frame_job 接管抽帧/predict/落库/进度/收尾, 此处只发起并返回。
        if execution_unit == "frame":
            from app.workers.frame_preannotate import dispatch_frame_preannotate

            disp = await dispatch_frame_preannotate(
                db,
                project_id=project_id,
                ml_backend_id=ml_backend_id,
                tasks=tasks,
                stage0=stages[0],
                job_id=str(async_job_id),
                celery_root_task_id=celery_task_id,
            )
            if disp["dispatched"]:
                _publish_progress(
                    project_id,
                    0,
                    disp["total_frames"],
                    status="running",
                    job_meta=job_meta_base,
                )
                await db.commit()
                await engine.dispose()
                return
            # 无可跑帧 (元数据缺失 / 非视频 task) → 空收尾, 不落到图像主循环。
            await async_job_svc.mark_complete(
                db,
                async_job_id,
                result={
                    "success_count": 0,
                    "failed_count": 0,
                    "execution_unit": "frame",
                    "reason": "no_frames",
                },
            )
            await notify_job_terminal(db, job_id=async_job_id)
            await db.commit()
            _publish_progress(
                project_id, 0, 0, status="completed", job_meta=job_meta_base
            )
            await engine.dispose()
            return

        box_thr = float(project.box_threshold) if project is not None else None
        text_thr = float(project.text_threshold) if project is not None else None
        dispatch_context_factory = build_gpu_dispatch_context_factory(SessionLocal)
        stage_clients: list[RoutedMLBackendClient] = []
        stage_contexts: list[dict | None] = []
        # v0.18.14 · 每阶段投递模式 (crop|geometry), 按 write.target 推断 (input.mode 可覆盖)。
        # 源阶段 (stage 0) 模式无意义, 占位 "crop"。
        stage_modes: list[str] = []
        for s in stages:
            if s["parent_stage"] is None:
                stage_clients.append(
                    RoutedMLBackendClient(
                        db,
                        backend,
                        project_id=uuid.UUID(project_id),
                        owner=f"batch:{async_job_id}:stage:{s['stage']}",
                        operation="batch_predict",
                        shadow_session_factory=SessionLocal,
                        dispatch_context_factory=dispatch_context_factory,
                    )
                )
                stage_contexts.append(context)
                stage_modes.append("crop")
            else:
                s_backend = await db.get(MLBackend, uuid.UUID(s["ml_backend_id"]))
                stage_clients.append(
                    RoutedMLBackendClient(
                        db,
                        s_backend,
                        project_id=uuid.UUID(project_id),
                        owner=f"batch:{async_job_id}:stage:{s['stage']}",
                        operation="batch_predict",
                        shadow_session_factory=SessionLocal,
                        dispatch_context_factory=dispatch_context_factory,
                    )
                )
                stage_modes.append(_resolve_input_mode(s))
                stage_contexts.append(
                    _build_predict_context(
                        prompt=None,
                        output_mode=output_mode,
                        params=s.get("params"),
                        model_id=s.get("model_id"),
                        task_type=s.get("task_type"),
                        model_variants=s.get("model_variants"),
                        class_filter=s.get("class_filter"),
                        box_threshold=box_thr,
                        text_threshold=text_thr,
                    )
                )

        # v0.18.4 · 多阶段时构造 crop 上传器 (presigned 投递, 对所有下游后端通用)。
        # 单阶段无下游 crop, 不建 (省去对象存储往返)。
        crop_uploader = None
        if len(stages) > 1:
            from app.services.storage import StorageService

            crop_uploader = _make_crop_uploader(StorageService(), str(async_job_id))

        retry_request_context = _retryable_request_context(stage_contexts[0])
        if stage_contexts[0] is not None and retry_request_context is None:
            logger.warning(
                "batch_predict retry context exceeds %d bytes; omitting from failures job=%s",
                _MAX_FAILED_PREDICTION_CONTEXT_BYTES,
                async_job_id,
            )

        # v0.18.2 · 逐阶段统计累加器 (跨 task 汇总): {stage_idx: {detected/targeted/ok/failed}}。
        pipeline_stage_totals: dict[int, dict] = {}

        def _accumulate_stage_stats(per_task: dict | None) -> None:
            if not per_task:
                return
            for sidx, s in per_task.items():
                bucket = pipeline_stage_totals.setdefault(sidx, {})
                for k, v in s.items():
                    bucket[k] = bucket.get(k, 0) + v

        for i, task in enumerate(tasks):
            if await _cancel_requested():
                await _finish_cancelled(cancelled_at_index=i)
                await engine.dispose()
                return

            # Reauthorize the initiating actor against fresh state before each
            # final write: a long inference must not keep writing after the
            # actor's account or project authority was revoked.  A bounded
            # system job (actor is None) keeps its own explicit authority.
            if actor is not None and await _preannotation_actor_revoked(
                db, actor, project_uuid
            ):
                await async_job_svc.mark_failed(
                    db, async_job_id, error="preannotation_actor_revoked"
                )
                await notify_job_terminal(db, job_id=async_job_id)
                await db.commit()
                _publish_progress(
                    project_id,
                    i,
                    total,
                    status="failed",
                    error="permission_revoked",
                    job_meta=job_meta_base,
                )
                await engine.dispose()
                return

            try:
                results, pipeline_extra, stage_stats = await _run_task_pipeline(
                    task,
                    stages,
                    stage_clients,
                    stage_contexts,
                    resolve_url=_resolve_task_url,
                    upload_crop=crop_uploader,
                    stage_modes=stage_modes,
                )
                _accumulate_stage_stats(stage_stats)
                gpu_arbiter_failures.extend(
                    (pipeline_extra or {})
                    .get("pipeline", {})
                    .get("gpu_arbiter_failures", [])
                )
                executed_backend_id = stage_clients[0].last_instance_id or backend.id
                executed_pool_id = stage_clients[0].pool_id or source_pool_id
                # Re-check under locks after inference: a revocation landing
                # mid-run must not let this write commit.  The account/project
                # share locks are held until the commit below, so a concurrent
                # disable or ownership transfer cannot interleave.
                if actor is not None and not await _hold_preannotation_authority(
                    db, actor, project_uuid
                ):
                    await db.rollback()
                    await async_job_svc.mark_failed(
                        db, async_job_id, error="preannotation_actor_revoked"
                    )
                    await notify_job_terminal(db, job_id=async_job_id)
                    await db.commit()
                    _publish_progress(
                        project_id,
                        i,
                        total,
                        status="failed",
                        error="permission_revoked",
                        job_meta=job_meta_base,
                    )
                    await engine.dispose()
                    return
                for pred_result in results:
                    await pred_svc.create_from_ml_result(
                        task_id=task.id,
                        project_id=uuid.UUID(project_id),
                        ml_backend_id=executed_backend_id,
                        result=pred_result.result,
                        score=pred_result.score,
                        model_version=pred_result.model_version,
                        inference_time_ms=pred_result.inference_time_ms,
                        token_meta=pred_result.meta,
                        pipeline_extra=pipeline_extra,
                        ml_backend_pool_id=executed_pool_id,
                    )
                    # v0.9.11 · 单条 cost 累加到 job 级总费用
                    if pred_result.meta:
                        cost = pred_result.meta.get("total_cost")
                        if cost is not None:
                            running_total_cost += float(cost)
                await db.commit()
                success_count += 1
            except Exception as exc:
                gpu_arbiter_error = gpu_arbiter_failure_record(exc)
                if gpu_arbiter_error is not None:
                    gpu_arbiter_failures.append(gpu_arbiter_error)
                failure_extra: dict = {}
                if retry_request_context is not None:
                    failure_extra["request_context"] = retry_request_context
                if gpu_arbiter_error is not None:
                    failure_extra["gpu_arbiter_error"] = gpu_arbiter_error
                failed = await pred_svc.create_failed(
                    task_id=task.id,
                    project_id=uuid.UUID(project_id),
                    ml_backend_id=stage_clients[0].last_instance_id or backend.id,
                    error_type=(
                        gpu_arbiter_error["error_code"]
                        if gpu_arbiter_error is not None
                        else type(exc).__name__
                    ),
                    message=(
                        gpu_arbiter_error["message"]
                        if gpu_arbiter_error is not None
                        else str(exc)
                    ),
                    extra=failure_extra or None,
                    ml_backend_pool_id=stage_clients[0].pool_id or source_pool_id,
                )
                failed_prediction_ids.append(str(failed.id))
                await db.commit()
                failed_count += 1

            # v0.18.6 · 逐阶段累加快照随进度推送 (多阶段才有), 让前端运行中实时更新卡上徽标。
            # 按 5% 步长 (或末条) 节流, 避免每条都塞快照; 阶段数少, payload 增量可忽略。
            pct = int(((i + 1) / total) * 100) if total > 0 else 0
            stage_step = pct % 5 == 0 or (i + 1) == total
            _publish_progress(
                project_id,
                i + 1,
                total,
                pipeline_stages=(
                    _stage_totals_snapshot(pipeline_stage_totals)
                    if pipeline_stage_totals and stage_step
                    else None
                ),
            )

            # v0.10.16 · async_jobs 进度（每 5% 步长写一次，避免每条都 DB write）
            if total > 0:
                if stage_step:
                    try:
                        await async_job_svc.update_progress(db, async_job_id, pct)
                        await db.commit()
                    except Exception:
                        await db.rollback()

        if await _cancel_requested():
            await _finish_cancelled(cancelled_at_index=success_count + failed_count)
            await engine.dispose()
            return

        # B-45 · 全部子项失败 → job 视为失败（避免「失败也显示已完成」），
        # 不推进 batch 状态、终态走 failed 分支。
        all_failed = success_count == 0 and failed_count > 0

        # v0.9.5 · 跑完自动 active → pre_annotated（仅当指定 batch + 当前还在 active 时）
        # issue #124 · 未分派人员的 draft 批次跑完保持 draft：不隐式推进生命周期、
        # 不隐式分派人员；预测挂在 task 上保留，管理员随后正常分派并 draft → active
        # 激活即可进入人工流程（激活不清预测）。all_failed 时任何状态都不推进。
        if batch_id and not all_failed:
            batch = await db.get(TaskBatch, uuid.UUID(batch_id))
            if batch and batch.status == BatchStatus.ACTIVE:
                batch.status = BatchStatus.PRE_ANNOTATED
                await db.commit()

        # v0.10.49 · 结束时点 → 写 async_job final stats (result JSONB)
        duration_ms = int((time.perf_counter() - started_perf) * 1000)
        result_stats = {
            "success_count": success_count,
            "failed_count": failed_count,
            "failed_prediction_ids": failed_prediction_ids,
            "duration_ms": duration_ms,
            # v0.9.11 · total_cost 接通 PredictionMeta.total_cost 累加
            "total_cost": f"{running_total_cost:.4f}",
        }
        # v0.18.2 · 多阶段预标: 逐阶段统计 (检出框数 / 各下游富集成功失败), 供前端逐阶段徽标。
        # v0.18.6 · 与运行中实时快照共用 _stage_totals_snapshot, 终态为最终真值。
        if pipeline_stage_totals:
            result_stats["pipeline_stages"] = _stage_totals_snapshot(
                pipeline_stage_totals
            )
        if gpu_arbiter_failures:
            result_stats["gpu_arbiter_failures"] = summarize_gpu_arbiter_failures(
                gpu_arbiter_failures
            )
        if all_failed:
            await async_job_svc.mark_failed(
                db,
                async_job_id,
                error=f"全部 {failed_count} 条预标失败",
                result=result_stats,
            )
        else:
            await async_job_svc.mark_complete(
                db,
                async_job_id,
                result=result_stats,
            )
        await notify_job_terminal(db, job_id=async_job_id)
        await db.commit()

        _publish_progress(
            project_id,
            total,
            total,
            status="failed" if all_failed else "completed",
            job_meta={
                **job_meta_base,
                "success_count": success_count,
                "failed_count": failed_count,
                "duration_ms": duration_ms,
            },
        )

    await engine.dispose()


async def _mark_job_failed(celery_task_id: str, error_message: str) -> None:
    """v0.10.49 · _BatchPredictTask.on_failure 回查 async_jobs 行写错误.

    任务级未捕获异常（dispatch TypeError / 内部 raise / Celery retry 耗尽）走这条路；
    job 通过 celery_task_id 反查（worker 创建 async_job 时已存）。"""
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    from app.services import async_job as async_job_svc
    from app.services.async_job_notify import notify_job_terminal

    engine = create_async_engine(settings.database_url, echo=False)
    SessionLocal = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    try:
        async with SessionLocal() as db:
            aj = await async_job_svc.find_by_celery_task_id(db, celery_task_id)
            if aj is None:
                return
            await async_job_svc.mark_failed(db, aj.id, error=error_message)
            await notify_job_terminal(db, job_id=aj.id)
            await db.commit()
    finally:
        await engine.dispose()


class _BatchPredictTask(celery_app.Task):
    """B-1: dispatch 阶段（如 TypeError 关键字不识别）或 body 内未捕获异常都推到 WS,
    避免前端停在「已排队」状态。args[0] 是 project_id。
    v0.10.49: 同步把 async_jobs 行翻成 status='failed'。"""

    def on_failure(self, exc, task_id, args, kwargs, einfo):  # noqa: ARG002
        project_id = kwargs.get("project_id") or (args[0] if args else None)
        error_message = f"{type(exc).__name__}: {exc}"
        if project_id:
            try:
                _publish_progress(
                    str(project_id),
                    0,
                    0,
                    status="error",
                    error=error_message,
                    job_meta={"job_celery_task_id": task_id},
                )
            except Exception:
                pass
        try:
            asyncio.run(_mark_job_failed(task_id, error_message))
        except Exception:
            pass


@celery_app.task(
    bind=True,
    base=_BatchPredictTask,
    max_retries=3,
    default_retry_delay=30,
)
def batch_predict(
    self,
    project_id: str,
    ml_backend_id: str,
    task_ids: list[str] | None = None,
    prompt: str | None = None,
    output_mode: str = "mask",
    batch_id: str | None = None,
    user_id: str | None = None,
    params: dict | None = None,
    predict_mode: str = "skip_predicted",
    model_id: str | None = None,
    task_type: str | None = None,
    model_variants: dict | None = None,
    class_filter: list[int] | None = None,
    pipeline_stages: list[dict] | None = None,
    execution_unit: str | None = None,
    execution_scope: str = "bulk",
    async_job_id: str | None = None,
):
    asyncio.run(
        _run_batch(
            project_id,
            ml_backend_id,
            task_ids,
            prompt=prompt,
            output_mode=output_mode,
            batch_id=batch_id,
            celery_task_id=self.request.id,
            user_id=user_id,
            params=params,
            predict_mode=predict_mode,
            model_id=model_id,
            task_type=task_type,
            model_variants=model_variants,
            class_filter=class_filter,
            pipeline_stages=pipeline_stages,
            execution_unit=execution_unit,
            execution_scope=execution_scope,
            async_job_id=async_job_id,
        )
    )
