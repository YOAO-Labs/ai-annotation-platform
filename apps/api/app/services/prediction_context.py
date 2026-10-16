from __future__ import annotations

import io

from aap_task_contract import normalize_task_fields


def build_predict_context(
    *,
    prompt: str | None,
    output_mode: str,
    params: dict | None,
    model_id: str | None,
    task_type: str | None = None,
    model_variants: dict | None = None,
    class_filter: list[int] | None = None,
    box_threshold: float | None = None,
    text_threshold: float | None = None,
    frozen_model_ref: dict | None = None,
    task: str | None = None,
    prompt_type: str | None = None,
) -> dict | None:
    """Build canonical context; legacy task_type is a compatibility input only."""
    semantic = normalize_task_fields(
        {"task": task, "task_type": task_type, "prompt_type": prompt_type}
    )
    visual_task = semantic.get("task")
    prompt_type = semantic.get("prompt_type")
    if frozen_model_ref:
        if visual_task not in {None, "detect"} or prompt or prompt_type:
            raise ValueError(
                "Controlled models require the detect task without prompts"
            )
        if class_filter and any(
            type(i) is not int or not 0 <= i < len(frozen_model_ref["classes"])
            for i in class_filter
        ):
            raise ValueError("Controlled class filter is outside native classes")
        context = {
            "task": "detect",
            "model_id": f"trained:{frozen_model_ref['model_version_id']}",
            "model_variants": {},
            "params": params or {},
            "frozen_model_ref": frozen_model_ref,
        }
    elif prompt:
        context = {"type": prompt_type or "text", "text": prompt, "output": output_mode}
        if visual_task:
            context["task"] = visual_task
        if box_threshold is not None:
            context["box_threshold"] = box_threshold
            context["text_threshold"] = text_threshold
        reserved = {
            "task",
            "task_type",
            "prompt_type",
            "type",
            "text",
            "output",
            "model_id",
            "model_variants",
            "frozen_model_ref",
        }
        context.update(
            {
                k: v
                for k, v in (params or {}).items()
                if v is not None and k not in reserved
            }
        )
        if model_variants:
            context["model_variants"] = model_variants
    elif model_variants is not None:
        context = {"model_variants": model_variants, "params": params or {}}
        if visual_task or not model_id:
            context["task"] = visual_task or "detect"
        if prompt_type:
            context["type"] = prompt_type
    elif visual_task or model_id or prompt_type:
        context = {}
        if visual_task:
            context["task"] = visual_task
        if prompt_type:
            context["type"] = prompt_type
        reserved = {
            "task",
            "task_type",
            "prompt_type",
            "type",
            "model_id",
            "frozen_model_ref",
        }
        context.update(
            {
                k: v
                for k, v in (params or {}).items()
                if v is not None and k not in reserved
            }
        )
    else:
        return None
    if model_id and not frozen_model_ref:
        context["model_id"] = model_id
    if class_filter:
        context["classes"] = class_filter
    return context


def model_label(model_variants: dict | None) -> str | None:
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


def load_task_image(task):
    """Read the original task image from object storage as an RGB PIL Image for ROI crops.

    Read through StorageService's boto3 client and internal endpoint, not through a presigned
    URL or host rewrite, which may be unreachable from workers.
    """
    from PIL import Image

    from app.services.storage import StorageService

    storage = StorageService()
    bucket = storage.datasets_bucket if task.dataset_item_id else storage.bucket
    obj = storage.client.get_object(Bucket=bucket, Key=task.file_path)
    raw = obj["Body"].read()
    return Image.open(io.BytesIO(raw)).convert("RGB")
