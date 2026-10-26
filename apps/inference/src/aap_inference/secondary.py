"""Portable selected-annotation secondary prediction transformations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from aap_inference_contract.execution import (
    ModelOperationResult,
    freeze_json,
    thaw_json,
)

from .execution_context import build_predict_context
from .roi import merge_classify_attributes, remap_geometry_to_image


@dataclass(frozen=True, slots=True)
class SecondaryInferenceOutput:
    write_target: Literal["attributes", "geometry"]
    model_ref: tuple[tuple[str, str | None], ...]
    attributes: tuple[tuple[str, Any], ...]
    geometry_shapes: tuple[Any, ...]


def select_text_output_mode(
    health_meta: Mapping[str, Any] | None, model_id: str | None
) -> str:
    """Select the established text-output mode from the chosen registry facts."""
    capabilities = (health_meta or {}).get("capabilities") or {}
    for model in capabilities.get("models") or ():
        if model.get("id") != model_id:
            continue
        outputs = model.get("supported_text_outputs") or ()
        if "box" in outputs:
            return "box"
        for candidate in outputs:
            if candidate in {"box", "mask", "both"}:
                return candidate
        break
    return "mask"


def build_secondary_context(
    *,
    health_meta: Mapping[str, Any] | None,
    prompt: str | None,
    params: Mapping[str, Any] | None,
    model_id: str | None,
    task: str | None,
    task_type: str | None = None,
    prompt_type: str | None = None,
    model_variants: Mapping[str, Any] | None = None,
    class_filter: list[int] | None = None,
) -> dict[str, Any] | None:
    """Build the same canonical context used by secondary prompt inference."""
    return build_predict_context(
        prompt=prompt,
        output_mode=select_text_output_mode(health_meta, model_id),
        params=None if params is None else dict(params),
        model_id=model_id,
        task=task,
        task_type=task_type,
        prompt_type=prompt_type,
        model_variants=None if model_variants is None else dict(model_variants),
        class_filter=class_filter,
    )


def apply_secondary_result(
    ls_box: dict[str, Any],
    operation: ModelOperationResult,
    *,
    write_target: Literal["attributes", "geometry"],
    model_id: str | None,
    write_keys: list[str] | None = None,
    label: str | None = None,
    transform: Mapping[str, Any] | None = None,
) -> SecondaryInferenceOutput:
    """Map an immutable model result into existing Labeling secondary facts."""
    model_ref = (
        ("backend_id", str(operation.provenance.instance_id)),
        ("model_id", model_id),
    )
    if write_target == "attributes":
        merge_classify_attributes(
            [ls_box], operation.results, write_keys=write_keys, label=label
        )
        attributes = ls_box.get("attributes") or {}
        return SecondaryInferenceOutput(
            write_target="attributes",
            model_ref=model_ref,
            attributes=tuple(
                (key, freeze_json(value)) for key, value in attributes.items()
            ),
            geometry_shapes=(),
        )
    raw_shapes = [
        shape
        for result in operation.results
        for shape in thaw_json(result.result)
        if isinstance(shape, dict)
    ]
    remapped = (
        remap_geometry_to_image(raw_shapes, dict(transform))
        if transform is not None
        else []
    )
    return SecondaryInferenceOutput(
        write_target="geometry",
        model_ref=model_ref,
        attributes=(),
        geometry_shapes=tuple(freeze_json(shape) for shape in remapped),
    )
