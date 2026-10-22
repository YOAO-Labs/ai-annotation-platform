"""Portable structural validation for saved preannotation pipelines."""

from __future__ import annotations

from pydantic import ValidationError

from .preannotate import PreannotateRequest


class PipelineValidationError(ValueError):
    """Portable saved-pipeline validation failure with the existing API detail."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


def validate_saved_pipeline(stages) -> list[dict] | None:
    """Validate saved stages structurally, without backend availability checks.

    PipelineStage normalizes stored backend ID strings through the public request
    schema. Backend existence and project enablement are apply-time checks.
    """
    if not stages:
        return stages
    if not isinstance(stages, list) or not all(isinstance(s, dict) for s in stages):
        raise PipelineValidationError("preannotate_pipeline 须为阶段对象数组")
    roots = [s for s in stages if s.get("parent_stage") is None]
    if len(roots) != 1:
        raise PipelineValidationError(
            "preannotate_pipeline 须恰有一个源阶段 (parent_stage=None)"
        )
    try:
        parsed = PreannotateRequest(
            ml_backend_id=roots[0].get("ml_backend_id"), pipeline_stages=stages
        )
        return [
            stage.model_dump(mode="json", exclude_unset=True)
            for stage in parsed.pipeline_stages
        ]
    except ValidationError as exc:
        raise PipelineValidationError(f"preannotate_pipeline 校验失败: {exc}") from exc


__all__ = ["PipelineValidationError", "validate_saved_pipeline"]
