"""Bounded Labeling selection and Task-scope projections for video inputs."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Literal
from uuid import UUID

from pydantic import Field, StrictBool, StrictStr, model_validator

from .authority import VideoSelection, json_bytes
from .execution import CapturedVideoTask
from .values import Count, PAGE_SIZE, REPLY_BYTES, REQUEST_BYTES, Revision, Value

SELECTION_SCHEMA = "aap-labeling-video-input-selection/v1"
ErrorCode = Literal[
    "invalid_request",
    "invalid_response",
    "account_not_active",
    "account_busy",
    "invalid_account_role",
    "task_not_visible",
    "task_busy",
    "permission_denied",
    "source_missing",
    "source_not_ready",
    "source_busy",
    "task_scope_changed",
    "unsupported_media",
    "resource_limit",
]


class SelectionFailure(Exception):
    def __init__(self, code: ErrorCode):
        self.code = code
        super().__init__(code)


class VideoSelectionScope(Value):
    actor_id: UUID
    project_id: UUID
    selection: VideoSelection


class OpenVideoInputManifestMembersRequest(VideoSelectionScope):
    @model_validator(mode="after")
    def selection_scope(self):
        _validate_selection_scope(self.selection)
        return self


class OpenVideoInputManifestMembersResult(Value):
    cursor: StrictStr = Field(
        min_length=1,
        max_length=64,
        pattern=r"^aap_video_members_[0-9a-f]{32}$",
    )


class LockVideoInputManifestMemberRequest(VideoSelectionScope):
    task_id: UUID
    captured_task: CapturedVideoTask

    @model_validator(mode="after")
    def member_scope(self):
        _validate_selection_scope(self.selection)
        if (
            self.captured_task.task_id != self.task_id
            or self.captured_task.project_id != self.project_id
            or (
                self.selection.task_ids is not None
                and self.task_id not in self.selection.task_ids
            )
            or (
                self.selection.batch_id is not None
                and self.captured_task.batch_id != self.selection.batch_id
            )
            or (
                self.selection.execution_scope == "workbench"
                and self.selection.task_ids != (self.task_id,)
            )
        ):
            raise ValueError("video member escaped its captured selection")
        return self


class LockedVideoInputManifestMember(Value):
    actor_id: UUID
    task: CapturedVideoTask
    upload_ready: StrictBool


class SelectionCursorHeader(Value):
    kind: Literal["header"]
    candidate_count: Count
    selected_count: Count

    @model_validator(mode="after")
    def counts(self):
        if self.selected_count > self.candidate_count:
            raise ValueError("selected video members exceed candidates")
        return self


class SelectionCursorMember(Value):
    kind: Literal["member"]
    ordinal: Revision
    task: CapturedVideoTask


class SelectionCursorError(Value):
    kind: Literal["error"]
    code: Literal[
        "invalid_response", "task_scope_changed", "unsupported_media", "resource_limit"
    ]


@dataclass(frozen=True)
class NativeSelectionOperation:
    schema: str
    request: type[Value]
    result: type[Value]
    arguments: tuple[str, ...]
    sql_types: tuple[str, ...]
    consumer: Literal["assets_owner"] = "assets_owner"


OPERATIONS = {
    "open_video_input_manifest_members": NativeSelectionOperation(
        SELECTION_SCHEMA,
        OpenVideoInputManifestMembersRequest,
        OpenVideoInputManifestMembersResult,
        ("actor_id", "project_id", "selection"),
        ("uuid", "uuid", "jsonb"),
    ),
    "lock_video_input_manifest_member": NativeSelectionOperation(
        SELECTION_SCHEMA,
        LockVideoInputManifestMemberRequest,
        LockedVideoInputManifestMember,
        ("actor_id", "project_id", "task_id", "selection", "captured_task"),
        ("uuid", "uuid", "uuid", "jsonb", "jsonb"),
    ),
}


def _validate_selection_scope(selection: VideoSelection) -> None:
    if selection.execution_scope == "workbench" and (
        selection.batch_id is not None or selection.task_ids is None
    ):
        raise ValueError("workbench selection requires one Task and no batch")


def parse_cursor_row(raw):
    """Validate a single bounded row fetched from an opened PostgreSQL cursor."""

    try:
        if isinstance(raw, str):
            if len(raw.encode("utf-8")) > REPLY_BYTES:
                raise ValueError("oversized video selection cursor row")
            raw = json.loads(raw)
        if not isinstance(raw, dict) or len(json_bytes(raw)) > REPLY_BYTES:
            raise ValueError("invalid video selection cursor row")
        kind = raw.get("kind")
        if kind == "header":
            return SelectionCursorHeader.model_validate(raw)
        if kind == "member":
            return SelectionCursorMember.model_validate(raw)
        if kind == "error":
            return SelectionCursorError.model_validate(raw)
        raise ValueError("unknown video selection cursor row")
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise SelectionFailure("invalid_response") from None


def parse_native(operation: str, request: Value, raw) -> Value:
    definition = OPERATIONS.get(operation)
    if definition is None or type(request) is not definition.request:
        raise SelectionFailure("invalid_request")
    try:
        if len(json_bytes(request)) > REQUEST_BYTES:
            raise ValueError("oversized video selection request")
        if isinstance(raw, str):
            if len(raw.encode("utf-8")) > REPLY_BYTES:
                raise ValueError("oversized video selection reply")
            raw = json.loads(raw)
        if (
            not isinstance(raw, dict)
            or len(json_bytes(raw)) > REPLY_BYTES
            or raw.get("schema") != definition.schema
        ):
            raise ValueError("invalid video selection envelope")
        if raw.get("ok") is False and set(raw) == {"schema", "ok", "error"}:
            raise SelectionFailure(_Error.model_validate(raw["error"]).code)
        if raw.get("ok") is not True or set(raw) != {"schema", "ok", "data"}:
            raise ValueError("invalid video selection success")
        result = definition.result.model_validate(raw["data"])
        _require_result_scope(operation, request, result)
        return result
    except SelectionFailure:
        raise
    except (
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        UnicodeError,
        RecursionError,
    ):
        raise SelectionFailure("invalid_response") from None


def _require_result_scope(operation: str, request: Value, result: Value) -> None:
    if operation == "open_video_input_manifest_members":
        return
    assert isinstance(request, LockVideoInputManifestMemberRequest)
    assert isinstance(result, LockedVideoInputManifestMember)
    if (
        result.actor_id != request.actor_id
        or result.task.task_id != request.task_id
        or result.task.project_id != request.project_id
        or result.task.model_dump(exclude={"task_version"})
        != request.captured_task.model_dump(exclude={"task_version"})
        or not result.upload_ready
    ):
        raise ValueError("video source lock escaped its captured Task scope")


class _Error(Value):
    code: ErrorCode


def contract_document() -> dict:
    definitions = {}
    models = [m for op in OPERATIONS.values() for m in (op.request, op.result)] + [
        SelectionCursorHeader,
        SelectionCursorMember,
        SelectionCursorError,
        _Error,
    ]
    for model in models:
        shape = model.model_json_schema()
        for name, child in shape.pop("$defs", {}).items():
            if name in definitions and definitions[name] != child:
                raise ValueError("conflicting video selection definitions")
            definitions[name] = child
        if model.__name__ in definitions and definitions[model.__name__] != shape:
            raise ValueError("conflicting video selection model")
        definitions[model.__name__] = shape
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SELECTION_SCHEMA,
        "$defs": definitions,
        "x-stage": "Labeling selection projection; native capture owner integration pending",
        "x-budgets": {
            "request_bytes": REQUEST_BYTES,
            "reply_bytes": REPLY_BYTES,
            "page_size": PAGE_SIZE,
            "explicit_task_limit": 200,
        },
        "x-cursor_rows": {
            "header": "SelectionCursorHeader",
            "member": "SelectionCursorMember",
            "error": "SelectionCursorError",
            "ordering": "header first, then selected Task UUID ascending",
        },
        "x-native": {
            name: {
                "signature": "aap_labeling_v1."
                + name
                + "("
                + ",".join(op.sql_types)
                + ")",
                "arguments": list(op.arguments),
                "request": op.request.__name__,
                "result": op.result.__name__,
                "consumer": op.consumer,
            }
            for name, op in OPERATIONS.items()
        },
    }
