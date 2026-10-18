"""Portable, strict commands and replies for Assets spatial state."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any, Literal, TypeVar
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    model_validator,
)

from .spatial_values import (
    FramePose,
    LidarAxisConvention,
    SensorCalibration,
    calibration_digest,
)

SPATIAL_SCHEMA = "aap-assets-spatial/v1"
SPATIAL_PAGE_SIZE = 200
Count = Annotated[StrictInt, Field(ge=0)]
PageSize = Annotated[StrictInt, Field(ge=1, le=500)]
Digest = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
Revision = Annotated[StrictInt, Field(ge=1)]
ItemIds = Annotated[list[UUID], Field(min_length=1, max_length=5000)]
SceneMode = Literal["single", "per_subdirectory", "auto"]
SpatialAccess = Literal["read", "manager_read", "manager_write"]
SpatialFailureCode = Literal[
    "invalid_request",
    "account_not_active",
    "account_busy",
    "invalid_account_role",
    "catalog_manager_required",
    "dataset_missing",
    "scene_missing",
    "source_missing",
    "source_busy",
    "dataset_busy",
    "scene_conflict",
    "source_changed",
    "operation_conflict",
    "operation_missing",
    "association_changed",
    "operation_cancelled",
    "project_missing",
    "project_manager_required",
    "task_missing",
    "task_not_visible",
    "task_busy",
    "annotation_write_forbidden",
    "camera_role_not_linked",
    "camera_dataset_item_missing",
    "camera_calibration_missing",
    "camera_calibration_invalid",
    "calibration_revision_conflict",
    "camera_shared_across_projects",
    "storage_unavailable",
    "resource_budget_exceeded",
    "service_unavailable",
    "invalid_response",
]
_FAILURE_CODES = frozenset(SpatialFailureCode.__args__)


class SpatialFailure(Exception):
    def __init__(self, code: SpatialFailureCode):
        if code not in _FAILURE_CODES:
            raise ValueError("Unknown Assets spatial failure")
        self.code = code
        super().__init__(code)


class _Value(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class SpatialScene(_Value):
    id: UUID
    display_id: StrictStr
    dataset_id: UUID
    name: StrictStr
    source_format: StrictStr | None
    source_metadata: dict[str, Any]
    created_by: UUID | None
    created_at: datetime
    updated_at: datetime


class SpatialItem(_Value):
    id: UUID
    dataset_id: UUID
    file_name: StrictStr
    file_path: StrictStr
    file_type: StrictStr
    scene_id: UUID | None
    frame_index: StrictInt | None
    metadata: dict[str, Any]
    created_at: datetime
    width: StrictInt | None
    height: StrictInt | None
    file_size: StrictInt | None
    content_hash: StrictStr | None
    thumbnail_path: StrictStr | None
    axis_convention: LidarAxisConvention | None
    calibration_revision: Revision | None
    source_snapshot: dict[str, Any]
    camera_roles: list[StrictStr]


class SceneCursor(_Value):
    created_at: datetime
    scene_id: UUID


class CalibrationCursor(_Value):
    dataset_item_id: UUID
    revision: Revision


class GuardSpatialActorRequest(_Value):
    actor_id: UUID
    access: SpatialAccess


class SpatialActorResult(GuardSpatialActorRequest):
    pass


class ListScenesRequest(_Value):
    actor_id: UUID | None
    dataset_id: UUID
    after: SceneCursor | None = None
    page_size: PageSize = SPATIAL_PAGE_SIZE


class GetSceneRequest(_Value):
    actor_id: UUID | None
    scene_id: UUID


class SceneResult(_Value):
    actor_id: UUID | None
    scene_id: UUID
    scene: SpatialScene

    @model_validator(mode="after")
    def check_scope(self):
        if self.scene.id != self.scene_id:
            raise ValueError("Scene reply escaped its scope")
        return self


class ScenePage(_Value):
    actor_id: UUID | None
    dataset_id: UUID
    after: SceneCursor | None
    next_cursor: SceneCursor | None
    snapshot_digest: Digest
    total: Count
    complete: StrictBool
    scenes: list[SpatialScene]

    @model_validator(mode="after")
    def check_scope(self):
        if any(scene.dataset_id != self.dataset_id for scene in self.scenes):
            raise ValueError("Scenes escaped Dataset scope")
        _check_page(self.scenes, self.total, self.complete, self.next_cursor)
        return self


class ReadLayoutRequest(_Value):
    actor_id: UUID | None
    dataset_id: UUID
    creation_operation_id: UUID | None = None
    after_item_id: UUID | None = None
    page_size: PageSize = SPATIAL_PAGE_SIZE


class LayoutPage(_Value):
    actor_id: UUID | None
    dataset_id: UUID
    creation_operation_id: UUID | None
    dataset_name: StrictStr
    is_temporal: StrictBool
    axis_convention: LidarAxisConvention | None
    scene_count: Count
    assigned_count: Count
    after_item_id: UUID | None
    next_item_id: UUID | None
    snapshot_digest: Digest
    total: Count
    complete: StrictBool
    items: list[SpatialItem]
    scenes: list[SpatialScene]

    @model_validator(mode="after")
    def check_scope(self):
        if any(item.dataset_id != self.dataset_id for item in self.items):
            raise ValueError("Layout items escaped Dataset scope")
        if any(scene.dataset_id != self.dataset_id for scene in self.scenes):
            raise ValueError("Layout scenes escaped Dataset scope")
        _check_page(self.items, self.total, self.complete, self.next_item_id)
        return self


class ReadItemsRequest(_Value):
    dataset_id: UUID | None = None
    item_ids: ItemIds | None = None
    after_item_id: UUID | None = None
    page_size: PageSize = SPATIAL_PAGE_SIZE

    @model_validator(mode="after")
    def check_selector(self):
        if (self.dataset_id is None) == (self.item_ids is None):
            raise ValueError("Exactly one item selector is required")
        if self.item_ids is not None and len(set(self.item_ids)) != len(self.item_ids):
            raise ValueError("Duplicate item IDs")
        return self


class ItemFactsPage(_Value):
    dataset_id: UUID | None
    item_ids: list[UUID] | None
    after_item_id: UUID | None
    next_item_id: UUID | None
    snapshot_digest: Digest
    total: Count
    complete: StrictBool
    items: list[SpatialItem]
    scenes: list[SpatialScene]

    @model_validator(mode="after")
    def check_scope(self):
        requested = set(self.item_ids or [])
        if (self.dataset_id is None) == (self.item_ids is None):
            raise ValueError("Exactly one item selector is required")
        if any(
            (self.dataset_id is not None and item.dataset_id != self.dataset_id)
            or (self.item_ids is not None and item.id not in requested)
            for item in self.items
        ):
            raise ValueError("Item facts escaped requested scope")
        _check_page(self.items, self.total, self.complete, self.next_item_id)
        return self


class ReadPosesRequest(_Value):
    actor_id: UUID | None
    scene_id: UUID
    after_frame_index: StrictInt | None = None
    page_size: PageSize = SPATIAL_PAGE_SIZE


class PosePage(_Value):
    actor_id: UUID | None
    scene_id: UUID
    after_frame_index: StrictInt | None
    next_frame_index: StrictInt | None
    snapshot_digest: Digest
    total: Count
    complete: StrictBool
    poses: list[FramePose]

    @model_validator(mode="after")
    def check_page(self):
        _check_page(self.poses, self.total, self.complete, self.next_frame_index)
        return self


class ReadCalibrationsRequest(_Value):
    dataset_item_ids: ItemIds
    include_history: StrictBool = False
    after: CalibrationCursor | None = None
    page_size: PageSize = SPATIAL_PAGE_SIZE

    @model_validator(mode="after")
    def check_ids(self):
        if len(set(self.dataset_item_ids)) != len(self.dataset_item_ids):
            raise ValueError("Duplicate calibration item IDs")
        return self


class CalibrationRecord(_Value):
    """Native raw snapshot; only the owner normalizes historical calibration JSON."""

    dataset_item_id: UUID
    revision: Revision
    calibration: dict[str, Any] | None
    digest: Digest | None
    created_at: datetime | None
    source_snapshot: dict[str, Any]
    error: (
        Literal[
            "camera_dataset_item_missing",
            "camera_calibration_missing",
            "camera_calibration_invalid",
        ]
        | None
    )


class CalibrationState(_Value):
    dataset_item_id: UUID
    revision: Revision
    calibration: SensorCalibration
    digest: Digest
    created_at: datetime | None
    source_snapshot: dict[str, Any]


def normalize_calibration_record(record: CalibrationRecord) -> CalibrationState:
    if record.error is not None:
        raise SpatialFailure(record.error)
    if record.calibration is None:
        raise SpatialFailure("camera_calibration_missing")
    try:
        calibration = SensorCalibration.model_validate(record.calibration)
        digest = calibration_digest(calibration)
    except (TypeError, ValueError) as exc:
        raise SpatialFailure("camera_calibration_invalid") from exc
    if record.digest is not None and record.digest != digest:
        raise ValueError("Materialized calibration digest does not match its value")
    return CalibrationState(
        dataset_item_id=record.dataset_item_id,
        revision=record.revision,
        calibration=calibration,
        digest=digest,
        created_at=record.created_at,
        source_snapshot=record.source_snapshot,
    )


class CalibrationPage(_Value):
    dataset_item_ids: list[UUID]
    include_history: StrictBool
    after: CalibrationCursor | None
    next_cursor: CalibrationCursor | None
    snapshot_digest: Digest
    total: Count
    complete: StrictBool
    records: list[CalibrationRecord]

    @model_validator(mode="after")
    def check_scope(self):
        if any(
            row.dataset_item_id not in self.dataset_item_ids for row in self.records
        ):
            raise ValueError("Calibration reply escaped requested item scope")
        _check_page(self.records, self.total, self.complete, self.next_cursor)
        return self


class LockCalibrationFactsRequest(_Value):
    actor_id: UUID | None
    task_id: UUID | None
    camera_role: StrictStr | None
    dataset_item_id: UUID
    expected_source_snapshot: dict[str, Any]


class ScenePatch(_Value):
    name: Annotated[StrictStr, Field(min_length=1, max_length=255)] | None = None
    source_format: Annotated[StrictStr, Field(max_length=50)] | None = None
    source_metadata: dict[str, Any] | None = None

    def native_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


class MutateSceneRequest(_Value):
    operation_id: UUID
    actor_id: UUID
    scene_id: UUID
    patch: ScenePatch
    audit_context: dict[str, Any] = Field(default_factory=dict)


class SceneMutationResult(_Value):
    operation_id: UUID
    actor_id: UUID | None
    scene_id: UUID
    scene: SpatialScene

    @model_validator(mode="after")
    def check_scope(self):
        if self.scene.id != self.scene_id:
            raise ValueError("Mutation escaped requested Scene scope")
        return self


class SceneAssignment(_Value):
    item_id: UUID
    scene_id: UUID
    frame_index: Annotated[StrictInt, Field(ge=0)] | None


class PlannedScene(_Value):
    id: UUID
    name: Annotated[StrictStr, Field(max_length=255)]
    source_format: Annotated[StrictStr, Field(max_length=50)] | None
    source_metadata: dict[str, Any]


class SpatialLayoutPlan(_Value):
    scenes: list[PlannedScene]
    assignments: list[SceneAssignment]
    created_scenes: Count
    assigned_items: Count
    skipped_items: Count
    dry_run: StrictBool
    notes: list[StrictStr]

    @model_validator(mode="after")
    def check_plan(self):
        scene_ids = [scene.id for scene in self.scenes]
        item_ids = [entry.item_id for entry in self.assignments]
        if len(set(scene_ids)) != len(scene_ids) or len(set(item_ids)) != len(item_ids):
            raise ValueError("Duplicate Scene or assignment IDs")
        if any(entry.scene_id not in scene_ids for entry in self.assignments):
            raise ValueError("Layout assignment escaped its Scene plan")
        if self.created_scenes != len(self.scenes) or (
            not self.dry_run and self.assigned_items != len(self.assignments)
        ):
            raise ValueError("Layout counts disagree with the actual plan")
        return self


class ApplyLayoutRequest(_Value):
    operation_id: UUID
    actor_id: UUID | None
    dataset_id: UUID
    mode: SceneMode
    dry_run: StrictBool = False
    plan: SpatialLayoutPlan
    snapshot: dict[str, Any]

    @model_validator(mode="after")
    def check_dry_run(self):
        if self.dry_run != self.plan.dry_run:
            raise ValueError("Layout plan dry-run scope differs")
        return self


class BackfillScenesRequest(_Value):
    operation_id: UUID
    actor_id: UUID | None
    dataset_id: UUID
    mode: SceneMode = "auto"
    dry_run: StrictBool = False


class LayoutMutationResult(_Value):
    operation_id: UUID
    actor_id: UUID | None
    dataset_id: UUID
    created_scenes: Count
    assigned_items: Count
    skipped_items: Count
    dry_run: StrictBool
    notes: list[StrictStr]


class UpsertPosesRequest(_Value):
    operation_id: UUID
    scene_id: UUID
    poses: list[FramePose]


class PoseMutationResult(_Value):
    operation_id: UUID
    scene_id: UUID
    upserted: Count


class CreateSceneRequest(_Value):
    operation_id: UUID
    scene_id: UUID | None = None
    dataset_id: UUID
    name: Annotated[StrictStr, Field(max_length=255)]
    source_format: Annotated[StrictStr, Field(max_length=50)] | None = None
    source_metadata: dict[str, Any] = Field(default_factory=dict)
    created_by: UUID | None = None


class AssignItemsRequest(_Value):
    operation_id: UUID
    dataset_id: UUID
    assignments: list[SceneAssignment]


class AssignmentMutationResult(_Value):
    operation_id: UUID
    dataset_id: UUID
    assigned_items: Count


class SetSceneSourceRequest(_Value):
    operation_id: UUID
    scene_id: UUID
    source_format: Annotated[StrictStr, Field(max_length=50)] | None
    source_metadata: dict[str, Any]


class ReadCameraTargetRequest(_Value):
    actor_id: UUID
    task_id: UUID
    camera_role: StrictStr


class CameraTargetResult(_Value):
    actor_id: UUID
    task_id: UUID
    camera_role: StrictStr
    dataset_item_id: UUID


class UpdateCalibrationRequest(_Value):
    operation_id: UUID
    actor_id: UUID
    task_id: UUID
    camera_role: StrictStr
    expected_revision: Revision
    expected_digest: Digest
    calibration: SensorCalibration
    audit_context: dict[str, Any] = Field(default_factory=dict)


class ApplyCalibrationRequest(UpdateCalibrationRequest):
    canonical_document: StrictStr
    current_canonical_document: StrictStr
    source_snapshot: dict[str, Any]


class CalibrationMutationResult(_Value):
    operation_id: UUID
    actor_id: UUID
    task_id: UUID
    camera_role: StrictStr
    state: CalibrationState


class ReadPreparationRequest(_Value):
    creation_operation_id: UUID
    after_item_id: UUID | None = None
    page_size: PageSize = SPATIAL_PAGE_SIZE


class PreparationPage(LayoutPage):
    actor_id: None = None
    creation_operation_id: UUID


class CalibrationImportUpdate(_Value):
    dataset_item_id: UUID
    source_item_id: UUID
    source_snapshot: dict[str, Any]
    source_etag: StrictStr
    source_size: Count
    calibration: SensorCalibration


class ApplyPreparationRequest(_Value):
    creation_operation_id: UUID
    snapshot: dict[str, Any]
    calibration_updates: list[CalibrationImportUpdate]
    frame_plan: SpatialLayoutPlan


class PreparationMutationResult(_Value):
    creation_operation_id: UUID
    dataset_id: UUID
    calibrated_items: Count
    created_scenes: Count
    assigned_items: Count


class SniffAxisRequest(_Value):
    actor_id: UUID
    dataset_id: UUID


class AxisCandidate(_Value):
    convention: LidarAxisConvention
    score: float


class AxisCameraResult(_Value):
    camera_role: StrictStr | None
    best: LidarAxisConvention
    score: float


class AxisInferenceResult(_Value):
    best: LidarAxisConvention
    score: float
    candidates: list[AxisCandidate]
    source: Literal["task_link", "dataset_item"]
    camera_role: StrictStr | None
    camera_item_id: UUID
    per_camera: list[AxisCameraResult]
    agreement: float


class AxisSniffResult(_Value):
    actor_id: UUID
    dataset_id: UUID
    result: AxisInferenceResult | None


def _check_page(rows: list[Any], total: int, complete: bool, next_cursor: Any) -> None:
    if len(rows) > min(total, 500) or complete != (next_cursor is None):
        raise ValueError("Spatial page completion is inconsistent")
    if not complete and not rows:
        raise ValueError("Incomplete page must advance")


_Result = TypeVar("_Result", bound=BaseModel)


def decode_spatial_reply(payload: Any, result_type: type[_Result]) -> _Result:
    if not isinstance(payload, dict) or payload.get("schema") != SPATIAL_SCHEMA:
        raise ValueError("Unknown Assets spatial schema")
    if payload.get("ok") is True and set(payload) == {"schema", "ok", "data"}:
        return result_type.model_validate(payload["data"])
    if payload.get("ok") is False and set(payload) == {"schema", "ok", "error"}:
        error = payload["error"]
        if (
            isinstance(error, dict)
            and set(error) == {"code"}
            and isinstance(error["code"], str)
            and error["code"] in _FAILURE_CODES
        ):
            raise SpatialFailure(error["code"])
    raise ValueError("Malformed Assets spatial reply")


def canonical_calibration_document(calibration: SensorCalibration) -> str:
    return json.dumps(
        calibration.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


SPATIAL_NATIVE_SIGNATURES = {
    "guard_spatial_actor": "uuid,text",
    "list_spatial_scenes": "uuid,uuid,timestamp with time zone,uuid,integer",
    "get_spatial_scene": "uuid,uuid",
    "read_spatial_layout": "uuid,uuid,uuid,uuid,integer",
    "read_spatial_items": "uuid,uuid[],uuid,integer",
    "read_spatial_poses": "uuid,uuid,integer,integer",
    "read_spatial_calibrations": "uuid[],boolean,uuid,integer,integer",
    "lock_spatial_calibration_facts": "uuid,uuid,text,uuid,jsonb",
    "mutate_spatial_scene": "uuid,uuid,uuid,jsonb,jsonb",
    "apply_spatial_layout": "uuid,uuid,uuid,text,boolean,jsonb,jsonb",
    "upsert_spatial_poses": "uuid,uuid,jsonb",
    "create_spatial_scene": "uuid,uuid,uuid,text,text,jsonb,uuid",
    "assign_spatial_items": "uuid,uuid,jsonb",
    "set_spatial_scene_source": "uuid,uuid,text,jsonb",
    "update_spatial_calibration": "uuid,uuid,uuid,text,integer,text,jsonb,text,text,jsonb,jsonb",
    "read_pointcloud_preparation": "uuid,uuid,integer",
    "apply_pointcloud_preparation": "uuid,jsonb,jsonb,jsonb",
    "read_spatial_camera_target": "uuid,uuid,text",
}

SPATIAL_OPERATIONS = {
    "guard_spatial_actor": (GuardSpatialActorRequest, SpatialActorResult),
    "list_spatial_scenes": (ListScenesRequest, ScenePage),
    "get_spatial_scene": (GetSceneRequest, SceneResult),
    "read_spatial_layout": (ReadLayoutRequest, LayoutPage),
    "read_spatial_items": (ReadItemsRequest, ItemFactsPage),
    "read_spatial_poses": (ReadPosesRequest, PosePage),
    "read_spatial_calibrations": (ReadCalibrationsRequest, CalibrationPage),
    "lock_spatial_calibration_facts": (LockCalibrationFactsRequest, CalibrationRecord),
    "mutate_spatial_scene": (MutateSceneRequest, SceneMutationResult),
    "apply_spatial_layout": (ApplyLayoutRequest, LayoutMutationResult),
    "upsert_spatial_poses": (UpsertPosesRequest, PoseMutationResult),
    "create_spatial_scene": (CreateSceneRequest, SceneMutationResult),
    "assign_spatial_items": (AssignItemsRequest, AssignmentMutationResult),
    "set_spatial_scene_source": (SetSceneSourceRequest, SceneMutationResult),
    "update_spatial_calibration": (ApplyCalibrationRequest, CalibrationMutationResult),
    "read_pointcloud_preparation": (ReadPreparationRequest, PreparationPage),
    "apply_pointcloud_preparation": (
        ApplyPreparationRequest,
        PreparationMutationResult,
    ),
    "read_spatial_camera_target": (ReadCameraTargetRequest, CameraTargetResult),
}


def require_spatial_scope(request: BaseModel, result: BaseModel) -> None:
    """Reject replies for another operation, actor, resource, selector, or cursor."""
    for name in (
        "operation_id",
        "actor_id",
        "dataset_id",
        "scene_id",
        "task_id",
        "camera_role",
        "creation_operation_id",
        "dataset_item_id",
        "include_history",
        "after",
        "after_item_id",
        "after_frame_index",
        "dry_run",
    ):
        if name in type(request).model_fields and name in type(result).model_fields:
            if getattr(request, name) != getattr(result, name):
                raise ValueError(f"Assets spatial reply escaped {name}")
    for name in ("item_ids", "dataset_item_ids"):
        if name in type(request).model_fields and name in type(result).model_fields:
            left, right = getattr(request, name), getattr(result, name)
            if (left is None) != (right is None) or (
                left is not None
                and (len(right) != len(left) or set(left) != set(right))
            ):
                raise ValueError(f"Assets spatial reply escaped {name}")


def spatial_json_schema() -> dict[str, Any]:
    """Build the distributable schema from the exact native and HTTP wire values."""
    definitions: dict[str, Any] = {}
    models = set()
    for request, result in SPATIAL_OPERATIONS.values():
        models.update((request, result))
    models.update(
        (
            BackfillScenesRequest,
            UpdateCalibrationRequest,
            SniffAxisRequest,
            AxisSniffResult,
        )
    )
    for model in sorted(models, key=lambda value: value.__name__):
        schema = model.model_json_schema(ref_template="#/$defs/{model}")
        definitions.update(schema.pop("$defs", {}))
        definitions[model.__name__] = schema
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:aap:assets-spatial:v1",
        "title": "Assets spatial owner contract",
        "type": "object",
        "required": ["schema", "ok"],
        "properties": {"schema": {"const": SPATIAL_SCHEMA}, "ok": {"type": "boolean"}},
        "oneOf": [
            {
                "type": "object",
                "required": ["schema", "ok", "data"],
                "properties": {
                    "schema": {"const": SPATIAL_SCHEMA},
                    "ok": {"const": True},
                    "data": {
                        "anyOf": [
                            {"$ref": f"#/$defs/{model.__name__}"}
                            for model in sorted(
                                {result for _, result in SPATIAL_OPERATIONS.values()}
                                | {AxisSniffResult},
                                key=lambda value: value.__name__,
                            )
                        ]
                    },
                },
                "additionalProperties": False,
            },
            {
                "type": "object",
                "required": ["schema", "ok", "error"],
                "properties": {
                    "schema": {"const": SPATIAL_SCHEMA},
                    "ok": {"const": False},
                    "error": {
                        "type": "object",
                        "required": ["code"],
                        "properties": {
                            "code": {"enum": list(SpatialFailureCode.__args__)}
                        },
                        "additionalProperties": False,
                    },
                },
                "additionalProperties": False,
            },
        ],
        "$defs": definitions,
        "x-native-capabilities": {
            name: {
                "signature": f"aap_assets_v1.{name}({SPATIAL_NATIVE_SIGNATURES[name]})",
                "request": {"$ref": f"#/$defs/{request.__name__}"},
                "response": {"$ref": f"#/$defs/{result.__name__}"},
            }
            for name, (request, result) in SPATIAL_OPERATIONS.items()
        },
        "x-paging": {
            "snapshot": "opaque SHA256 of the complete native source snapshot",
            "completion": "stable scope, total, snapshot, monotone unique records, exact echoed cursors, complete record count",
        },
    }
