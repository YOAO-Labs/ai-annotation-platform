"""Compatibility exports for shared preannotation pipeline validation."""

from yoao_inference_contract.pipeline import (
    PipelineValidationError,
    validate_saved_pipeline,
)

__all__ = ["PipelineValidationError", "validate_saved_pipeline"]
