"""Bounded, replayable bulk image input manifests and native capabilities."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Annotated, Any, Literal, TypeVar
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    Field,
    StrictStr,
    field_validator,
    model_validator,
)

from aap_assets_images.contract import (
    Count,
    Digest,
    ImageSource,
    ObjectIdentity,
    Revision,
    Value,
    json_bytes,
)
from aap_assets_inputs.contract import ErrorCode as InputErrorCode, InputRecord

MANIFEST_SCHEMA = "aap-assets-input-manifest/v1"
REQUEST_BYTES = 64 * 1024
REPLY_BYTES = 2 * 1024 * 1024
PAGE_SIZE = 100
EXPLICIT_TASK_LIMIT = 200
ErrorCode = Literal[
    InputErrorCode,
    "manifest_missing",
    "manifest_busy",
    "manifest_scope_mismatch",
    "manifest_not_ready",
    "manifest_cursor_invalid",
    "task_scope_changed",
    "unsupported_media",
]
TerminalMemberError = Literal[
    "source_missing",
    "source_changed",
    "source_not_ready",
    "not_image",
    "task_scope_changed",
    "input_expired",
    "input_corrupt",
    "resource_budget_exceeded",
]
Ordinal = Annotated[Count, Field(ge=1)]
PageLimit = Annotated[Count, Field(ge=1, le=PAGE_SIZE)]


class ManifestFailure(Exception):
    def __init__(self, code: ErrorCode):
        self.code = code
        super().__init__(code)


class ManifestSelection(Value):
    batch_id: UUID | None = None
    task_ids: (
        Annotated[list[UUID], Field(min_length=1, max_length=EXPLICIT_TASK_LIMIT)]
        | None
    ) = None
    predict_mode: Literal["skip_predicted", "overwrite", "append"]

    @field_validator("task_ids")
    @classmethod
    def canonical_task_ids(cls, value):
        return sorted(set(value), key=str) if value is not None else None


class ManifestScope(Value):
    job_id: UUID
    actor_id: UUID
    project_id: UUID


class CaptureManifestRequest(ManifestScope):
    selection: ManifestSelection


class ManifestHeader(ManifestScope):
    selection: ManifestSelection
    selection_digest: Digest
    candidate_count: Count
    selected_count: Count
    prepared_count: Count
    status: Literal["preparing", "sealed"]
    ready_count: Count | None
    failed_count: Count | None
    sealed_digest: Digest | None
    created_at: AwareDatetime
    sealed_at: AwareDatetime | None

    @model_validator(mode="after")
    def coherent_barrier(self):
        if self.selected_count > self.candidate_count:
            raise ValueError("selected membership exceeds its candidate scope")
        if self.prepared_count > self.selected_count:
            raise ValueError("preparation count exceeds immutable membership")
        if self.status == "preparing":
            if any(
                value is not None
                for value in (
                    self.ready_count,
                    self.failed_count,
                    self.sealed_digest,
                    self.sealed_at,
                )
            ):
                raise ValueError("unsealed manifest cannot assert final input outcomes")
        elif (
            self.prepared_count != self.selected_count
            or self.ready_count is None
            or self.failed_count is None
            or self.ready_count + self.failed_count != self.selected_count
            or self.sealed_digest is None
            or self.sealed_at is None
            or self.sealed_at < self.created_at
        ):
            raise ValueError("sealed manifest must account for every selected input")
        return self


class ManifestMember(Value):
    ordinal: Ordinal
    task_id: UUID
    project_id: UUID
    task_version: Count
    task_source_generation: Revision
    dataset_item_id: UUID | None
    task_source_snapshot: Digest
    file_path: Annotated[StrictStr, Field(max_length=1024)]
    file_type: Literal["image"]
    source: ImageSource | None
    state: Literal["unprepared", "preparing", "ready", "failed"]
    input: InputRecord | None
    error: ErrorCode | None

    @model_validator(mode="after")
    def coherent_member(self):
        if self.source is not None and (
            self.source.task_id != self.task_id
            or self.source.project_id != self.project_id
        ):
            raise ValueError("manifest source escaped its captured Task")
        if self.input is not None:
            record = self.input
            if (
                record.task_id != self.task_id
                or record.project_id != self.project_id
                or record.task_version != self.task_version
                or record.task_source_generation != self.task_source_generation
                or record.dataset_item_id != self.dataset_item_id
                or record.task_source_snapshot != self.task_source_snapshot
            ):
                raise ValueError("input changed captured Task facts")
            if self.source is None or (
                record.source_owner != self.source.owner
                or record.source_generation != self.source.source_generation
                or record.source_snapshot != self.source.source_snapshot
            ):
                raise ValueError("input changed captured source facts")
        if self.state == "unprepared":
            if self.source is None or self.input is not None or self.error is not None:
                raise ValueError("unprepared member requires its frozen source")
        elif self.state == "preparing":
            if (
                self.input is None
                or self.input.status not in {"pending", "materializing"}
                or self.error is not None
            ):
                raise ValueError("preparing member requires a pending original input")
        elif self.state == "ready":
            # The sealed outcome remains historical after the input later expires.
            if (
                self.input is None
                or self.input.sha256 is None
                or self.input.byte_size is None
                or self.error is not None
            ):
                raise ValueError("ready outcome requires measured original bytes")
        elif self.error is None:
            raise ValueError("failed member requires a fixed error code")
        return self


class ManifestPageRequest(ManifestScope):
    selection_digest: Digest
    sealed_digest: Digest | None = None
    cursor: Count = 0
    page_size: PageLimit = PAGE_SIZE


class ManifestPage(Value):
    manifest: ManifestHeader
    cursor: Count
    next_cursor: Count | None
    members: Annotated[list[ManifestMember], Field(max_length=PAGE_SIZE)]

    @model_validator(mode="after")
    def complete_bounded_page(self):
        count = self.manifest.selected_count
        if self.cursor > count:
            raise ValueError("manifest cursor exceeds its immutable membership")
        if self.cursor < count and not self.members:
            raise ValueError("manifest page omitted remaining members")
        if len({member.task_id for member in self.members}) != len(self.members):
            raise ValueError("manifest page repeats a Task")
        for index, member in enumerate(self.members, start=self.cursor + 1):
            if member.ordinal != index or index > count:
                raise ValueError("manifest page is unordered or incomplete")
            _require_member_scope(self.manifest, member)
            if self.manifest.status == "sealed" and member.state not in {
                "ready",
                "failed",
            }:
                raise ValueError("sealed manifest contains unfinished preparation")
        end = self.cursor + len(self.members)
        expected_cursor = end if end < count else None
        if self.next_cursor != expected_cursor:
            raise ValueError("manifest page cursor does not describe its progress")
        return self


class PrepareManifestMemberRequest(ManifestScope):
    selection_digest: Digest
    ordinal: Ordinal
    task_id: UUID


class NativePrepareManifestMemberRequest(PrepareManifestMemberRequest):
    source_snapshot: Digest | None
    object_identity: ObjectIdentity | None
    terminal_error: TerminalMemberError | None

    @model_validator(mode="after")
    def exactly_one_observation(self):
        if (self.object_identity is None) == (self.terminal_error is None):
            raise ValueError("provide an observed identity or a terminal member error")
        if self.object_identity is not None and self.source_snapshot is None:
            raise ValueError("an observation requires the captured source snapshot")
        return self


class ManifestMemberReceipt(ManifestScope):
    selection_digest: Digest
    member: ManifestMember

    @model_validator(mode="after")
    def matches_manifest(self):
        _require_member_scope(self, self.member)
        return self


class SealManifestRequest(ManifestScope):
    selection_digest: Digest


@dataclass(frozen=True)
class NativeOperation:
    request: type[BaseModel]
    result: type[BaseModel]
    arguments: tuple[str, ...]
    sql_types: tuple[str, ...]
    role: Literal["workflow", "control"]


_SCOPE_ARGUMENTS = ("job_id", "actor_id", "project_id")
_SCOPE_TYPES = ("uuid", "uuid", "uuid")
OPERATIONS = {
    "capture_accepted_image_input_manifest": NativeOperation(
        CaptureManifestRequest,
        ManifestHeader,
        (*_SCOPE_ARGUMENTS, "selection"),
        (*_SCOPE_TYPES, "jsonb"),
        "workflow",
    ),
    "read_accepted_image_input_manifest": NativeOperation(
        ManifestScope, ManifestHeader, _SCOPE_ARGUMENTS, _SCOPE_TYPES, "control"
    ),
    "read_accepted_image_input_manifest_page": NativeOperation(
        ManifestPageRequest,
        ManifestPage,
        (
            *_SCOPE_ARGUMENTS,
            "selection_digest",
            "sealed_digest",
            "cursor",
            "page_size",
        ),
        (*_SCOPE_TYPES, "text", "text", "bigint", "integer"),
        "control",
    ),
    "prepare_accepted_image_input_manifest_member": NativeOperation(
        NativePrepareManifestMemberRequest,
        ManifestMemberReceipt,
        (
            *_SCOPE_ARGUMENTS,
            "selection_digest",
            "ordinal",
            "task_id",
            "source_snapshot",
            "object_identity",
            "terminal_error",
        ),
        (*_SCOPE_TYPES, "text", "bigint", "uuid", "text", "jsonb", "text"),
        "control",
    ),
    "seal_accepted_image_input_manifest": NativeOperation(
        SealManifestRequest,
        ManifestHeader,
        (*_SCOPE_ARGUMENTS, "selection_digest"),
        (*_SCOPE_TYPES, "text"),
        "control",
    ),
}


def _require_member_scope(scope: ManifestScope, member: ManifestMember) -> None:
    if member.project_id != scope.project_id:
        raise ValueError("manifest member escaped its Project")
    if member.input is not None and (
        member.input.job_id != scope.job_id
        or member.input.actor_id != scope.actor_id
        or member.input.project_id != scope.project_id
    ):
        raise ValueError("manifest input escaped its accepted job scope")


def require_scope(request: BaseModel, result: BaseModel) -> None:
    scope = result.manifest if isinstance(result, ManifestPage) else result
    for name in ("job_id", "actor_id", "project_id", "selection_digest"):
        expected = getattr(request, name, None)
        if expected is not None and getattr(scope, name, None) != expected:
            raise ValueError("manifest reply escaped the requested scope")
    if isinstance(request, CaptureManifestRequest) and (
        not isinstance(scope, ManifestHeader) or scope.selection != request.selection
    ):
        raise ValueError("manifest replay changed its original selector")
    if isinstance(request, ManifestPageRequest):
        if not isinstance(result, ManifestPage) or (
            result.cursor != request.cursor
            or len(result.members) > request.page_size
            or (
                request.sealed_digest is not None
                and (
                    result.manifest.status != "sealed"
                    or result.manifest.sealed_digest != request.sealed_digest
                )
            )
        ):
            raise ValueError("manifest page changed its requested boundary")
    if isinstance(request, PrepareManifestMemberRequest) and (
        not isinstance(result, ManifestMemberReceipt)
        or result.member.ordinal != request.ordinal
        or result.member.task_id != request.task_id
        or result.member.state == "unprepared"
    ):
        raise ValueError("manifest preparation changed its selected member")
    if isinstance(request, SealManifestRequest) and (
        not isinstance(scope, ManifestHeader) or scope.status != "sealed"
    ):
        raise ValueError("manifest seal returned unfinished preparation")


class _Error(Value):
    code: ErrorCode


_Result = TypeVar("_Result", bound=BaseModel)


def decode_reply(raw: Any, result_type: type[_Result]) -> _Result:
    try:
        if isinstance(raw, (str, bytes)):
            if len(raw.encode("utf-8") if isinstance(raw, str) else raw) > REPLY_BYTES:
                raise ValueError("oversized manifest reply")

            def reject_constant(_value):
                raise ValueError("nonfinite JSON")

            raw = json.loads(raw, parse_constant=reject_constant)
        elif len(json_bytes(raw)) > REPLY_BYTES:
            raise ValueError("oversized manifest reply")
        if (
            not isinstance(raw, dict)
            or raw.get("schema") != MANIFEST_SCHEMA
            or type(raw.get("ok")) is not bool
        ):
            raise ValueError("invalid manifest envelope")
        if not raw["ok"]:
            if set(raw) != {"schema", "ok", "error"}:
                raise ValueError("invalid manifest failure")
            raise ManifestFailure(_Error.model_validate(raw["error"]).code)
        if set(raw) != {"schema", "ok", "data"}:
            raise ValueError("invalid manifest success")
        return result_type.model_validate(raw["data"])
    except ManifestFailure:
        raise
    except (ValueError, TypeError, UnicodeError, AttributeError, RecursionError):
        raise ManifestFailure("invalid_response") from None


def http_error_status(code: ErrorCode) -> int:
    if code == "manifest_missing":
        return 404
    if code in {"manifest_busy", "manifest_not_ready"}:
        return 409
    if code in {"manifest_scope_mismatch", "manifest_cursor_invalid"}:
        return 409
    from aap_assets_inputs.contract import http_error_status as input_status

    return input_status(code)


def contract_document() -> dict[str, Any]:
    definitions: dict[str, Any] = {}
    models = [m for op in OPERATIONS.values() for m in (op.request, op.result)] + [
        PrepareManifestMemberRequest,
        _Error,
    ]
    for model in models:
        schema = model.model_json_schema()
        for name, definition in schema.pop("$defs", {}).items():
            if name in definitions and definitions[name] != definition:
                raise ValueError("conflicting manifest definition")
            definitions[name] = definition
        if model.__name__ in definitions and definitions[model.__name__] != schema:
            raise ValueError("conflicting manifest model")
        definitions[model.__name__] = schema
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": MANIFEST_SCHEMA,
        "$defs": definitions,
        "x-budgets": {
            "request_bytes": REQUEST_BYTES,
            "reply_bytes": REPLY_BYTES,
            "page_size": PAGE_SIZE,
            "explicit_task_limit": EXPLICIT_TASK_LIMIT,
        },
        "x-native": {
            name: {
                "signature": "aap_assets_v1."
                + name
                + "("
                + ",".join(op.sql_types)
                + ")",
                "arguments": list(op.arguments),
                "request": op.request.__name__,
                "result": op.result.__name__,
                "role": op.role,
            }
            for name, op in OPERATIONS.items()
        },
    }
