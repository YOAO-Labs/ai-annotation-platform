"""Safe platform boundary for native single-frame Mask interactions."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from aap_protocol_v2 import (
    MAX_MASK_DIMENSION,
    MAX_MASK_PIXELS,
    MAX_MASK_RUNS,
    MAX_RLE_OBJECT_BYTES,
    MaskPromptPayload,
    MaskInteractionDiagnostic,
    NativeMaskCandidate,
    ScribblePrompt,
    native_mask_candidate_id,
    normalize_context_model_variants,
)
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.services.ai_mask_session import AiMaskSessionError, verify_ai_mask_session


class _MaskCompanionBoxValue(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    x: float = Field(ge=0.0, lt=1.0)
    y: float = Field(ge=0.0, lt=1.0)
    width: float = Field(gt=0.0, le=1.0)
    height: float = Field(gt=0.0, le=1.0)
    rectanglelabels: list[str] = Field(min_length=1, max_length=1)

    @model_validator(mode="after")
    def _validate_bounds_and_label(self) -> "_MaskCompanionBoxValue":
        if self.x + self.width > 1.0 + 1e-9 or self.y + self.height > 1.0 + 1e-9:
            raise ValueError("box must stay inside normalized image bounds")
        if not 1 <= len(self.rectanglelabels[0]) <= 128:
            raise ValueError("box must have one non-empty label <= 128 chars")
        return self


class _MaskCompanionBox(BaseModel):
    """A detection box paired with the next native exemplar Mask."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    type: Literal["rectanglelabels"]
    value: _MaskCompanionBoxValue
    score: float | None = Field(default=None, ge=0.0, le=1.0)


def _error(status_code: int, reason: str, message: str, **detail: Any) -> None:
    raise HTTPException(
        status_code=status_code,
        detail={"reason": reason, "message": message, **detail},
    )


def prepare_interactive_context(
    context: dict[str, Any],
    capabilities: dict[str, Any] | None,
    *,
    task_id: str,
    frame_index: int | None = None,
    requested_backend_id: str | None = None,
    reference_input: dict | None = None,
    original_input: dict | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Resolve one image-interactive model and bind its safe prompt contract."""

    if reference_input is not None and original_input is not None:
        _error(409, "mask_source_identity_conflict", "Mask source identities conflict")
    if original_input is not None:
        from app.services.managed_image_inference import normalize_identity

        original_input = normalize_identity(original_input)
        if original_input["task_id"] != task_id or frame_index is not None:
            _error(
                409, "mask_source_identity_conflict", "Mask source does not match task"
            )
    prepared = dict(context)
    # The shared backend may still serve older API instances that only accept
    # Masks. Only this platform boundary negotiates the paired response.
    prepared.pop("native_mask_companion_boxes", None)
    uses_mask_contract = any(
        key in prepared
        for key in (
            "mask_prompt",
            "mask_input",
            "scribbles",
            "output_geometry",
            "model_id",
        )
    ) or prepared.get("type") in {"point", "interactive_box", "mask", "scribble"}
    if not uses_mask_contract:
        return prepared, None

    prompt = prepared.get("type")
    if not isinstance(prompt, str) or not prompt:
        _error(422, "unsupported_prompt", "context.type must name a prompt")
    output_geometry = prepared.get("output_geometry", "polygon")
    if output_geometry not in ("polygon", "mask"):
        _error(
            422,
            "unsupported_output_geometry",
            "output_geometry must be polygon or mask",
            requested=output_geometry,
        )

    required_inputs = {
        "point": "point_prompt",
        "interactive_box": "bbox_prompt",
        "mask": "mask_prompt",
        "scribble": "scribble_prompt",
    }
    required = [required_inputs[prompt]] if prompt in required_inputs else []
    if "mask_prompt" in prepared and "mask_prompt" not in required:
        required.append("mask_prompt")

    models = [
        model
        for model in (capabilities or {}).get("models", [])
        if isinstance(model, dict)
        and model.get("task") == "interactive_seg"
        and model.get("is_interactive") is True
    ]
    if not models:
        _error(
            503,
            "capability_unavailable",
            "interactive model capability is unavailable",
        )

    requested_model_id = prepared.get("model_id")
    target: dict[str, Any] | None = None
    if requested_model_id is not None:
        target = next(
            (model for model in models if model.get("id") == requested_model_id),
            None,
        )
        if target is None:
            _error(
                422,
                "unknown_model",
                "target interactive model is not available",
                model_id=requested_model_id,
            )
    else:
        prompt_models = [
            model
            for model in models
            if prompt in (model.get("supported_prompts") or [])
        ]
        output_models = [
            model
            for model in prompt_models
            if output_geometry in (model.get("supported_geometric_outputs") or [])
        ]
        input_models = [
            model
            for model in output_models
            if all(item in (model.get("supported_inputs") or []) for item in required)
        ]
        if len(input_models) == 1:
            target = input_models[0]
        elif len(input_models) > 1:
            _error(
                422,
                "ambiguous_model",
                "multiple interactive models match the request",
                model_ids=[model.get("id") for model in input_models],
            )
        elif not prompt_models:
            _error(
                422,
                "unsupported_prompt",
                "no interactive model supports the requested prompt",
                requested=prompt,
            )
        elif not output_models:
            sole_prompt_model = prompt_models[0] if len(prompt_models) == 1 else None
            _error(
                422,
                "unsupported_output_geometry",
                "no interactive model supports the requested output geometry",
                requested=output_geometry,
                model_id=sole_prompt_model.get("id") if sole_prompt_model else None,
                supported_geometric_outputs=(
                    list(sole_prompt_model.get("supported_geometric_outputs") or [])
                    if sole_prompt_model
                    else []
                ),
            )
        elif required:
            sole_output_model = output_models[0] if len(output_models) == 1 else None
            _error(
                422,
                "unsupported_input",
                "no interactive model supports the required prompt inputs",
                missing_inputs=required,
                model_id=sole_output_model.get("id") if sole_output_model else None,
                supported_inputs=(
                    list(sole_output_model.get("supported_inputs") or [])
                    if sole_output_model
                    else []
                ),
            )
        elif len(output_models) == 1:
            target = output_models[0]
        else:
            _error(
                422,
                "ambiguous_model",
                "multiple interactive models match the request",
                model_ids=[model.get("id") for model in output_models],
            )

    assert target is not None
    supported_prompts = list(target.get("supported_prompts") or [])
    if prompt not in supported_prompts:
        _error(
            422,
            "unsupported_prompt",
            "target model does not support the requested prompt",
            model_id=target.get("id"),
            requested=prompt,
            supported_prompts=supported_prompts,
        )
    supported_outputs = list(target.get("supported_geometric_outputs") or [])
    if output_geometry not in supported_outputs:
        _error(
            422,
            "unsupported_output_geometry",
            "target model does not support the requested output geometry",
            model_id=target.get("id"),
            requested=output_geometry,
            supported_geometric_outputs=supported_outputs,
        )
    supported_inputs = list(target.get("supported_inputs") or [])
    missing_inputs = [item for item in required if item not in supported_inputs]
    if missing_inputs:
        _error(
            422,
            "unsupported_input",
            "target model does not support the requested prompt input",
            model_id=target.get("id"),
            missing_inputs=missing_inputs,
            supported_inputs=supported_inputs,
        )

    try:
        prepared, _deprecated = normalize_context_model_variants(prepared)
    except ValueError:
        _error(
            422,
            "invalid_model_variants",
            "model variants failed schema validation",
        )
    for legacy_field in ("variants", "sam_variant", "dino_variant", "model_variant"):
        prepared.pop(legacy_field, None)
    default_variants = target.get("default_variants")
    effective_variants = {
        str(key): str(value)
        for key, value in (
            default_variants.items() if isinstance(default_variants, dict) else []
        )
        if value is not None
    }
    requested_variants = prepared.get("model_variants")
    if isinstance(requested_variants, dict):
        effective_variants.update(
            {
                str(key): str(value)
                for key, value in requested_variants.items()
                if value is not None
            }
        )
    if effective_variants:
        prepared["model_variants"] = dict(sorted(effective_variants.items()))
    else:
        prepared.pop("model_variants", None)

    mask_prompt = prepared.get("mask_prompt")
    if mask_prompt is not None:
        try:
            prepared["mask_prompt"] = MaskPromptPayload.model_validate(
                mask_prompt
            ).model_dump(mode="json")
        except ValidationError:
            _error(
                422,
                "invalid_mask_prompt",
                "Mask prompt failed schema validation",
            )
    if prompt == "mask" and "mask_prompt" not in prepared:
        _error(422, "invalid_mask_prompt", "mask prompt is required")
    if prompt == "scribble":
        try:
            scribble = ScribblePrompt.model_validate(
                {
                    "type": "scribble",
                    "scribbles": prepared.get("scribbles"),
                    "output_geometry": output_geometry,
                    "mask_prompt": prepared.get("mask_prompt"),
                }
            )
        except ValidationError:
            _error(
                422,
                "invalid_scribble_prompt",
                "scribble prompt failed schema validation",
            )
        prepared["scribbles"] = [
            stroke.model_dump(mode="json") for stroke in scribble.scribbles
        ]
        if (
            "mask_prompt" not in prepared
            and "mask_input" not in prepared
            and all(stroke.polarity == 0 for stroke in scribble.scribbles)
        ):
            _error(
                422,
                "negative_scribble_requires_seed",
                "negative-only scribble requires a Mask seed",
            )

    if prepared.get("multimask_output") is True and (
        "mask_prompt" in prepared or "mask_input" in prepared
    ):
        _error(
            422,
            "invalid_multimask_seed",
            "multimask_output cannot be combined with a Mask seed",
        )

    model_id = str(target.get("id"))
    prepared["model_id"] = model_id
    if (
        prompt == "exemplar"
        and prepared.get("output") == "both"
        and output_geometry == "mask"
    ):
        prepared["native_mask_companion_boxes"] = True
    session_origin: dict[str, Any] | None = None
    if "mask_input" in prepared:
        token = prepared.get("mask_input")
        try:
            claims = verify_ai_mask_session(token) if isinstance(token, str) else None
        except AiMaskSessionError as exc:
            _error(409, exc.reason, str(exc))
        if claims is None:
            _error(422, "invalid_mask_session", "Mask session is invalid")
        if original_input is not None:
            from app.services.content_snapshot import ContentError

            try:
                normalize_identity(claims.get("original_input"))
            except ContentError:
                _error(
                    409,
                    "mask_session_mismatch",
                    "Mask session does not match this prompt",
                )
        mask_prompt = prepared.get("mask_prompt")
        source = (
            {
                "source_annotation_id": mask_prompt.get("source_annotation_id"),
                "source_version": mask_prompt.get("source_version"),
                "source_digest": mask_prompt.get("source_digest"),
            }
            if isinstance(mask_prompt, dict)
            else None
        )
        expected_session = {
            "task_id": task_id,
            "frame_index": frame_index,
            "requested_backend_id": requested_backend_id,
            "model_id": model_id,
            "source": source,
            "model_variants": prepared.get("model_variants") or {},
            "reference_input": reference_input,
            "original_input": original_input,
        }
        if any(claims.get(key) != value for key, value in expected_session.items()):
            _error(
                409, "mask_session_mismatch", "Mask session does not match this prompt"
            )
        prepared["mask_input"] = claims["raw"]
        session_origin = {
            "origin_prompt_revision": claims.get("origin_prompt_revision"),
            "candidate_id": claims.get("candidate_id"),
            "candidate_index": claims.get("candidate_index"),
        }
    if output_geometry == "mask":
        revision_context = dict(prepared)
        revision_context.pop("prompt_revision", None)
        revision_context.pop("mask_input", None)
        mask_prompt = revision_context.get("mask_prompt")
        if isinstance(mask_prompt, dict):
            revision_context["mask_prompt"] = {
                "source_annotation_id": mask_prompt.get("source_annotation_id"),
                "source_version": mask_prompt.get("source_version"),
                "source_digest": mask_prompt.get("source_digest"),
            }
        revision = {
            "task_id": task_id,
            "frame_index": frame_index,
            "model_id": model_id,
            "context": revision_context,
            "session_origin": session_origin,
            "reference_input": reference_input,
        }
        if original_input is not None:
            revision["original_input"] = original_input
        revision_bytes = json.dumps(
            revision,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        prepared["prompt_revision"] = hashlib.sha256(revision_bytes).hexdigest()
    return prepared, model_id


def _precheck_rle_budget(raw_candidate: Any, candidate_index: int) -> None:
    if not isinstance(raw_candidate, dict):
        return
    value = raw_candidate.get("value")
    rle = value.get("rle") if isinstance(value, dict) else None
    if not isinstance(rle, dict):
        return
    size = rle.get("size")
    counts = rle.get("counts")
    if (
        isinstance(size, list)
        and len(size) == 2
        and all(type(value) is int for value in size)
    ):
        height, width = size
        if (
            height > MAX_MASK_DIMENSION
            or width > MAX_MASK_DIMENSION
            or (height > 0 and width > 0 and height * width > MAX_MASK_PIXELS)
        ):
            _error(
                413,
                "mask_payload_too_large",
                "native Mask dimensions exceed the payload budget",
                candidate_index=candidate_index,
            )
    if isinstance(counts, list) and len(counts) > MAX_MASK_RUNS:
        _error(
            413,
            "mask_payload_too_large",
            "native Mask run count exceeds the payload budget",
            candidate_index=candidate_index,
        )
    try:
        canonical_size = len(
            json.dumps(
                {
                    "encoding": rle.get("encoding"),
                    "size": size,
                    "counts": counts,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        )
    except (TypeError, ValueError):
        return
    if canonical_size > MAX_RLE_OBJECT_BYTES:
        _error(
            413,
            "mask_payload_too_large",
            "native Mask RLE exceeds the payload byte budget",
            candidate_index=candidate_index,
        )


def normalize_native_mask_response(
    result: Any,
    diagnostic: Any,
    *,
    context: dict[str, Any],
    expected_size: tuple[int, int] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Validate and canonicalize one native Mask backend response."""

    if context.get("output_geometry", "polygon") != "mask":
        if not isinstance(result, list):
            _error(502, "invalid_backend_response", "backend result must be an array")
        return result, diagnostic if isinstance(diagnostic, dict) else None
    if not isinstance(result, list):
        _error(502, "invalid_mask_payload", "backend Mask result must be an array")

    prompt_revision = context.get("prompt_revision")
    if not isinstance(prompt_revision, str) or not prompt_revision:
        _error(502, "invalid_backend_response", "native Mask revision is missing")

    paired_boxes = context.get("type") == "exemplar" and context.get("output") == "both"
    if paired_boxes and len(result) % 2:
        _error(
            502,
            "invalid_mask_payload",
            "native exemplar output requires complete box/Mask pairs",
        )

    normalized: list[dict[str, Any]] = []
    for index, raw_candidate in enumerate(result):
        if paired_boxes and index % 2 == 0:
            try:
                box = _MaskCompanionBox.model_validate(raw_candidate)
            except ValidationError:
                _error(
                    502,
                    "invalid_mask_payload",
                    "backend returned an invalid native Mask companion box",
                    candidate_index=index,
                )
            normalized.append(box.model_dump(mode="json"))
            continue
        _precheck_rle_budget(raw_candidate, index)
        try:
            candidate = NativeMaskCandidate.model_validate(raw_candidate)
        except ValidationError:
            _error(
                502,
                "invalid_mask_payload",
                "backend returned an invalid native Mask candidate",
                candidate_index=index,
            )
        if expected_size is not None:
            expected_width, expected_height = expected_size
            if candidate.value.rle.size != [expected_height, expected_width]:
                _error(
                    502,
                    "invalid_mask_payload",
                    "backend Mask size does not match task media",
                    candidate_index=index,
                )
        expected_id = native_mask_candidate_id(
            candidate.value.rle,
            prompt_revision=prompt_revision,
            candidate_index=index,
        )
        if candidate.candidate_id != expected_id:
            _error(
                502,
                "invalid_mask_payload",
                "backend Mask candidate_id does not match its pixels and revision",
                candidate_index=index,
            )
        if paired_boxes:
            box = normalized[index - 1]
            if (
                box["value"]["rectanglelabels"] != candidate.value.masklabels
                or box["score"] != candidate.score
            ):
                _error(
                    502,
                    "invalid_mask_payload",
                    "native Mask companion box must match its Mask label and score",
                    candidate_index=index,
                )
        normalized.append(candidate.model_dump(mode="json"))

    if not normalized:
        try:
            parsed_diagnostic = MaskInteractionDiagnostic.model_validate(diagnostic)
        except ValidationError:
            _error(
                502,
                "invalid_mask_payload",
                "empty native Mask result requires an empty_mask diagnostic",
            )
        if parsed_diagnostic.reason != "empty_mask":
            _error(
                502,
                "invalid_mask_payload",
                "empty native Mask result requires an empty_mask diagnostic",
            )
        return [], parsed_diagnostic.model_dump(mode="json", exclude_none=True)

    parsed_diagnostic: dict[str, Any] | None = None
    if diagnostic is not None:
        try:
            parsed_diagnostic = MaskInteractionDiagnostic.model_validate(
                diagnostic
            ).model_dump(mode="json", exclude_none=True)
        except ValidationError:
            _error(
                502,
                "invalid_mask_payload",
                "backend returned an invalid Mask diagnostic",
            )
    return normalized, parsed_diagnostic


__all__ = [
    "normalize_native_mask_response",
    "prepare_interactive_context",
]
