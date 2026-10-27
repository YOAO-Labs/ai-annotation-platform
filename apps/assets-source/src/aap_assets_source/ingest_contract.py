"""Strict wire values for synchronous ZIP and bucket source ingestion."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any, Literal, Protocol, TypeVar
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

INGEST_SCHEMA = "aap-dataset-source-ingest/v1"
INGEST_DELIVERY_SCHEMA = "aap-dataset-source-ingest-delivery/v1"
INGEST_BATCH_SIZE = 200
ZIP_MAX_BYTES = 200 * 1024 * 1024
ZIP_MAX_FILES = 5000
ZIP_MAX_CENTRAL_DIRECTORY_ENTRIES = 10_000
ZIP_ENTRY_MAX_BYTES = 100 * 1024 * 1024
SCAN_REQUEST_DIGEST = "f5f47f290df98f65d1879eb9896f8bc6e16b4b0c805a834cca38c44a44faf065"

IngestKind = Literal["zip", "scan"]
IngestFileType = Literal["image", "video", "point_cloud", "other"]
IngestFailureCode = Literal[
    "invalid_request",
    "account_not_active",
    "account_busy",
    "invalid_account_role",
    "catalog_manager_required",
    "dataset_missing",
    "dataset_busy",
    "source_busy",
    "operation_conflict",
    "operation_missing",
    "lease_lost",
    "reserved_namespace",
    "scene_conflict",
    "temporal_scenes_missing",
    "archive_invalid",
    "archive_unsafe",
    "resource_budget_exceeded",
    "storage_unavailable",
    "service_unavailable",
    "invalid_response",
]
IngestEntryErrorCode = Literal[
    "invalid_source_object",
    "archive_entry_read_failed",
    "storage_write_failed",
    "source_changed",
    "source_missing",
    "source_read_failed",
]
_FAILURE_CODES = frozenset(IngestFailureCode.__args__)
Count = Annotated[int, Field(strict=True, ge=0, le=2147483647)]
BytesCount = Annotated[int, Field(strict=True, ge=0, le=9223372036854775807)]
Dimension = Annotated[int, Field(strict=True, ge=1, le=2147483647)]
Digest = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]
Md5 = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{32}$")]
PathText = Annotated[str, Field(strict=True, min_length=1, max_length=4096)]
NameText = Annotated[str, Field(strict=True, min_length=1, max_length=500)]


class IngestFailure(Exception):
    """A stable failure requiring the entire ingest transaction to roll back."""

    def __init__(
        self, code: IngestFailureCode, detail: IngestFailureDetail | None = None
    ):
        if code not in _FAILURE_CODES:
            raise ValueError("Unknown Assets source ingest failure")
        super().__init__(code)
        self.code = code
        self.detail = detail


class _Value(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class IngestFailureDetail(_Value):
    reason: Annotated[str, Field(strict=True, pattern=r"^[a-z][a-z0-9_]{0,63}$")]
    message: Annotated[str, Field(strict=True, max_length=4096)]
    budget: StrictStr | None = None
    limit: StrictInt | StrictFloat | None = None
    observed: StrictInt | StrictFloat | None = None
    path: PathText | None = None
    conflicts_with: PathText | None = None


class IngestScope(_Value):
    operation_id: UUID
    actor_id: UUID
    dataset_id: UUID


class IngestCommand(IngestScope):
    """Verified gateway scope, without a caller role or storage origin."""


class IngestDescriptor(_Value):
    kind: IngestKind
    request_digest: Digest
    total_in_zip: Count | None
    planned_entries: Count | None
    hidden_count: Count = 0
    unsafe_count: Count = 0
    top_level_dirs: Annotated[list[StrictStr], Field(max_length=ZIP_MAX_FILES)] = []

    @model_validator(mode="after")
    def check_descriptor(self) -> IngestDescriptor:
        if self.kind == "scan":
            if self.request_digest != SCAN_REQUEST_DIGEST:
                raise ValueError("Scan command digest does not match the protocol")
            if (
                self.total_in_zip is not None
                or self.planned_entries is not None
                or self.hidden_count
                or self.unsafe_count
                or self.top_level_dirs
            ):
                raise ValueError("Scan descriptors cannot carry ZIP metadata")
        elif (
            self.total_in_zip is None
            or self.planned_entries is None
            or self.total_in_zip > ZIP_MAX_FILES
            or self.planned_entries + self.hidden_count != self.total_in_zip
        ):
            raise ValueError("ZIP entry counts do not match")
        if len(self.top_level_dirs) != len(set(self.top_level_dirs)):
            raise ValueError("Top-level directory names must be unique")
        if any(
            not value
            or len(value.encode("utf-8")) > 4096
            or "/" in value
            or "\\" in value
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
            for value in self.top_level_dirs
        ):
            raise ValueError("Invalid top-level directory name")
        return self


class IngestFileError(_Value):
    name: Annotated[str, Field(strict=True, max_length=512)]
    error: Annotated[str, Field(strict=True, min_length=1, max_length=512)]


class IngestReceipt(IngestScope):
    kind: IngestKind
    request_digest: Digest
    added: Count
    deduped: Count
    skipped: Count
    errors: Annotated[list[IngestFileError], Field(max_length=ZIP_MAX_FILES)]
    error_count: Count
    total_in_zip: Count | None
    linked_tasks: Count
    scene_inference_notes: Annotated[list[StrictStr], Field(max_length=1024)]

    @model_validator(mode="after")
    def check_receipt(self) -> IngestReceipt:
        if len(self.errors) > self.error_count:
            raise ValueError("Error details exceed the recorded count")
        if self.kind == "zip":
            if self.total_in_zip is None or self.total_in_zip > ZIP_MAX_FILES:
                raise ValueError("ZIP receipt is missing its file count")
            if len(self.errors) != self.error_count:
                raise ValueError("ZIP receipt must include all entry errors")
        elif self.total_in_zip is not None or self.scene_inference_notes:
            raise ValueError("Scan receipt cannot report ZIP scene processing")
        if self.kind == "scan" and self.request_digest != SCAN_REQUEST_DIGEST:
            raise ValueError("Scan receipt does not match the protocol command")
        return self

    def public_result(self) -> dict[str, Any]:
        if self.kind == "scan":
            return {
                "status": "ok",
                "new_items": self.added,
                "linked_tasks": self.linked_tasks,
            }
        return {
            "added": self.added,
            "deduped": self.deduped,
            "skipped": self.skipped,
            "errors": [error.model_dump(mode="json") for error in self.errors],
            "total_in_zip": self.total_in_zip,
            "linked_tasks": self.linked_tasks,
            "scene_inference_notes": self.scene_inference_notes,
        }


class IngestContext(IngestScope):
    descriptor: IngestDescriptor
    state: Literal["preparing", "completed"]
    dataset_name: Annotated[str, Field(strict=True, max_length=255)]
    is_temporal: StrictBool
    source_prefix: StrictStr
    next_index: Count
    result: IngestReceipt | None

    @model_validator(mode="after")
    def check_context(self) -> IngestContext:
        if self.source_prefix != self.dataset_name + "/":
            raise ValueError("Scan namespace does not match the accepted dataset name")
        if self.state == "completed":
            if self.result is None:
                raise ValueError("Completed ingest is missing its receipt")
            require_ingest_scope(self, self.result)
            if (self.result.kind, self.result.request_digest) != (
                self.descriptor.kind,
                self.descriptor.request_digest,
            ):
                raise ValueError("Receipt does not match the accepted request")
        elif self.result is not None:
            raise ValueError("Uncommitted ingest cannot have a completed receipt")
        return self


class IngestSourceEntry(_Value):
    source_name: PathText
    relpath: PathText
    source_key: PathText | None
    file_type: IngestFileType
    content_type: Annotated[str, Field(strict=True, max_length=255)]
    file_size: BytesCount
    content_hash: Md5 | None
    width: Dimension | None
    height: Dimension | None
    error_code: IngestEntryErrorCode | None = None

    @model_validator(mode="after")
    def check_dimensions(self) -> IngestSourceEntry:
        if (self.width is None) != (self.height is None):
            raise ValueError("Image dimensions must be a pair")
        if self.file_type != "image" and self.width is not None:
            raise ValueError("Only image entries can carry decoded dimensions")
        return self


class IngestPlannedEntry(IngestSourceEntry):
    entry_id: UUID
    index: Count
    status: Literal["pending", "deduped", "skipped", "error", "added"]
    item_id: UUID | None
    publication_id: UUID | None
    file_name: NameText | None
    file_path: PathText | None

    @model_validator(mode="after")
    def check_plan(self) -> IngestPlannedEntry:
        if self.status == "pending" and (
            self.item_id is None
            or self.file_name is None
            or self.file_path is None
            or self.error_code is not None
        ):
            raise ValueError("Pending entry has no complete publication target")
        if self.status == "error" and self.error_code is None:
            raise ValueError("An errored entry requires a stable code")
        if self.status != "error" and self.error_code is not None:
            raise ValueError("Only an errored entry can carry a failure code")
        return self


class IngestProgress(IngestScope):
    staged: Count
    settled: Count
    added: Count
    deduped: Count
    skipped: Count
    error_count: Count
    linked_tasks: Count

    @model_validator(mode="after")
    def check_counts(self) -> IngestProgress:
        if self.settled != self.added + self.deduped + self.skipped + self.error_count:
            raise ValueError("Settled entry counts do not match")
        if self.settled > self.staged:
            raise ValueError("More entries settled than staged")
        return self


class IngestBatchPlan(IngestScope):
    batch_id: UUID
    entries: Annotated[list[IngestPlannedEntry], Field(max_length=INGEST_BATCH_SIZE)]
    progress: IngestProgress

    @model_validator(mode="after")
    def check_batch(self) -> IngestBatchPlan:
        require_ingest_scope(self, self.progress)
        if len({item.entry_id for item in self.entries}) != len(self.entries):
            raise ValueError("Duplicate entry IDs")
        if any(b.index != a.index + 1 for a, b in zip(self.entries, self.entries[1:])):
            raise ValueError("Entry ordinals are not consecutive")
        return self


class IngestEntryCompletion(_Value):
    entry_id: UUID
    status: Literal["ready", "error"]
    publication_id: UUID | None
    file_size: BytesCount | None
    content_hash: Md5 | None
    width: Dimension | None
    height: Dimension | None
    error_code: IngestEntryErrorCode | None

    @model_validator(mode="after")
    def check_completion(self) -> IngestEntryCompletion:
        if self.status == "ready":
            if self.file_size is None or self.error_code is not None:
                raise ValueError("Ready entries require observations, not an error")
            if (self.width is None) != (self.height is None):
                raise ValueError("Image dimensions must be a pair")
        elif (
            self.error_code is None
            or self.file_size is not None
            or self.content_hash is not None
            or self.width is not None
            or self.height is not None
        ):
            raise ValueError("Failed entries require only a stable failure code")
        return self


class IngestBatchResult(IngestScope):
    batch_id: UUID
    progress: IngestProgress

    @model_validator(mode="after")
    def check_scope(self) -> IngestBatchResult:
        require_ingest_scope(self, self.progress)
        return self


class IngestLayoutItem(_Value):
    id: UUID
    file_name: StrictStr
    file_path: StrictStr
    file_type: Annotated[str, Field(strict=True, max_length=20)]
    scene_id: UUID | None
    frame_index: (
        Annotated[int, Field(strict=True, ge=-2147483648, le=2147483647)] | None
    )
    logical_path: PathText | None


class IngestLayoutPage(IngestScope):
    snapshot_digest: Digest
    scene_count: Count
    assigned_count: Count
    total_items: Count
    items: Annotated[list[IngestLayoutItem], Field(max_length=500)]
    next_item_id: UUID | None

    @model_validator(mode="after")
    def check_page(self) -> IngestLayoutPage:
        ids = [item.id for item in self.items]
        if ids != sorted(set(ids)):
            raise ValueError("Layout items must be unique and ordered")
        if self.next_item_id is not None and (not ids or self.next_item_id != ids[-1]):
            raise ValueError("Layout cursor must be the last returned item")
        if self.assigned_count > self.total_items:
            raise ValueError("Assigned item count exceeds its snapshot")
        return self


class IngestSceneMetadata(_Value):
    mode: Literal["single", "per_subdirectory", "auto"]
    group_key: StrictStr


class IngestPlannedScene(_Value):
    id: UUID
    name: Annotated[str, Field(strict=True, max_length=255)]
    source_format: Literal["inferred"]
    source_metadata: IngestSceneMetadata


class IngestFrameAssignment(_Value):
    item_id: UUID
    scene_id: UUID
    frame_index: Count | None


class IngestScenePlan(_Value):
    mode: Literal["single", "per_subdirectory", "auto"]
    dry_run: StrictBool
    scenes: Annotated[list[IngestPlannedScene], Field(max_length=100)]
    assignments: list[IngestFrameAssignment]
    created_scenes: Count
    assigned_items: Count
    skipped_items: Count
    notes: Annotated[list[StrictStr], Field(max_length=1024)]

    @model_validator(mode="after")
    def check_scene_plan(self) -> IngestScenePlan:
        scene_ids = [scene.id for scene in self.scenes]
        if len(set(scene_ids)) != len(scene_ids):
            raise ValueError("Scene IDs must be unique")
        if len({scene.name for scene in self.scenes}) != len(self.scenes):
            raise ValueError("Scene names must be unique within a dataset")
        assignment_ids = [entry.item_id for entry in self.assignments]
        if len(set(assignment_ids)) != len(assignment_ids):
            raise ValueError("An item cannot be assigned more than once")
        if any(entry.scene_id not in scene_ids for entry in self.assignments):
            raise ValueError("Assignment targets a scene outside the plan")
        if not self.dry_run and (
            self.created_scenes != len(self.scenes)
            or self.assigned_items != len(self.assignments)
        ):
            raise ValueError("Applied scene counts do not match the plan")
        return self


class IngestFinish(_Value):
    layout_snapshot_digest: Digest | None
    scene_plan: IngestScenePlan | None
    scene_inference_notes: Annotated[list[StrictStr], Field(max_length=1024)] = []

    @model_validator(mode="after")
    def check_snapshot(self) -> IngestFinish:
        if self.scene_plan is not None and self.layout_snapshot_digest is None:
            raise ValueError("A scene plan requires its authoritative snapshot")
        return self


class IngestDeliveryLease(_Value):
    id: UUID
    operation_id: UUID
    kind: Literal["thumbnail", "video_metadata"]
    target_id: UUID
    lease_token: UUID
    lease_expires_at: datetime

    @field_validator("lease_expires_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Delivery lease expiry must be timezone-aware")
        return value


class IngestDeliveryAcknowledged(_Value):
    id: UUID
    lease_token: UUID
    delivered: Literal[True]

    @field_validator("delivered", mode="before")
    @classmethod
    def require_delivered_boolean(cls, value: object) -> bool:
        if value is not True:
            raise ValueError("Delivery acknowledgement must be the boolean true")
        return True


class IngestDataPort(Protocol):
    """Owner capabilities bound to the caller's one outer transaction."""

    async def allocate(self, request: dict): ...

    async def open_operation(self, creation_id): ...

    async def original_prepared(self, creation_id, prepared) -> None: ...

    async def original_writing(self, creation_id) -> None: ...

    async def original_written(self, creation_id, receipt) -> None: ...

    async def original_verified(self, creation_id, verification) -> None: ...

    async def fail_original(
        self, creation_id, outcome, error_code, evidence
    ) -> None: ...

    async def close_operation(
        self, creation_id, operation_id, physical_settled
    ) -> None: ...

    async def begin(
        self, command: IngestCommand, descriptor: IngestDescriptor
    ) -> IngestContext: ...

    async def prepare_batch(
        self, command: IngestCommand, batch_id: UUID, entries: list[IngestSourceEntry]
    ) -> IngestBatchPlan: ...

    async def complete_batch(
        self,
        command: IngestCommand,
        batch_id: UUID,
        entries: list[IngestEntryCompletion],
    ) -> IngestBatchResult: ...

    async def read_layout(
        self, command: IngestCommand, *, after_item_id: UUID | None, limit: int = 500
    ) -> IngestLayoutPage: ...

    async def finish(
        self, command: IngestCommand, finish: IngestFinish
    ) -> IngestReceipt: ...


def require_ingest_scope(expected: IngestScope, returned: IngestScope) -> None:
    if (expected.operation_id, expected.actor_id, expected.dataset_id) != (
        returned.operation_id,
        returned.actor_id,
        returned.dataset_id,
    ):
        raise IngestFailure("invalid_response")


_Result = TypeVar("_Result", bound=BaseModel)


def decode_ingest_reply(
    payload: object,
    result_type: type[_Result],
    *,
    schema: str = INGEST_SCHEMA,
) -> _Result | None:
    """Decode native/HTTP results without coercing envelope flags or scope."""
    if isinstance(payload, (str, bytes, bytearray)):
        try:
            payload = json.loads(payload)
        except (ValueError, UnicodeError):
            raise IngestFailure("invalid_response") from None
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != schema
        or type(payload.get("ok")) is not bool
    ):
        raise IngestFailure("invalid_response")
    if payload["ok"]:
        if set(payload) != {"schema", "ok", "data"}:
            raise IngestFailure("invalid_response")
        if payload["data"] is None:
            return None
        try:
            return result_type.model_validate(payload["data"])
        except (ValidationError, ValueError, TypeError):
            raise IngestFailure("invalid_response") from None
    if (
        set(payload) != {"schema", "ok", "error"}
        or not isinstance(payload["error"], dict)
        or set(payload["error"]) not in ({"code"}, {"code", "detail"})
        or not isinstance(payload["error"]["code"], str)
        or payload["error"]["code"] not in _FAILURE_CODES
    ):
        raise IngestFailure("invalid_response")
    try:
        detail = (
            IngestFailureDetail.model_validate(payload["error"]["detail"])
            if "detail" in payload["error"]
            else None
        )
    except (ValidationError, ValueError, TypeError):
        raise IngestFailure("invalid_response") from None
    raise IngestFailure(payload["error"]["code"], detail)


def ingest_json_schema() -> dict[str, Any]:
    """Describe every request/reply model used by the published capabilities."""
    from pydantic.json_schema import models_json_schema

    models = (
        IngestCommand,
        IngestDescriptor,
        IngestContext,
        IngestSourceEntry,
        IngestBatchPlan,
        IngestEntryCompletion,
        IngestBatchResult,
        IngestLayoutPage,
        IngestFinish,
        IngestReceipt,
        IngestDeliveryLease,
        IngestDeliveryAcknowledged,
        IngestFailureDetail,
    )
    _, schema = models_json_schema([(model, "validation") for model in models])
    definitions = schema["$defs"]
    for model in ("IngestDescriptor", "IngestReceipt"):
        definitions[model]["allOf"] = [
            {
                "if": {"properties": {"kind": {"const": "scan"}}, "required": ["kind"]},
                "then": {
                    "properties": {"request_digest": {"const": SCAN_REQUEST_DIGEST}}
                },
            }
        ]
    reply_types = (
        "IngestContext",
        "IngestBatchPlan",
        "IngestBatchResult",
        "IngestLayoutPage",
        "IngestReceipt",
    )
    alternatives = []
    for name, tag, values in (
        ("Ingest", INGEST_SCHEMA, reply_types),
        (
            "Delivery",
            INGEST_DELIVERY_SCHEMA,
            ("IngestDeliveryLease", "IngestDeliveryAcknowledged"),
        ),
    ):
        for successful in (True, False):
            definition = name + ("Success" if successful else "Failure")
            body_name = "data" if successful else "error"
            body = (
                {
                    "anyOf": [{"$ref": "#/$defs/" + value} for value in values]
                    + [{"type": "null"}]
                }
                if successful
                else {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["code"],
                    "properties": {
                        "code": {"enum": sorted(_FAILURE_CODES)},
                        "detail": {"$ref": "#/$defs/IngestFailureDetail"},
                    },
                }
            )
            definitions[definition] = {
                "type": "object",
                "additionalProperties": False,
                "required": ["schema", "ok", body_name],
                "properties": {
                    "schema": {"const": tag},
                    "ok": {"const": successful},
                    body_name: body,
                },
            }
            alternatives.append({"$ref": "#/$defs/" + definition})
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:aap:dataset-source-ingest:v1",
        "title": INGEST_SCHEMA,
        "x-zip-admission": {
            "raw_max_bytes": ZIP_MAX_BYTES,
            "expanded_max_bytes": ZIP_MAX_BYTES,
            "entry_max_bytes": ZIP_ENTRY_MAX_BYTES,
            "file_max_count": ZIP_MAX_FILES,
            "central_directory_max_count": ZIP_MAX_CENTRAL_DIRECTORY_ENTRIES,
            "max_compression_ratio": 100,
        },
        "x-scan-command": {
            "digest_algorithm": "SHA-256",
            "utf8_input": "aap-dataset-source-ingest/v1:scan",
            "request_digest": SCAN_REQUEST_DIGEST,
        },
        "x-postgresql-functions": [
            "aap_assets_v1.begin_dataset_ingest(uuid,uuid,uuid,jsonb)",
            "aap_assets_v1.prepare_dataset_ingest_batch(uuid,uuid,uuid,uuid,jsonb)",
            "aap_assets_v1.complete_dataset_ingest_batch(uuid,uuid,uuid,uuid,jsonb)",
            "aap_assets_v1.read_dataset_ingest_layout(uuid,uuid,uuid,uuid,integer)",
            "aap_assets_v1.finish_dataset_ingest(uuid,uuid,uuid,jsonb)",
            "aap_assets_v1.claim_dataset_source_ingest_delivery(uuid,integer)",
            "aap_assets_v1.complete_dataset_source_ingest_delivery(uuid,uuid)",
        ],
        "x-native-envelope": {
            "schema": INGEST_SCHEMA,
            "strict_boolean_ok": True,
            "failure_codes": sorted(_FAILURE_CODES),
        },
        **schema,
        "oneOf": alternatives,
    }
