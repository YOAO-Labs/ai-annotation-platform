"""Public, persistence-free contract for inference results written by labeling."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from aap_assets_video_inputs.evidence import VideoInputEvidence

from app.services.labeling_write_scope import LabelingWriteScope


RETRYABLE_APPLY_PREDICTION_CODES = frozenset(
    {
        "account_busy",
        "project_busy",
        "membership_busy",
        "batch_busy",
        "task_busy",
        "job_busy",
        "input_busy",
        "mask_content_deleting",
        "mask_content_epoch_changed",
        "mask_storage_unavailable",
    }
)


@dataclass(frozen=True, slots=True)
class BatchResultPredictionIdentity:
    """Server-allocated identity for an immutable captured batch result."""

    prediction_id: uuid.UUID
    prediction_created_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.prediction_id, uuid.UUID):
            raise TypeError("prediction_id must be a UUID")
        created_at = self.prediction_created_at
        if (
            not isinstance(created_at, datetime)
            or created_at.tzinfo is None
            or created_at.utcoffset() != timedelta(0)
        ):
            raise ValueError("prediction_created_at must be an aware UTC datetime")


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
    accepted_input_job_id: uuid.UUID | None = None
    accepted_video_input: VideoInputEvidence | None = None
    batch_result_identity: BatchResultPredictionIdentity | None = None
    # Issuer-bound evidence from the input owner. Receipts are transaction
    # bound, so callers must attach them only to the writing transaction.
    original_receipt: Any | None = None
    reference_receipt: Any | None = None
    background_native_reference_receipt: Any | None = None
    native_reference_receipt: Any | None = None
    native_original_receipt: Any | None = None
    repair_reference_receipt: Any | None = None
    repair_original_receipt: Any | None = None
    video_input_receipt: Any | None = None
    video_result_receipt: Any | None = None


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

    async def apply_prediction_result_bundle(
        self, requests: tuple[ApplyPredictionResultRequest, ...]
    ) -> tuple[ApplyPredictionResultReceipt, ...]:
        """Atomically preflight and persist one immutable Task result bundle."""


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
