"""Public, persistence-free contract for inference results written by labeling."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from app.services.labeling_write_scope import LabelingWriteScope


RETRYABLE_APPLY_PREDICTION_CODES = frozenset(
    {
        "account_busy",
        "project_busy",
        "membership_busy",
        "batch_busy",
        "task_busy",
        "job_busy",
    }
)


@dataclass(frozen=True, slots=True)
class ApplyPredictionResultRequest:
    task_id: uuid.UUID
    project_id: uuid.UUID
    ml_backend_id: uuid.UUID | None
    result: list[dict[str, Any]]
    write_scope: LabelingWriteScope
    score: float | None = None
    model_version: str | None = None
    inference_time_ms: int | None = None
    token_meta: dict[str, Any] | None = None
    source: str = "ml_backend"
    pipeline_extra: dict[str, Any] | None = None
    ml_backend_pool_id: uuid.UUID | None = None
    model_provenance: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ApplyPredictionResultReceipt:
    """Small immutable receipt shared by prediction-write callers."""

    id: uuid.UUID
    created_at: datetime
    tool_unit_id: str


class ApplyPredictionResultPort(Protocol):
    async def apply_prediction_result(
        self, request: ApplyPredictionResultRequest
    ) -> ApplyPredictionResultReceipt:
        """Write in the already-open caller transaction; never commit it."""


class ApplyPredictionResultError(Exception):
    """Stable write failure facts with the existing HTTP projection preserved."""

    def __init__(
        self,
        *,
        code: str,
        status_code: int,
        detail: Any,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.code = code
        self.retryable = code in RETRYABLE_APPLY_PREDICTION_CODES
        self.status_code = status_code
        self.detail = detail
        self.headers = headers
        super().__init__(str(detail))
