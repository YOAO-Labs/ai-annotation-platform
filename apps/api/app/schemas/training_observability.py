"""Independent, strictly bounded observation cursors and public attempt pages."""

import math
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from training_contract.observability import (
    CHUNK_BYTES,
    METRICS,
    REQUEST_BYTES,
    STATE_BYTES,
    encoded,
    validate_sample,
)

from app.schemas.training_run import ExecutorFence


class ObservationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Measurement(ObservationModel):
    value: int | float | None
    reason: (
        Literal[
            "baseline_pending",
            "process_unavailable",
            "permission_denied",
            "pid_reused",
            "device_unavailable",
            "invalid_value",
            "pid_namespace_unresolved",
            "cuda_uninitialized",
            "callback_unavailable",
            "callback_stale",
            "recovery_no_process",
        ]
        | None
    )

    @field_validator("value", mode="before")
    @classmethod
    def actual_number(cls, value):
        if value is not None and type(value) not in {int, float}:
            raise ValueError("观测值必须为实际数值")
        return value


class EpochCycle(ObservationModel):
    epoch: int = Field(ge=1, le=1000, strict=True)
    seconds: float = Field(gt=0)
    comparable: bool
    closing: bool


class TrainingEstimate(ObservationModel):
    available: bool
    reason: (
        Literal[
            "insufficient_comparable_cycles",
            "training_complete",
            "unobserved_augmentation_regime",
        ]
        | None
    ) = None
    sample_count: int = Field(ge=0, le=20)
    remaining_epochs: int | None = Field(default=None, ge=1, le=1000)
    minimum_seconds: float | None = Field(default=None, ge=0)
    maximum_seconds: float | None = Field(default=None, ge=0)
    scope: Literal["training_only"] | None = None
    closing: bool | None = None


class NativeRaw(ObservationModel):
    epoch_seconds: float = Field(ge=0)


class TrainingProgress(ObservationModel):
    completed_epoch: int = Field(default=0, ge=0, le=1000, strict=True)
    total_epochs: int | None = Field(default=None, ge=1, le=1000, strict=True)
    cycles: list[EpochCycle] = Field(default_factory=list, max_length=20)
    phase_durations: dict[
        Literal["preparing", "training", "evaluating", "uploading"], float
    ] = Field(default_factory=dict, max_length=4)
    eta: TrainingEstimate | None = None
    native_raw: NativeRaw | None = None

    @field_validator("phase_durations")
    @classmethod
    def positive_durations(cls, values):
        if any(not math.isfinite(value) or value < 0 for value in values.values()):
            raise ValueError("阶段耗时必须为非负有限数值")
        return values


class TelemetryItem(ObservationModel):
    sequence: int = Field(ge=1, le=2147483647, strict=True)
    producer_boot_id: uuid.UUID
    captured_at: datetime
    phase: Literal["preparing", "training", "evaluating", "uploading"]
    phase_elapsed_seconds: float = Field(ge=0)
    callback_age_seconds: float | None = Field(default=None, ge=0)
    metrics: dict[str, Measurement] = Field(max_length=9)
    progress: TrainingProgress = Field(default_factory=TrainingProgress)

    @model_validator(mode="after")
    def typed_measurements(self):
        validate_sample(
            self.model_dump(mode="json", exclude_none=True)
            | {
                "metrics": {
                    key: item.model_dump() for key, item in self.metrics.items()
                }
            }
        )
        return self

    @field_validator("captured_at")
    @classmethod
    def aware_time(cls, value):
        if value.tzinfo is None:
            raise ValueError("采集时间必须包含时区")
        return value


class LogItem(ObservationModel):
    sequence: int = Field(ge=1, le=2147483647, strict=True)
    producer_boot_id: uuid.UUID
    captured_at: datetime
    text: str

    @field_validator("text")
    @classmethod
    def utf8_bound(cls, value):
        if not value or len(value.encode()) > CHUNK_BYTES:
            raise ValueError("日志块超出 UTF-8 字节上限")
        return value

    @field_validator("captured_at")
    @classmethod
    def aware_time(cls, value):
        return TelemetryItem.aware_time(value)


class ObservationSummary(ObservationModel):
    protocol: Literal[1]
    peaks: dict[str, int | float] = Field(default_factory=dict, max_length=9)
    latest: TelemetryItem | None = None

    @field_validator("peaks")
    @classmethod
    def valid_peaks(cls, values):
        if any(
            key not in METRICS
            or type(value) not in {int, float}
            or not math.isfinite(value)
            or value < 0
            for key, value in values.items()
        ):
            raise ValueError("资源峰值不合法")
        return values

    @model_validator(mode="after")
    def bounded(self):
        if len(encoded(self.model_dump(mode="json"))) > STATE_BYTES:
            raise ValueError("观测摘要超出上限")
        return self


class CursorSkip(ObservationModel):
    after: int = Field(ge=0, le=2147483647, strict=True)
    through: int = Field(ge=1, le=2147483647, strict=True)

    @model_validator(mode="after")
    def positive_interval(self):
        if self.through <= self.after:
            raise ValueError("跳过区间必须向前推进")
        return self


class ExecutorObservations(ExecutorFence):
    stream_id: uuid.UUID
    request_id: uuid.UUID
    producer_boot_id: uuid.UUID
    telemetry: list[TelemetryItem] = Field(default_factory=list, max_length=12)
    logs: list[LogItem] = Field(default_factory=list, max_length=32)
    skipped: dict[Literal["telemetry", "logs"], CursorSkip] = Field(
        default_factory=dict, max_length=2
    )
    summary: ObservationSummary

    @model_validator(mode="after")
    def bounded(self):
        if len(encoded(self.model_dump(mode="json"))) > REQUEST_BYTES:
            raise ValueError("观测请求超出编码字节上限")
        if not self.telemetry and not self.logs and not self.skipped:
            raise ValueError("观测请求不能为空")
        return self


class ObservationAck(ObservationModel):
    stream_id: uuid.UUID
    request_id: uuid.UUID
    telemetry_cursor: int
    logs_cursor: int


class CursorGap(CursorSkip):
    reason: Literal["retention", "producer_retention"]


class TelemetryOut(TelemetryItem):
    received_at: datetime


class LogOut(LogItem):
    received_at: datetime


class ObservationPage(ObservationModel):
    attempt_id: uuid.UUID
    stream_id: uuid.UUID | None
    earliest_cursor: int | None
    latest_cursor: int
    next_cursor: int
    has_more: bool
    gaps: list[CursorGap]
    summary: ObservationSummary | None


class TelemetryPage(ObservationPage):
    items: list[TelemetryOut]
    metric_definitions: dict[str, dict[str, str]]


class LogPage(ObservationPage):
    items: list[LogOut]


class TrainingRecovery(ObservationModel):
    attempt_id: uuid.UUID | None
    eligible: bool
    reasons: list[str]
    executor_online: bool
    runtime_compatible: bool
    checkpoint_epoch: int | None
