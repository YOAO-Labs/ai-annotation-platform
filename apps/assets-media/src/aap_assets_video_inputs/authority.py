"""Narrow Labeling/Jobs facts consumed by the video input owner."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StrictBool, field_validator, model_validator

from .values import (
    Digest,
    FrameSampling,
    REQUEST_BYTES,
    REPLY_BYTES,
    TaskVideoBinding,
    Value,
)

TASK_SCHEMA = "aap-labeling-video-input-source/v1"
JOB_SCHEMA = "aap-jobs-video-input-job/v1"
ErrorCode = Literal[
    "invalid_request",
    "invalid_response",
    "storage_unavailable",
    "account_not_active",
    "account_busy",
    "invalid_account_role",
    "task_not_visible",
    "task_busy",
    "permission_denied",
    "source_missing",
    "source_changed",
    "source_not_ready",
    "not_video",
    "job_busy",
    "job_not_active",
    "manifest_scope_mismatch",
]


class AuthorityFailure(Exception):
    def __init__(self, code: ErrorCode):
        self.code = code
        super().__init__(code)


class VideoSelection(Value):
    batch_id: UUID | None
    task_ids: Annotated[tuple[UUID, ...], Field(min_length=1, max_length=200)] | None
    predict_mode: Literal["skip_predicted", "overwrite", "append"]
    execution_scope: Literal["bulk", "workbench"]
    sampling: FrameSampling

    @field_validator("task_ids", mode="after")
    @classmethod
    def canonical_tasks(cls, values):
        return tuple(sorted(set(values), key=str)) if values is not None else None

    @model_validator(mode="after")
    def workbench_target(self):
        if self.execution_scope == "workbench" and (
            self.task_ids is None or len(self.task_ids) != 1
        ):
            raise ValueError("Workbench video input requires exactly one Task")
        return self


class ReadVideoTaskRequest(Value):
    task_id: UUID
    actor_id: UUID


class LockVideoTaskRequest(ReadVideoTaskRequest):
    expected_binding: TaskVideoBinding

    @model_validator(mode="after")
    def expected_task(self):
        if self.task_id != self.expected_binding.task_id:
            raise ValueError("source lock changed its Task")
        return self


class VideoTaskSource(Value):
    actor_id: UUID
    binding: TaskVideoBinding
    upload_ready: StrictBool


class VideoJobRequest(Value):
    job_id: UUID
    task_id: UUID | None
    actor_id: UUID
    project_id: UUID
    selection: VideoSelection
    selection_digest: Digest | None = None
    require_active: StrictBool = True

    @model_validator(mode="after")
    def selected_task(self):
        if (
            self.task_id is not None
            and self.selection.task_ids is not None
            and self.task_id not in self.selection.task_ids
        ):
            raise ValueError("Task is outside explicit video selection")
        return self


class VideoJobFacts(Value):
    job_id: UUID
    task_id: UUID | None
    actor_id: UUID | None
    project_id: UUID | None
    retained_project_id: UUID | None
    selection: VideoSelection
    status: Literal["pending", "running", "completed", "failed", "cancelled"]
    cancel_requested: StrictBool
    active: StrictBool


class FoundVideoJob(Value):
    job: VideoJobFacts | None


@dataclass(frozen=True)
class NativeAuthority:
    schema: str
    namespace: Literal["aap_labeling_v1", "aap_jobs_v1"]
    request: type[Value]
    result: type[Value]
    arguments: tuple[str, ...]
    sql_types: tuple[str, ...]
    consumer: Literal["assets_owner"] = "assets_owner"


OPERATIONS = {
    "read_video_input_task_source": NativeAuthority(
        TASK_SCHEMA,
        "aap_labeling_v1",
        ReadVideoTaskRequest,
        VideoTaskSource,
        ("task_id", "actor_id"),
        ("uuid", "uuid"),
    ),
    "lock_video_input_task_source": NativeAuthority(
        TASK_SCHEMA,
        "aap_labeling_v1",
        LockVideoTaskRequest,
        VideoTaskSource,
        ("task_id", "actor_id", "expected_binding"),
        ("uuid", "uuid", "jsonb"),
    ),
    "project_video_input_job": NativeAuthority(
        JOB_SCHEMA,
        "aap_jobs_v1",
        VideoJobRequest,
        FoundVideoJob,
        (
            "job_id",
            "task_id",
            "actor_id",
            "project_id",
            "selection",
            "selection_digest",
            "require_active",
        ),
        ("uuid", "uuid", "uuid", "uuid", "jsonb", "text", "boolean"),
    ),
}


def json_bytes(value) -> bytes:
    if isinstance(value, Value):
        value = value.model_dump(mode="json")
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")


def source_binding_matches(
    expected: TaskVideoBinding, current: TaskVideoBinding
) -> bool:
    # Workflow versions are provenance, not a new source identity.
    return expected.model_dump(exclude={"task_version"}) == current.model_dump(
        exclude={"task_version"}
    )


def require_result_scope(operation: str, request: Value, result: Value):
    if isinstance(result, VideoTaskSource):
        if (
            result.actor_id != request.actor_id
            or result.binding.task_id != request.task_id
        ):
            raise ValueError("video Task reply escaped its request")
        if isinstance(request, LockVideoTaskRequest) and (
            not result.upload_ready
            or not source_binding_matches(request.expected_binding, result.binding)
        ):
            raise ValueError("video source lock did not fence the original binding")
        return
    job = result.job
    if job is None:
        if request.require_active:
            raise ValueError("missing Job cannot grant execution")
        return
    if (
        job.job_id != request.job_id
        or job.task_id != request.task_id
        or job.selection != request.selection
    ):
        raise ValueError("video Job reply changed its scope")
    if job.actor_id is not None and job.actor_id != request.actor_id:
        raise ValueError("video Job changed its origin actor")
    if (
        (job.project_id is not None and job.project_id != request.project_id)
        or (
            job.retained_project_id is not None
            and job.retained_project_id != request.project_id
        )
        or (job.project_id is None and job.retained_project_id != request.project_id)
    ):
        raise ValueError("video Job changed project scope")
    active = (
        job.actor_id == request.actor_id
        and job.project_id == request.project_id
        and job.status in {"pending", "running"}
        and not job.cancel_requested
    )
    if job.active != active or (request.require_active and not active):
        raise ValueError("video Job cannot grant current execution")


class _Error(Value):
    code: ErrorCode


def parse_native(operation: str, request: Value, raw) -> Value:
    definition = OPERATIONS.get(operation)
    if definition is None or type(request) is not definition.request:
        raise AuthorityFailure("invalid_request")
    try:
        if isinstance(raw, str):
            if len(raw.encode("utf-8")) > REPLY_BYTES:
                raise ValueError("oversized video authority reply")
            raw = json.loads(raw)
        if (
            not isinstance(raw, dict)
            or len(json_bytes(raw)) > REPLY_BYTES
            or raw.get("schema") != definition.schema
        ):
            raise ValueError("invalid video authority envelope")
        if raw.get("ok") is False and set(raw) == {"schema", "ok", "error"}:
            raise AuthorityFailure(_Error.model_validate(raw["error"]).code)
        if raw.get("ok") is not True or set(raw) != {"schema", "ok", "data"}:
            raise ValueError("invalid video authority success")
        if (
            operation == "project_video_input_job"
            and raw["data"].get("job") is not None
        ):
            if raw["data"]["job"].get("selection") != request.selection.model_dump(
                mode="json"
            ):
                raise ValueError("native video selection is not canonical")
        result = definition.result.model_validate(raw["data"])
        require_result_scope(operation, request, result)
        return result
    except AuthorityFailure:
        raise
    except (
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        UnicodeError,
        RecursionError,
    ):
        raise AuthorityFailure("invalid_response") from None


def contract_document() -> dict:
    definitions = {}
    for model in [m for op in OPERATIONS.values() for m in (op.request, op.result)] + [
        _Error
    ]:
        shape = model.model_json_schema()
        for name, child in shape.pop("$defs", {}).items():
            if name in definitions and definitions[name] != child:
                raise ValueError("conflicting video authority definitions")
            definitions[name] = child
        if model.__name__ in definitions and definitions[model.__name__] != shape:
            raise ValueError("conflicting video authority model")
        definitions[model.__name__] = shape
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "aap-video-input-authorities/v1",
        "$defs": definitions,
        "x-budgets": {"request_bytes": REQUEST_BYTES, "reply_bytes": REPLY_BYTES},
        "x-native": {
            name: {
                "signature": op.namespace
                + "."
                + name
                + "("
                + ",".join(op.sql_types)
                + ")",
                "schema": op.schema,
                "arguments": list(op.arguments),
                "request": op.request.__name__,
                "result": op.result.__name__,
                "consumer": op.consumer,
            }
            for name, op in OPERATIONS.items()
        },
    }
