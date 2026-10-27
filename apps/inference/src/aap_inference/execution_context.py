"""Portable canonical model context construction."""

from __future__ import annotations

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
    """Build the canonical backend context while preserving null/empty semantics."""
    semantic = normalize_task_fields(
        {"task": task, "task_type": task_type, "prompt_type": prompt_type}
    )
    visual_task = semantic.get("task")
    prompt_type = semantic.get("prompt_type")
    if frozen_model_ref:
        controlled_task = normalize_task_fields(
            {"task": frozen_model_ref.get("task", "detection")}
        )["task"]
        if visual_task not in {None, controlled_task} or prompt or prompt_type:
            raise ValueError(
                "Controlled models require their frozen task without prompts"
            )
        if class_filter and any(
            type(index) is not int or not 0 <= index < len(frozen_model_ref["classes"])
            for index in class_filter
        ):
            raise ValueError("Controlled class filter is outside native classes")
        context = {
            "task": controlled_task,
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
                key: value
                for key, value in (params or {}).items()
                if value is not None and key not in reserved
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
                key: value
                for key, value in (params or {}).items()
                if value is not None and key not in reserved
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
    """Derive the existing display label from an explicit model variant mapping."""
    if not model_variants:
        return None
    series = model_variants.get("series")
    size = model_variants.get("size")
    if series and size:
        return f"{series}{size}"
    values = [str(value) for value in model_variants.values() if value]
    return "".join(values) or None
