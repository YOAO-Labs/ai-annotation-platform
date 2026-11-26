"""Framework-agnostic platform contract models for yoao-trainer/v1 plugins."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

TRAINER_PROTOCOL_VERSION = "yoao-trainer/v1"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
_SHA256_RE = re.compile(SHA256_PATTERN)

PREVIEW_SEED_MAX = 2147483647
PREVIEW_MAX_SOURCES = 16
PREVIEW_MAX_OUTPUTS = 4
PREVIEW_MAX_EDGE = 2560
PREVIEW_MAX_SOURCE_BYTES = 8 * 1024 * 1024
PREVIEW_MAX_POOL_BYTES = 64 * 1024 * 1024
PREVIEW_MAX_SOURCE_PIXELS = 16_000_000
PREVIEW_MAX_POOL_PIXELS = 32_000_000
PREVIEW_MAX_SOURCE_BOXES = 256
PREVIEW_MAX_POOL_BOXES = 4096
PREVIEW_MAX_OUTPUT_BOXES = 4096
PREVIEW_MAX_OUTPUT_BYTES = 24 * 1024 * 1024


def _has_control_chars(value: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def _require_trimmed(value: str, field_name: str, max_length: int | None = None) -> str:
    if (
        not value.strip()
        or value != value.strip()
        or _has_control_chars(value)
        or (max_length is not None and len(value) > max_length)
    ):
        raise ValueError(f"{field_name} must be a non-empty trimmed printable string")
    return value


def _validate_optional_trimmed(
    value: str | None, field_name: str, max_length: int | None = None
) -> str | None:
    if value is None:
        return None
    return _require_trimmed(value, field_name, max_length=max_length)


def _validate_unique_trimmed_list(
    values: list[str], field_name: str, max_length: int = 128
) -> list[str]:
    seen: set[str] = set()
    for item in values:
        _require_trimmed(item, field_name, max_length=max_length)
        if item in seen:
            raise ValueError(f"{field_name} must not contain duplicates: {item}")
        seen.add(item)
    return values


def _validate_safe_filename(value: str, field_name: str = "filename") -> str:
    _require_trimmed(value, field_name, max_length=255)
    if "/" in value or "\\" in value or value in {".", ".."}:
        raise ValueError(
            f"{field_name} must be a single safe filename without path separators"
        )
    return value


def _validate_safe_relative_path(value: str, field_name: str = "filename") -> str:
    _require_trimmed(value, field_name, max_length=255)
    if value.startswith("/") or "\\" in value:
        raise ValueError(f"{field_name} must be a relative POSIX path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(
            f"{field_name} must not contain empty or traversal path segments"
        )
    return value


def _normalize_zero_floats(value: Any) -> Any:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical JSON forbids NaN and Infinity")
        return 0.0 if value == 0.0 else value
    if isinstance(value, (list, tuple)):
        return [_normalize_zero_floats(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalize_zero_floats(item) for key, item in value.items()}
    return value


class ServingCompatibilityV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    framework: StrictStr = Field(min_length=1, max_length=64)
    format: StrictStr | None = Field(min_length=1, max_length=64)
    training_runtime: dict[str, Any] = Field(default_factory=dict)
    compatible_serving_runtimes: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("framework")
    @classmethod
    def _validate_framework(cls, value: str) -> str:
        return _require_trimmed(value, "framework", max_length=64)

    @field_validator("format")
    @classmethod
    def _validate_format(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _require_trimmed(value, "format", max_length=64)


class TrainerArchitectureV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: StrictStr = Field(min_length=1, max_length=128)
    family: StrictStr = Field(min_length=1, max_length=64)
    tasks: list[StrictStr] = Field(min_length=1)
    end2end: StrictBool = False
    base_asset_filename: StrictStr | None = Field(
        default=None, min_length=1, max_length=255
    )
    base_asset_sha256: StrictStr | None = Field(default=None, pattern=SHA256_PATTERN)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return _require_trimmed(value, "id", max_length=128)

    @field_validator("family")
    @classmethod
    def _validate_family(cls, value: str) -> str:
        return _require_trimmed(value, "family", max_length=64)

    @field_validator("tasks")
    @classmethod
    def _validate_tasks(cls, value: list[str]) -> list[str]:
        return _validate_unique_trimmed_list(value, "tasks", max_length=128)

    @field_validator("base_asset_filename")
    @classmethod
    def _validate_base_asset_filename(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_safe_filename(value, "base_asset_filename")

    @model_validator(mode="after")
    def _validate_base_asset_consistency(self) -> TrainerArchitectureV1:
        if self.base_asset_sha256 is not None and self.base_asset_filename is None:
            raise ValueError("base_asset_sha256 requires base_asset_filename")
        return self


class TrainerCapabilitiesV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal["yoao-trainer/v1"]
    framework_id: StrictStr = Field(min_length=1, max_length=64)
    framework_version: StrictStr = Field(min_length=1, max_length=64)
    runtime_versions: dict[str, StrictStr] = Field(default_factory=dict)
    supported_tasks: list[StrictStr] = Field(min_length=1)
    supported_dataset_contracts: list[StrictStr] = Field(min_length=1)
    architectures: list[TrainerArchitectureV1] = Field(min_length=1)
    parameter_schema: dict[str, Any] = Field(default_factory=dict)
    evaluator_descriptors: dict[str, dict[str, Any]] = Field(default_factory=dict)
    policy_digests: dict[str, StrictStr] = Field(default_factory=dict)
    serving_compatibility: list[ServingCompatibilityV1] = Field(default_factory=list)

    @field_validator("framework_id")
    @classmethod
    def _validate_framework_id(cls, value: str) -> str:
        return _require_trimmed(value, "framework_id", max_length=64)

    @field_validator("framework_version")
    @classmethod
    def _validate_framework_version(cls, value: str) -> str:
        return _require_trimmed(value, "framework_version", max_length=64)

    @field_validator("runtime_versions")
    @classmethod
    def _validate_runtime_versions(cls, value: dict[str, str]) -> dict[str, str]:
        for key, version in value.items():
            _require_trimmed(key, "runtime_versions key", max_length=64)
            _require_trimmed(version, f"runtime_versions[{key!r}]", max_length=128)
        return value

    @field_validator("supported_tasks")
    @classmethod
    def _validate_supported_tasks(cls, value: list[str]) -> list[str]:
        return _validate_unique_trimmed_list(value, "supported_tasks", max_length=128)

    @field_validator("supported_dataset_contracts")
    @classmethod
    def _validate_supported_dataset_contracts(cls, value: list[str]) -> list[str]:
        return _validate_unique_trimmed_list(
            value, "supported_dataset_contracts", max_length=128
        )

    @field_validator("policy_digests")
    @classmethod
    def _validate_policy_digests(cls, value: dict[str, str]) -> dict[str, str]:
        for key, digest in value.items():
            _require_trimmed(key, "policy_digests key", max_length=64)
            if not _SHA256_RE.fullmatch(digest):
                raise ValueError(
                    f"policy_digests[{key!r}] must be a 64-char lowercase hex SHA-256"
                )
        return value

    @model_validator(mode="after")
    def _validate_architectures_against_supported_tasks(self) -> TrainerCapabilitiesV1:
        seen_ids: set[str] = set()
        supported = set(self.supported_tasks)
        for arch in self.architectures:
            if arch.id in seen_ids:
                raise ValueError(f"duplicate architecture id: {arch.id}")
            seen_ids.add(arch.id)
            unknown_tasks = set(arch.tasks) - supported
            if unknown_tasks:
                raise ValueError(
                    f"architecture {arch.id!r} references unsupported tasks: {sorted(unknown_tasks)}"
                )
        return self


class ValidationIssueV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: StrictStr = Field(min_length=1, max_length=64)
    message: StrictStr = Field(min_length=1, max_length=512)
    field: StrictStr | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("code")
    @classmethod
    def _validate_code(cls, value: str) -> str:
        return _require_trimmed(value, "code", max_length=64)

    @field_validator("message")
    @classmethod
    def _validate_message(cls, value: str) -> str:
        return _require_trimmed(value, "message", max_length=512)

    @field_validator("field")
    @classmethod
    def _validate_field(cls, value: str | None) -> str | None:
        return _validate_optional_trimmed(value, "field", max_length=128)


class ValidateConfigResultV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal["yoao-trainer/v1"]
    valid: StrictBool
    schema_id: StrictStr | None = Field(default=None, min_length=1, max_length=128)
    schema_digest: StrictStr | None = Field(default=None, pattern=SHA256_PATTERN)
    resolved_config: dict[str, Any] = Field(default_factory=dict)
    effective_values: dict[str, Any] = Field(default_factory=dict)
    origins: dict[str, StrictStr] = Field(default_factory=dict)
    inactive: dict[str, StrictStr] = Field(default_factory=dict)
    issues: list[ValidationIssueV1] = Field(default_factory=list)

    @field_validator("schema_id")
    @classmethod
    def _validate_schema_id(cls, value: str | None) -> str | None:
        return _validate_optional_trimmed(value, "schema_id", max_length=128)

    @field_validator("origins", "inactive")
    @classmethod
    def _validate_string_maps(cls, value: dict[str, str], info: Any) -> dict[str, str]:
        for key, entry in value.items():
            _require_trimmed(key, f"{info.field_name} key", max_length=128)
            _require_trimmed(entry, f"{info.field_name}[{key!r}]", max_length=512)
        return value

    @model_validator(mode="after")
    def _validate_result_consistency(self) -> ValidateConfigResultV1:
        if self.valid:
            if self.issues:
                raise ValueError("valid=True must not include validation issues")
            if self.schema_id is None or self.schema_digest is None:
                raise ValueError("valid=True requires both schema_id and schema_digest")
        else:
            if not self.issues:
                raise ValueError("valid=False requires at least one validation issue")
        return self


class TrainSpecV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal["yoao-trainer/v1"]
    run_id: StrictStr = Field(min_length=1, max_length=128)
    attempt_id: StrictStr = Field(min_length=1, max_length=128)
    framework_id: StrictStr = Field(min_length=1, max_length=64)
    task: StrictStr = Field(min_length=1, max_length=64)
    architecture_id: StrictStr = Field(min_length=1, max_length=128)
    dataset_contract: StrictStr = Field(min_length=1, max_length=128)
    dataset_dir: StrictStr = Field(min_length=1, max_length=1024)
    dataset_snapshot_digest: StrictStr = Field(pattern=SHA256_PATTERN)
    config_digest: StrictStr = Field(pattern=SHA256_PATTERN)
    classes_digest: StrictStr = Field(pattern=SHA256_PATTERN)
    schema_id: StrictStr | None = Field(default=None, min_length=1, max_length=128)
    schema_digest: StrictStr | None = Field(default=None, pattern=SHA256_PATTERN)
    parameters: dict[str, Any] = Field(default_factory=dict)
    base_weights_path: StrictStr | None = Field(
        default=None, min_length=1, max_length=1024
    )
    resume_checkpoint_path: StrictStr | None = Field(
        default=None, min_length=1, max_length=1024
    )
    init_weights_path: StrictStr | None = Field(
        default=None, min_length=1, max_length=1024
    )
    native_budget: dict[str, Any] | None = None

    @field_validator(
        "run_id",
        "attempt_id",
        "framework_id",
        "task",
        "architecture_id",
        "dataset_contract",
        "dataset_dir",
    )
    @classmethod
    def _validate_required_strings(cls, value: str, info: Any) -> str:
        return _require_trimmed(value, str(info.field_name))

    @field_validator(
        "schema_id",
        "base_weights_path",
        "resume_checkpoint_path",
        "init_weights_path",
    )
    @classmethod
    def _validate_optional_strings(cls, value: str | None, info: Any) -> str | None:
        return _validate_optional_trimmed(value, str(info.field_name))

    @model_validator(mode="after")
    def _validate_spec_consistency(self) -> TrainSpecV1:
        if (
            self.resume_checkpoint_path is not None
            and self.init_weights_path is not None
        ):
            raise ValueError(
                "resume_checkpoint_path and init_weights_path are mutually exclusive"
            )
        if (self.schema_id is None) != (self.schema_digest is None):
            raise ValueError("schema_id and schema_digest must be provided together")
        return self


class TrainEventV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seq: StrictInt = Field(ge=1)
    kind: Literal["phase", "epoch", "progress", "checkpoint", "warning"]
    phase: StrictStr | None = Field(default=None, min_length=1, max_length=128)
    epoch: StrictInt | None = Field(default=None, ge=0)
    total_epochs: StrictInt | None = Field(default=None, ge=1)
    progress: StrictFloat | None = Field(default=None, ge=0.0, le=1.0)
    metrics: dict[str, Any] = Field(default_factory=dict)
    detail: dict[str, Any] = Field(default_factory=dict)

    @field_validator("phase")
    @classmethod
    def _validate_phase(cls, value: str | None) -> str | None:
        return _validate_optional_trimmed(value, "phase", max_length=128)

    @field_validator("progress")
    @classmethod
    def _validate_progress(cls, value: float | None) -> float | None:
        if value is None:
            return None
        as_float = float(value)
        if not math.isfinite(as_float):
            raise ValueError("progress must be a finite float")
        return 0.0 if as_float == 0.0 else as_float

    @model_validator(mode="after")
    def _validate_epoch_bounds(self) -> TrainEventV1:
        if (
            self.epoch is not None
            and self.total_epochs is not None
            and self.epoch > self.total_epochs
        ):
            raise ValueError("epoch cannot exceed total_epochs")
        return self


class TrainArtifactV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal[
        "weights", "checkpoint", "metrics", "config", "input_membership", "extra"
    ]
    filename: StrictStr = Field(min_length=1, max_length=255)
    sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    size_bytes: StrictInt = Field(ge=0)

    @field_validator("filename")
    @classmethod
    def _validate_filename(cls, value: str) -> str:
        return _validate_safe_filename(value, "filename")


class TrainReceiptV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal["yoao-trainer/v1"]
    status: Literal["succeeded", "failed", "cancelled"]
    artifacts: list[TrainArtifactV1] = Field(default_factory=list)
    serving_compatibility: ServingCompatibilityV1 | None = None
    failure_code: StrictStr | None = Field(default=None, min_length=1, max_length=64)
    failure_message: StrictStr | None = Field(
        default=None, min_length=1, max_length=2048
    )

    @field_validator("failure_code", "failure_message")
    @classmethod
    def _validate_failure_fields(cls, value: str | None, info: Any) -> str | None:
        return _validate_optional_trimmed(value, str(info.field_name))

    @model_validator(mode="after")
    def _validate_receipt_consistency(self) -> TrainReceiptV1:
        seen_names: set[str] = set()
        for artifact in self.artifacts:
            if artifact.filename in seen_names:
                raise ValueError(f"duplicate artifact filename: {artifact.filename}")
            seen_names.add(artifact.filename)
        if self.status == "failed" and self.failure_code is None:
            raise ValueError("status='failed' requires failure_code")
        if self.status == "succeeded" and (
            self.failure_code is not None or self.failure_message is not None
        ):
            raise ValueError(
                "status='succeeded' must not set failure_code or failure_message"
            )
        return self


class PreviewSourceV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: StrictStr = Field(min_length=1, max_length=128)
    filename: StrictStr = Field(min_length=1, max_length=255)
    sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    width: StrictInt = Field(ge=1, le=PREVIEW_MAX_SOURCE_PIXELS)
    height: StrictInt = Field(ge=1, le=PREVIEW_MAX_SOURCE_PIXELS)
    size_bytes: StrictInt = Field(ge=1, le=PREVIEW_MAX_SOURCE_BYTES)
    box_count: StrictInt = Field(ge=0, le=PREVIEW_MAX_SOURCE_BOXES)
    label_path: StrictStr | None = Field(default=None, min_length=1, max_length=255)
    label_sha256: StrictStr | None = Field(default=None, pattern=SHA256_PATTERN)

    @field_validator("task_id")
    @classmethod
    def _validate_task_id(cls, value: str) -> str:
        return _require_trimmed(value, "task_id", max_length=128)

    @field_validator("filename")
    @classmethod
    def _validate_filename(cls, value: str) -> str:
        return _validate_safe_relative_path(value, "filename")

    @field_validator("label_path")
    @classmethod
    def _validate_label_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_safe_relative_path(value, "label_path")

    @model_validator(mode="after")
    def _validate_source_bounds(self) -> PreviewSourceV1:
        if self.width * self.height > PREVIEW_MAX_SOURCE_PIXELS:
            raise ValueError(f"source pixels exceed {PREVIEW_MAX_SOURCE_PIXELS}")
        if (self.label_path is None) != (self.label_sha256 is None):
            raise ValueError("label_path and label_sha256 must be provided together")
        return self


class PreviewSpecV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal["yoao-trainer/v1"]
    preview_id: StrictStr = Field(min_length=1, max_length=128)
    framework_id: StrictStr = Field(min_length=1, max_length=64)
    task: StrictStr = Field(min_length=1, max_length=64)
    architecture_id: StrictStr = Field(min_length=1, max_length=128)
    input_digest: StrictStr = Field(pattern=SHA256_PATTERN)
    config_digest: StrictStr = Field(pattern=SHA256_PATTERN)
    dataset_snapshot_digest: StrictStr = Field(pattern=SHA256_PATTERN)
    policy_digest: StrictStr = Field(pattern=SHA256_PATTERN)
    seed: StrictInt = Field(ge=0, le=PREVIEW_SEED_MAX)
    epoch: StrictInt = Field(ge=1)
    count: StrictInt = Field(ge=1, le=PREVIEW_MAX_OUTPUTS)
    sources: list[PreviewSourceV1] = Field(min_length=1, max_length=PREVIEW_MAX_SOURCES)
    parameters: dict[str, Any] = Field(default_factory=dict)
    native_values: dict[str, Any] = Field(default_factory=dict)
    pool_summary: dict[str, Any] = Field(default_factory=dict)

    @field_validator("preview_id", "framework_id", "task", "architecture_id")
    @classmethod
    def _validate_required_strings(cls, value: str, info: Any) -> str:
        return _require_trimmed(value, str(info.field_name))

    @model_validator(mode="after")
    def _validate_pool_limits(self) -> PreviewSpecV1:
        seen_names: set[str] = set()
        total_bytes = 0
        total_pixels = 0
        total_boxes = 0
        for source in self.sources:
            if source.filename in seen_names:
                raise ValueError(
                    f"duplicate preview source filename: {source.filename}"
                )
            seen_names.add(source.filename)
            total_bytes += source.size_bytes
            total_pixels += source.width * source.height
            total_boxes += source.box_count
        if total_bytes > PREVIEW_MAX_POOL_BYTES:
            raise ValueError(f"preview pool bytes exceed {PREVIEW_MAX_POOL_BYTES}")
        if total_pixels > PREVIEW_MAX_POOL_PIXELS:
            raise ValueError(f"preview pool pixels exceed {PREVIEW_MAX_POOL_PIXELS}")
        if total_boxes > PREVIEW_MAX_POOL_BOXES:
            raise ValueError(f"preview pool boxes exceed {PREVIEW_MAX_POOL_BOXES}")
        return self


class PreviewOutputV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: StrictStr = Field(min_length=1, max_length=255)
    sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    size_bytes: StrictInt = Field(ge=1, le=PREVIEW_MAX_OUTPUT_BYTES)
    width: StrictInt = Field(ge=1, le=PREVIEW_MAX_EDGE)
    height: StrictInt = Field(ge=1, le=PREVIEW_MAX_EDGE)
    box_count: StrictInt = Field(ge=0, le=PREVIEW_MAX_OUTPUT_BOXES)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("filename")
    @classmethod
    def _validate_filename(cls, value: str) -> str:
        return _validate_safe_filename(value, "filename")


class PreviewReportV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal["yoao-trainer/v1"]
    status: Literal["succeeded", "failed"]
    input_digest: StrictStr = Field(pattern=SHA256_PATTERN)
    policy_digest: StrictStr = Field(pattern=SHA256_PATTERN)
    cuda_initialized: StrictBool = False
    outputs: list[PreviewOutputV1] = Field(
        default_factory=list, max_length=PREVIEW_MAX_OUTPUTS
    )
    warnings: list[StrictStr] = Field(default_factory=list)
    runtime: dict[str, Any] = Field(default_factory=dict)
    details: dict[str, Any] = Field(default_factory=dict)
    failure_code: StrictStr | None = Field(default=None, min_length=1, max_length=64)
    failure_message: StrictStr | None = Field(
        default=None, min_length=1, max_length=2048
    )

    @field_validator("failure_code", "failure_message")
    @classmethod
    def _validate_failure_fields(cls, value: str | None, info: Any) -> str | None:
        return _validate_optional_trimmed(value, str(info.field_name))

    @field_validator("warnings")
    @classmethod
    def _validate_warnings(cls, value: list[str]) -> list[str]:
        for item in value:
            _require_trimmed(item, "warnings", max_length=1024)
        return value

    @model_validator(mode="after")
    def _validate_preview_report_consistency(self) -> PreviewReportV1:
        seen_names: set[str] = set()
        for output in self.outputs:
            if output.filename in seen_names:
                raise ValueError(
                    f"duplicate preview output filename: {output.filename}"
                )
            seen_names.add(output.filename)
        if self.status == "succeeded":
            if self.cuda_initialized:
                raise ValueError("status='succeeded' preview must not initialize CUDA")
            if not self.outputs:
                raise ValueError(
                    "status='succeeded' preview requires at least one output"
                )
            if self.failure_code is not None or self.failure_message is not None:
                raise ValueError(
                    "status='succeeded' must not set failure_code or failure_message"
                )
        elif self.failure_code is None:
            raise ValueError("status='failed' requires failure_code")
        return self


def canonical_json_bytes(payload: BaseModel | dict[str, Any]) -> bytes:
    if isinstance(payload, BaseModel):
        _normalize_zero_floats(payload.model_dump(mode="python"))
        data: Any = payload.model_dump(mode="json")
    else:
        data = payload
    normalized = _normalize_zero_floats(data)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_json_digest(payload: BaseModel | dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        dir_fd = os.open(str(directory), flags)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def atomic_write_json(
    path: str | Path,
    payload: BaseModel | dict[str, Any],
    *,
    mode: int = 0o644,
) -> None:
    encoded = canonical_json_bytes(payload) + b"\n"
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", dir=str(target.parent))
    fd_open = True
    try:
        with os.fdopen(fd, "wb") as stream:
            fd_open = False
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, target)
        _fsync_directory(target.parent)
    except BaseException:
        if fd_open:
            os.close(fd)
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


def write_event_jsonl(path: str | Path, event: TrainEventV1) -> None:
    _normalize_zero_floats(event.model_dump(mode="python", exclude_none=True))
    normalized = _normalize_zero_floats(
        event.model_dump(mode="json", exclude_none=True)
    )
    line = (
        json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    )
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as stream:
        stream.write(line)
        stream.flush()
        os.fsync(stream.fileno())


__all__ = [
    "PREVIEW_MAX_EDGE",
    "PREVIEW_MAX_OUTPUTS",
    "PREVIEW_MAX_OUTPUT_BOXES",
    "PREVIEW_MAX_OUTPUT_BYTES",
    "PREVIEW_MAX_POOL_BOXES",
    "PREVIEW_MAX_POOL_BYTES",
    "PREVIEW_MAX_POOL_PIXELS",
    "PREVIEW_MAX_SOURCES",
    "PREVIEW_MAX_SOURCE_BOXES",
    "PREVIEW_MAX_SOURCE_BYTES",
    "PREVIEW_MAX_SOURCE_PIXELS",
    "PREVIEW_SEED_MAX",
    "SHA256_PATTERN",
    "TRAINER_PROTOCOL_VERSION",
    "PreviewOutputV1",
    "PreviewReportV1",
    "PreviewSourceV1",
    "PreviewSpecV1",
    "ServingCompatibilityV1",
    "TrainArtifactV1",
    "TrainEventV1",
    "TrainReceiptV1",
    "TrainSpecV1",
    "TrainerArchitectureV1",
    "TrainerCapabilitiesV1",
    "ValidateConfigResultV1",
    "ValidationIssueV1",
    "atomic_write_json",
    "canonical_json_bytes",
    "canonical_json_digest",
    "write_event_jsonl",
]
