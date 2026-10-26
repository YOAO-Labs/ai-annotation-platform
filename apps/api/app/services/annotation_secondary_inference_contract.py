"""Persistence-free port for applying completed secondary inference."""

from __future__ import annotations

from typing import Literal, Protocol

from app.schemas.annotation import SecondaryInferenceResponse
from app.services.audit_origin_contract import AuditOriginContext
from app.services.labeling_write_scope import LabelingWriteScope
from app.services.secondary_inference_result import SecondaryInferenceResult


class SecondaryInferenceWritePort(Protocol):
    async def apply_secondary_inference(
        self,
        *,
        write_scope: LabelingWriteScope,
        result: SecondaryInferenceResult,
        write_target: Literal["attributes", "geometry"],
        audit_origin: AuditOriginContext,
    ) -> SecondaryInferenceResponse:
        """Apply facts and finalize the existing HTTP write transaction."""
