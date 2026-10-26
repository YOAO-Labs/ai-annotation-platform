"""Pure command and runtime-port types for batch prediction execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from aap_jobs.dispatch_contract import DispatchLease


@dataclass(frozen=True, slots=True)
class BatchPredictionRequest:
    """JSON-safe facts passed by the HTTP/Celery boundary into the use case.

    ``task_ids=None`` means select from the project/batch scope; ``[]`` means
    select no tasks. ``pipeline_stages=None`` or ``[]`` selects the legacy
    single-stage configuration. ``async_job_id=None`` asks the application to
    create its job. A missing or null frozen reference means no frozen identity;
    a supplied snapshot is retained. ``execution_scope`` defaults to ``bulk``
    only when omitted and otherwise passes through unchanged, including ``None``.
    Nested JSON retains its existing representation to avoid copying model configs.
    """

    project_id: str
    ml_backend_id: str
    task_ids: list[str] | None = None
    prompt: str | None = None
    output_mode: str = "mask"
    batch_id: str | None = None
    user_id: str | None = None
    params: dict[str, Any] | None = None
    predict_mode: str = "skip_predicted"
    model_id: str | None = None
    task_type: str | None = None
    model_variants: dict[str, Any] | None = None
    class_filter: list[int] | None = None
    pipeline_stages: list[dict[str, Any]] | None = None
    execution_unit: str | None = None
    execution_scope: str | None = "bulk"
    async_job_id: str | None = None
    frozen_model_ref: dict[str, Any] | None = None
    accepted_inputs_protocol: str | None = None
    accepted_input_ids: dict[str, str] | None = None
    accepted_input_manifest_protocol: str | None = None
    accepted_input_manifest_id: str | None = None
    accepted_input_manifest_digest: str | None = None
    accepted_video_inputs_protocol: str | None = None
    accepted_video_manifest_id: str | None = None
    accepted_video_selection_digest: str | None = None
    accepted_video_selection: dict[str, Any] | None = None
    task: str | None = None
    prompt_type: str | None = None


@dataclass(frozen=True, slots=True)
class VideoFrameTaskPlan:
    """A retained original's selected ordinals, independent of mutable metadata."""

    task_id: str
    input_id: str
    input_job_id: str
    plan_digest: str
    selected_count: int


@dataclass(frozen=True, slots=True)
class FramePreannotationDispatchRequest:
    project_id: str
    ml_backend_id: str
    stage0: dict[str, Any]
    job_id: str
    root_execution_id: str | None
    actor_id: str | None = None
    video_task_plans: tuple[VideoFrameTaskPlan, ...] = ()
    accepted_video_manifest_id: str | None = None
    accepted_video_selection_digest: str | None = None
    accepted_video_sealed_digest: str | None = None


@dataclass(frozen=True, slots=True)
class FramePreannotationDispatchReceipt:
    dispatched: bool
    segments: int
    total_frames: int


@dataclass(frozen=True, slots=True)
class PreparedFramePreannotationDispatch:
    lease: DispatchLease | None
    segment_ids: tuple[str, ...]
    total_frames: int


class FramePreannotationDispatcher(Protocol):
    async def prepare(
        self, request: FramePreannotationDispatchRequest, *, db: Any
    ) -> PreparedFramePreannotationDispatch: ...

    async def __call__(
        self, prepared: PreparedFramePreannotationDispatch, *, db: Any
    ) -> FramePreannotationDispatchReceipt: ...


class BatchPredictionDispatcher(Protocol):
    def dispatch(
        self,
        request: BatchPredictionRequest,
        *,
        queue: str,
        soft_time_limit: int | None = None,
    ) -> str:
        """Publish one unchanged batch_predict task and return its execution ID."""
