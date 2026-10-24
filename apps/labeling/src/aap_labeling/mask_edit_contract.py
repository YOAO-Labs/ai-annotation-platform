"""Closed persistence commands for polygon and raster Mask edits."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError,
)

from .annotation_write_contract import _json_bytes

MASK_EDIT_PROTOCOL = "aap-labeling-mask-edits/v1"
MASK_EDIT_FUNCTION = "aap_labeling_v1.mask_edit_command(jsonb)"


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _Command(_ClosedModel):
    schema_: Literal["aap-labeling-mask-edits/v1"] = Field(alias="schema")
    operation: StrictStr


class MaskTaskFence(_ClosedModel):
    task_id: uuid.UUID
    project_id: uuid.UUID
    actor_id: uuid.UUID
    expected_batch_id: uuid.UUID | None
    expected_status: StrictStr
    expected_review_round_id: uuid.UUID | None


class FindOperation(_Command):
    operation: Literal["find_operation"]
    task_id: uuid.UUID
    actor_id: uuid.UUID
    idempotency_key: StrictStr


class ReadOperation(_Command):
    operation: Literal["read_operation"]
    task_id: uuid.UUID
    operation_id: uuid.UUID
    lock: StrictBool


class ApplyPolygonSlice(_Command, MaskTaskFence):
    operation: Literal["apply_polygon_slice"]
    source_annotation_id: uuid.UUID
    created_annotation_id: uuid.UUID
    expected_version: StrictInt = Field(ge=1)
    source_geometry: dict[str, object]
    created_geometry: dict[str, object]


class UpdateMaskAnnotation(_ClosedModel):
    action: Literal["update"]
    annotation_id: uuid.UUID
    geometry: dict[str, object]
    track_id: StrictStr | None


class CreateMaskAnnotation(_ClosedModel):
    action: Literal["create"]
    annotation_id: uuid.UUID
    source_annotation_id: uuid.UUID
    geometry: dict[str, object]
    track_id: StrictStr | None


class DeactivateMaskAnnotation(_ClosedModel):
    action: Literal["deactivate"]
    annotation_id: uuid.UUID


MaskAnnotationChange = Annotated[
    UpdateMaskAnnotation | CreateMaskAnnotation | DeactivateMaskAnnotation,
    Field(discriminator="action"),
]


class ApplyMaskMutations(_Command, MaskTaskFence):
    operation: Literal["apply_mask_mutations"]
    mutation_kind: Literal[
        "slice_mask",
        "split_components",
        "copy_component",
        "copy_keyframe",
        "join_masks",
        "overlap",
        "delete_small_islands",
        "fill_small_holes",
        "resolve_same_class_overlap",
        "mask_repair_rollback",
    ]
    expected_versions: dict[StrictStr, StrictInt]
    changes: list[MaskAnnotationChange]


class RestoreSliceAnnotation(_ClosedModel):
    annotation_id: uuid.UUID
    expected_version: StrictInt = Field(ge=1)
    expected_geometry: dict[str, object]
    expected_active: StrictBool
    geometry: dict[str, object]
    active: StrictBool


class ApplySliceRestore(_Command, MaskTaskFence):
    operation: Literal["apply_slice_restore"]
    original_operation_id: uuid.UUID
    target: Literal["before", "after"]
    no_op: StrictBool
    annotations: list[RestoreSliceAnnotation]


class AnnotationLineage(_ClosedModel):
    source_annotation_id: uuid.UUID | None
    result_annotation_id: uuid.UUID | None
    relation: StrictStr
    source_version: StrictInt | None
    result_version: StrictInt | None
    frame_index: StrictInt | None


class WriteAnnotationOperation(_Command, MaskTaskFence):
    operation: Literal["write_operation"]
    operation_id: uuid.UUID
    kind: StrictStr
    idempotency_key: StrictStr
    request_digest: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    scope_fingerprint: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    source_versions: dict[StrictStr, StrictInt]
    result_versions: dict[StrictStr, StrictInt]
    report: dict[str, object]
    response_json: dict[str, object]
    created_at: AwareDatetime | None
    completed_at: AwareDatetime | None
    lineage: list[AnnotationLineage]
    original_operation_id: uuid.UUID | None = None
    original_report: dict[str, object] | None = None


MaskEditCommand = Annotated[
    FindOperation
    | ReadOperation
    | ApplyPolygonSlice
    | ApplyMaskMutations
    | ApplySliceRestore
    | WriteAnnotationOperation,
    Field(discriminator="operation"),
]


class AnnotationOperationData(_ClosedModel):
    found: StrictBool
    operation_id: uuid.UUID | None
    task_id: uuid.UUID | None
    actor_id: uuid.UUID | None
    kind: StrictStr | None
    idempotency_key: StrictStr | None
    request_digest: StrictStr | None
    report: dict[str, object] | None
    response_json: dict[str, object] | None
    created_at: AwareDatetime | None


class MaskAnnotationData(_ClosedModel):
    annotation_id: uuid.UUID
    action: Literal["update", "create", "deactivate"]
    version: StrictInt = Field(ge=1)
    annotation_type: StrictStr
    track_id: StrictStr | None
    user_id: uuid.UUID | None
    video_segment_id: uuid.UUID | None
    tool_unit_id: StrictStr
    class_name: StrictStr
    geometry: dict[str, object]
    confidence: float | None
    attributes: dict[str, object]
    attributes_meta: dict[str, object]
    z_order: StrictInt
    parent_annotation_id: uuid.UUID | None
    is_active: StrictBool
    updated_at: AwareDatetime


class ApplySliceData(_ClosedModel):
    source: MaskAnnotationData
    created: MaskAnnotationData


class ApplyMaskMutationsData(_ClosedModel):
    annotations: list[MaskAnnotationData]


class ApplyRestoreData(_ClosedModel):
    result_versions: dict[StrictStr, StrictInt]
    original_report: dict[str, object]


class WriteOperationData(_ClosedModel):
    stored: StrictBool


class _Reply(_ClosedModel):
    schema_: Literal["aap-labeling-mask-edits/v1"] = Field(alias="schema")
    ok: StrictBool
    request: MaskEditCommand
    data: (
        AnnotationOperationData
        | ApplySliceData
        | ApplyMaskMutationsData
        | ApplyRestoreData
        | WriteOperationData
    )


class MaskEditUnavailable(RuntimeError):
    """The Mask edit owner returned an invalid or mismatched result."""


_COMMAND = TypeAdapter(MaskEditCommand)
_REPLY = TypeAdapter(_Reply)


def decode_mask_edit_command(value: object) -> MaskEditCommand:
    try:
        return _COMMAND.validate_json(_json_bytes(value))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise MaskEditUnavailable() from None


def mask_edit_command_json(command: MaskEditCommand) -> str:
    return decode_mask_edit_command(
        command.model_dump_json(by_alias=True, exclude_unset=True)
    ).model_dump_json(by_alias=True, exclude_unset=True)


def decode_mask_edit_reply(request: MaskEditCommand, value: object):
    try:
        reply = _REPLY.validate_json(_json_bytes(value))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise MaskEditUnavailable() from None
    if not reply.ok or reply.request != request:
        raise MaskEditUnavailable()
    expected_data = {
        FindOperation: AnnotationOperationData,
        ReadOperation: AnnotationOperationData,
        ApplyPolygonSlice: ApplySliceData,
        ApplyMaskMutations: ApplyMaskMutationsData,
        ApplySliceRestore: ApplyRestoreData,
        WriteAnnotationOperation: WriteOperationData,
    }[type(request)]
    if not isinstance(reply.data, expected_data):
        raise MaskEditUnavailable()
    return reply.data


def mask_edit_schema() -> dict[str, object]:
    schema = _REPLY.json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = MASK_EDIT_PROTOCOL
    schema["x-postgresql-functions"] = [MASK_EDIT_FUNCTION]
    return schema
