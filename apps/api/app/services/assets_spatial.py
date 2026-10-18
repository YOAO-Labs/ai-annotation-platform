"""Bind spatial reads and writes to the Assets owner capability surface."""

from __future__ import annotations

import json
import uuid
from typing import Any, TypeVar

from fastapi import HTTPException
from pydantic import BaseModel, ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aap_assets_catalog.spatial_contract import (
    SPATIAL_NATIVE_SIGNATURES,
    SPATIAL_NATIVE_ARGUMENTS,
    SPATIAL_OPERATIONS,
    ApplyCalibrationRequest,
    ApplyLayoutRequest,
    ApplyPreparationRequest,
    AssignItemsRequest,
    AssignmentMutationResult,
    AxisSniffResult,
    BackfillScenesRequest,
    CalibrationCursor,
    CalibrationMutationResult,
    DatasetAxisMutationResult,
    CalibrationPage,
    CalibrationRecord,
    CalibrationState,
    CameraTargetResult,
    CreateSceneRequest,
    GetSceneRequest,
    GuardSpatialActorRequest,
    ItemFactsPage,
    LayoutMutationResult,
    LayoutPage,
    ListScenesRequest,
    LockCalibrationFactsRequest,
    MutateSceneRequest,
    PoseMutationResult,
    PosePage,
    PreparationMutationResult,
    PreparationPage,
    PreparePointcloudRequest,
    ProvenanceMutationResult,
    ReadCalibrationsRequest,
    ReadCameraTargetRequest,
    ReadItemsRequest,
    ReadLayoutRequest,
    ReadPosesRequest,
    ReadPreparationRequest,
    RecordCalibrationProvenanceRequest,
    SceneCursor,
    SceneMutationResult,
    ScenePage,
    SceneResult,
    SetSceneSourceRequest,
    SetDatasetAxisRequest,
    SniffAxisRequest,
    SpatialAccess,
    SpatialActorResult,
    SpatialFailure,
    SpatialItem,
    SpatialScene,
    UpdateCalibrationRequest,
    UpsertPosesRequest,
    decode_spatial_reply,
    normalize_calibration_record,
    require_spatial_scope,
)
from aap_assets_catalog.spatial_values import FramePose

from app.services.assets_catalog import (
    AssetsCatalogGateway,
    CatalogGatewayError,
    _encode_request,
    _peer_url,
)
from app.services.storage import storage_service

_RESULT = TypeVar("_RESULT", bound=BaseModel)

_FAILURES: dict[str, tuple[int, str, dict[str, str] | None]] = {
    "invalid_request": (422, "Invalid Assets spatial request", None),
    "account_not_active": (
        401,
        "Account is not active",
        {"WWW-Authenticate": "Bearer"},
    ),
    "account_busy": (409, "Account is busy", None),
    "invalid_account_role": (403, "Account role is invalid", None),
    "catalog_manager_required": (403, "Catalog manager role required", None),
    "dataset_missing": (404, "Dataset not found", None),
    "scene_missing": (404, "Scene not found", None),
    "source_missing": (404, "Source item not found", None),
    "source_busy": (409, "Source item is busy", None),
    "dataset_busy": (409, "Dataset is busy", None),
    "scene_conflict": (409, "Scene name already exists", None),
    "source_changed": (409, "Source changed during the operation", None),
    "operation_conflict": (
        409,
        "Idempotency-Key conflicts with an earlier request",
        None,
    ),
    "operation_missing": (404, "Accepted operation was not found", None),
    "association_changed": (409, "Dataset association changed", None),
    "operation_cancelled": (409, "Accepted operation was cancelled", None),
    "project_missing": (404, "Project not found", None),
    "project_manager_required": (403, "Project manager access required", None),
    "task_missing": (404, "Task not found", None),
    "task_not_visible": (404, "Task not found", None),
    "task_busy": (409, "Task is busy", None),
    "annotation_write_forbidden": (403, "Annotation write access required", None),
    "camera_role_not_linked": (422, "Camera role is not linked to this task", None),
    "camera_dataset_item_missing": (404, "Camera item not found", None),
    "camera_calibration_missing": (404, "Camera calibration is missing", None),
    "camera_calibration_invalid": (409, "Camera calibration is invalid", None),
    "calibration_revision_conflict": (409, "Camera calibration changed", None),
    "camera_shared_across_projects": (
        403,
        "Only a super admin can update calibration shared across projects",
        None,
    ),
    "storage_unavailable": (503, "Assets source storage is unavailable", None),
    "resource_budget_exceeded": (
        422,
        "Assets spatial request exceeds its budget",
        None,
    ),
    "service_unavailable": (503, "Assets spatial service is unavailable", None),
    "invalid_response": (
        502,
        "Assets spatial service returned an invalid response",
        None,
    ),
}


class AssetsSpatialGatewayError(HTTPException):
    def __init__(self, code: str, message: str, status_code: int, headers=None):
        self.code = code
        self.message = message
        super().__init__(
            status_code, detail={"reason": code, "message": message}, headers=headers
        )


def _decode_spatial(payload: object, result_type: type[_RESULT]) -> _RESULT:
    try:
        return decode_spatial_reply(payload, result_type)
    except SpatialFailure as exc:
        status_code, message, headers = _FAILURES[exc.code]
        raise AssetsSpatialGatewayError(
            exc.code, message, status_code, headers
        ) from None
    except (ValidationError, TypeError, ValueError, KeyError) as exc:
        raise CatalogGatewayError(
            "invalid_response",
            "Assets spatial service returned an invalid response",
            502,
        ) from exc


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


class AssetsSpatialGateway:
    def __init__(self, db: AsyncSession):
        self.db = db
        self._catalog = AssetsCatalogGateway(db)

    def _spatial_workflow(self):
        from aap_assets_catalog.object_signer import SpatialCalibrationSourceReader
        from aap_assets_catalog.spatial_workflow import SpatialWorkflow

        return SpatialWorkflow(
            native=SameSessionAssetsSpatialGateway(self.db),
            source_reader=SpatialCalibrationSourceReader(
                storage_service.spatial_source_client,
                storage_service.datasets_bucket,
            ),
        )

    async def _call(
        self,
        operation: str,
        request: BaseModel,
        arguments: tuple[Any, ...],
        result_type: type[_RESULT],
        *,
        same_session_native: bool = False,
    ) -> tuple[_RESULT, bool]:
        if SPATIAL_OPERATIONS[operation] != (type(request), result_type):
            raise TypeError(f"Invalid request/result pair for {operation}")
        casts = SPATIAL_NATIVE_SIGNATURES[operation].split(",")
        argument_names = SPATIAL_NATIVE_ARGUMENTS[operation]
        if len(arguments) != len(casts) or len(argument_names) != len(casts):
            raise TypeError(f"Invalid native argument count for {operation}")
        placeholders = ", ".join(
            f"CAST(:{name} AS {type_name})"
            for name, type_name in zip(argument_names, casts, strict=True)
        )
        parameters = {
            name: (
                json.dumps(_json_value(value), separators=(",", ":"), allow_nan=False)
                if type_name == "jsonb"
                else value
            )
            for name, type_name, value in zip(
                argument_names, casts, arguments, strict=True
            )
        }
        reply, remote = await self._catalog._call(
            operation,
            f"SELECT aap_assets_v1.{operation}({placeholders})",
            parameters,
            _encode_request(request),
            result_type,
            decoder=_decode_spatial,
            namespace="spatial",
            same_session_native=same_session_native,
        )
        try:
            require_spatial_scope(request, reply)
        except (TypeError, ValueError, KeyError) as exc:
            raise CatalogGatewayError(
                "invalid_response",
                "Assets spatial reply escaped its request scope",
                502,
            ) from exc
        return reply, remote

    async def _remote_workflow(
        self,
        operation: str,
        request: BaseModel,
        result_type: type[_RESULT],
    ) -> _RESULT:
        result, _ = await self._catalog._call(
            operation,
            "SELECT NULL",
            {},
            _encode_request(request),
            result_type,
            decoder=_decode_spatial,
            namespace="spatial",
            remote_only=True,
        )
        try:
            require_spatial_scope(request, result)
        except (TypeError, ValueError, KeyError) as exc:
            raise self._invalid_response(
                "Assets workflow reply escaped request scope"
            ) from exc
        return result

    async def guard_spatial_actor(
        self, request: GuardSpatialActorRequest
    ) -> SpatialActorResult:
        result, _ = await self._call(
            "guard_spatial_actor",
            request,
            (request.actor_id, request.access),
            SpatialActorResult,
        )
        return result

    async def list_spatial_scenes(
        self,
        request: ListScenesRequest,
        *,
        same_session_native: bool = False,
    ) -> ScenePage:
        cursor = request.after
        result, _ = await self._call(
            "list_spatial_scenes",
            request,
            (
                request.actor_id,
                request.dataset_id,
                cursor.created_at if cursor else None,
                cursor.scene_id if cursor else None,
                request.page_size,
            ),
            ScenePage,
            same_session_native=same_session_native,
        )
        return result

    async def get_spatial_scene(self, request: GetSceneRequest) -> SceneResult:
        result, _ = await self._call(
            "get_spatial_scene",
            request,
            (request.actor_id, request.scene_id),
            SceneResult,
        )
        return result

    async def read_spatial_layout(self, request: ReadLayoutRequest) -> LayoutPage:
        result, _ = await self._call(
            "read_spatial_layout",
            request,
            (
                request.actor_id,
                request.dataset_id,
                request.creation_operation_id,
                request.after_item_id,
                request.page_size,
            ),
            LayoutPage,
        )
        return result

    async def read_spatial_items(self, request: ReadItemsRequest) -> ItemFactsPage:
        result, _ = await self._call(
            "read_spatial_items",
            request,
            (
                request.dataset_id,
                request.item_ids,
                request.after_item_id,
                request.page_size,
            ),
            ItemFactsPage,
        )
        return result

    async def read_spatial_poses(self, request: ReadPosesRequest) -> PosePage:
        result, _ = await self._call(
            "read_spatial_poses",
            request,
            (
                request.actor_id,
                request.scene_id,
                request.after_frame_index,
                request.page_size,
            ),
            PosePage,
        )
        return result

    async def read_spatial_calibrations(
        self, request: ReadCalibrationsRequest
    ) -> CalibrationPage:
        cursor = request.after
        result, _ = await self._call(
            "read_spatial_calibrations",
            request,
            (
                request.dataset_item_ids,
                request.include_history,
                cursor.dataset_item_id if cursor else None,
                cursor.revision if cursor else None,
                request.page_size,
            ),
            CalibrationPage,
        )
        return result

    async def lock_spatial_calibration_facts(
        self, request: LockCalibrationFactsRequest
    ) -> CalibrationRecord:
        result, _ = await self._call(
            "lock_spatial_calibration_facts",
            request,
            (
                request.actor_id,
                request.task_id,
                request.camera_role,
                request.dataset_item_id,
                request.expected_source_snapshot,
            ),
            CalibrationRecord,
        )
        return result

    async def mutate_spatial_scene(
        self, request: MutateSceneRequest
    ) -> SceneMutationResult:
        result, _ = await self._call(
            "mutate_spatial_scene",
            request,
            (
                request.operation_id,
                request.actor_id,
                request.scene_id,
                request.patch.native_payload(),
                request.audit_context,
            ),
            SceneMutationResult,
        )
        return result

    async def apply_spatial_layout(
        self, request: ApplyLayoutRequest
    ) -> LayoutMutationResult:
        result, _ = await self._call(
            "apply_spatial_layout",
            request,
            (
                request.operation_id,
                request.actor_id,
                request.dataset_id,
                request.mode,
                request.dry_run,
                request.plan,
                request.snapshot,
            ),
            LayoutMutationResult,
        )
        return result

    async def upsert_spatial_poses(
        self, request: UpsertPosesRequest
    ) -> PoseMutationResult:
        result, _ = await self._call(
            "upsert_spatial_poses",
            request,
            (request.operation_id, request.scene_id, request.poses),
            PoseMutationResult,
        )
        return result

    async def create_spatial_scene(
        self, request: CreateSceneRequest
    ) -> SceneMutationResult:
        result, _ = await self._call(
            "create_spatial_scene",
            request,
            (
                request.operation_id,
                request.scene_id,
                request.dataset_id,
                request.name,
                request.source_format,
                request.source_metadata,
                request.created_by,
            ),
            SceneMutationResult,
        )
        return result

    async def assign_spatial_items(
        self, request: AssignItemsRequest
    ) -> AssignmentMutationResult:
        result, _ = await self._call(
            "assign_spatial_items",
            request,
            (request.operation_id, request.dataset_id, request.assignments),
            AssignmentMutationResult,
        )
        return result

    async def set_spatial_scene_source(
        self, request: SetSceneSourceRequest
    ) -> SceneMutationResult:
        result, _ = await self._call(
            "set_spatial_scene_source",
            request,
            (
                request.operation_id,
                request.scene_id,
                request.source_format,
                request.source_metadata,
            ),
            SceneMutationResult,
        )
        return result

    async def update_spatial_calibration(
        self, request: ApplyCalibrationRequest
    ) -> CalibrationMutationResult:
        result, _ = await self._call(
            "update_spatial_calibration",
            request,
            (
                request.operation_id,
                request.actor_id,
                request.task_id,
                request.camera_role,
                request.expected_revision,
                request.expected_digest,
                request.calibration,
                request.canonical_document,
                request.current_canonical_document,
                request.source_snapshot,
                request.audit_context,
            ),
            CalibrationMutationResult,
        )
        return result

    async def read_pointcloud_preparation(
        self, request: ReadPreparationRequest
    ) -> PreparationPage:
        result, _ = await self._call(
            "read_pointcloud_preparation",
            request,
            (
                request.creation_operation_id,
                request.after_item_id,
                request.page_size,
            ),
            PreparationPage,
        )
        return result

    async def apply_pointcloud_preparation_native(
        self, request: ApplyPreparationRequest
    ) -> PreparationMutationResult:
        result, _ = await self._call(
            "apply_pointcloud_preparation",
            request,
            (
                request.creation_operation_id,
                request.snapshot,
                request.calibration_updates,
                request.frame_plan,
            ),
            PreparationMutationResult,
        )
        return result

    async def read_spatial_camera_target(
        self, request: ReadCameraTargetRequest
    ) -> CameraTargetResult:
        result, _ = await self._call(
            "read_spatial_camera_target",
            request,
            (request.actor_id, request.task_id, request.camera_role),
            CameraTargetResult,
        )
        return result

    async def record_spatial_calibration_provenance(
        self, request: RecordCalibrationProvenanceRequest
    ) -> ProvenanceMutationResult:
        result, _ = await self._call(
            "record_spatial_calibration_provenance",
            request,
            (request.operation_id, request.dataset_id, request.entries),
            ProvenanceMutationResult,
        )
        return result

    async def set_spatial_dataset_axis(
        self,
        request: SetDatasetAxisRequest,
        *,
        same_session_native: bool = False,
    ) -> DatasetAxisMutationResult:
        result, _ = await self._call(
            "set_spatial_dataset_axis",
            request,
            (request.operation_id, request.dataset_id, request.axis_convention),
            DatasetAxisMutationResult,
            same_session_native=same_session_native,
        )
        return result

    async def set_dataset_axis(
        self,
        request: SetDatasetAxisRequest,
        *,
        same_session_native: bool = False,
        commit: bool = True,
    ) -> DatasetAxisMutationResult:
        result = await self.set_spatial_dataset_axis(
            request, same_session_native=same_session_native
        )
        if commit and not same_session_native and _peer_url() is None:
            await self.db.commit()
        return result

    async def backfill_scenes(
        self, request: BackfillScenesRequest, *, commit: bool = True
    ) -> LayoutMutationResult:
        if _peer_url() is not None:
            return await self._remote_workflow(
                "backfill_scenes", request, LayoutMutationResult
            )
        result = await self._spatial_workflow().backfill_scenes(
            request, native=SameSessionAssetsSpatialGateway(self.db)
        )
        require_spatial_scope(request, result)
        if commit and not request.dry_run:
            await self.db.commit()
        return result

    async def sniff_axis(self, request: SniffAxisRequest) -> AxisSniffResult:
        if _peer_url() is not None:
            return await self._remote_workflow("sniff_axis", request, AxisSniffResult)
        result = await self._spatial_workflow().sniff_axis(
            request, native=SameSessionAssetsSpatialGateway(self.db)
        )
        require_spatial_scope(request, result)
        return result

    async def prepare_pointcloud(
        self,
        request: PreparePointcloudRequest,
        *,
        same_session_native: bool = False,
        commit: bool = True,
    ) -> PreparationMutationResult:
        if not same_session_native:
            if _peer_url() is not None:
                return await self._remote_workflow(
                    "prepare_pointcloud", request, PreparationMutationResult
                )
        result = await self._spatial_workflow().prepare_pointcloud(
            request, native=SameSessionAssetsSpatialGateway(self.db)
        )
        require_spatial_scope(request, result)
        if commit and not same_session_native:
            await self.db.commit()
        return result

    async def guard_actor(
        self, actor_id: uuid.UUID, access: SpatialAccess
    ) -> SpatialActorResult:
        request = GuardSpatialActorRequest(actor_id=actor_id, access=access)
        result, _ = await self._call(
            "guard_spatial_actor",
            request,
            (request.actor_id, request.access),
            SpatialActorResult,
        )
        return result

    async def lock_scene_source_references(self, scene_ids: list[uuid.UUID]) -> bool:
        result = await self.db.scalar(
            text(
                "SELECT aap_assets_v1.lock_scene_source_references(CAST(:ids AS uuid[]))"
            ),
            {"ids": scene_ids},
        )
        if type(result) is not bool:
            raise self._invalid_response("Invalid scene source reference guard result")
        return result

    async def list_scenes(
        self,
        request: ListScenesRequest,
        *,
        same_session_native: bool = False,
    ) -> list[SpatialScene]:
        scenes: list[SpatialScene] = []
        cursor = request.after
        digest: str | None = None
        total: int | None = None
        seen: set[uuid.UUID] = set()
        while True:
            page_request = request.model_copy(update={"after": cursor})
            page, _ = await self._call(
                "list_spatial_scenes",
                page_request,
                (
                    page_request.actor_id,
                    page_request.dataset_id,
                    cursor.created_at if cursor else None,
                    cursor.scene_id if cursor else None,
                    page_request.page_size,
                ),
                ScenePage,
                same_session_native=same_session_native,
            )
            if digest is None:
                digest, total = page.snapshot_digest, page.total
            elif (digest, total) != (page.snapshot_digest, page.total):
                raise self._invalid_response("Scene list snapshot changed while paging")
            keys = [(row.created_at, row.id) for row in page.scenes]
            if keys != sorted(keys) or any(
                scene_id in seen for scene_id in (row.id for row in page.scenes)
            ):
                raise self._invalid_response(
                    "Scene list page is not ordered and unique"
                )
            if (
                cursor is not None
                and keys
                and keys[0] <= (cursor.created_at, cursor.scene_id)
            ):
                raise self._invalid_response("Scene list cursor did not advance")
            if not page.complete and page.next_cursor != SceneCursor(
                created_at=page.scenes[-1].created_at,
                scene_id=page.scenes[-1].id,
            ):
                raise self._invalid_response(
                    "Scene list cursor does not match its page"
                )
            seen.update(row.id for row in page.scenes)
            scenes.extend(page.scenes)
            if page.complete:
                break
            cursor = page.next_cursor
        if total != len(scenes):
            raise self._invalid_response("Scene list was incomplete")
        return scenes

    async def has_scenes(self, dataset_id: uuid.UUID) -> bool:
        page = await self.list_spatial_scenes(
            ListScenesRequest(actor_id=None, dataset_id=dataset_id, page_size=1)
        )
        return page.total > 0

    async def get_scene(self, request: GetSceneRequest) -> SceneResult:
        result, _ = await self._call(
            "get_spatial_scene",
            request,
            (request.actor_id, request.scene_id),
            SceneResult,
        )
        return result

    async def read_layout(self, request: ReadLayoutRequest) -> LayoutPage:
        items: list[SpatialItem] = []
        scenes: dict[uuid.UUID, SpatialScene] = {}
        cursor = request.after_item_id
        first: LayoutPage | None = None
        while True:
            page_request = request.model_copy(update={"after_item_id": cursor})
            page, _ = await self._call(
                "read_spatial_layout",
                page_request,
                (
                    page_request.actor_id,
                    page_request.dataset_id,
                    page_request.creation_operation_id,
                    cursor,
                    page_request.page_size,
                ),
                LayoutPage,
            )
            if first is None:
                first = page
            elif self._layout_snapshot(page) != self._layout_snapshot(first):
                raise self._invalid_response("Layout snapshot changed while paging")
            self._extend_scenes(scenes, page.scenes)
            keys = [item.id for item in page.items]
            if keys != sorted(keys) or any(
                item_id in {item.id for item in items} for item_id in keys
            ):
                raise self._invalid_response(
                    "Layout item page is not ordered and unique"
                )
            if cursor is not None and keys and keys[0] <= cursor:
                raise self._invalid_response("Layout item cursor did not advance")
            if not page.complete and page.next_item_id != page.items[-1].id:
                raise self._invalid_response(
                    "Layout item cursor does not match its page"
                )
            items.extend(page.items)
            if page.complete:
                break
            cursor = page.next_item_id
        assert first is not None
        if len(items) != first.total or len(scenes) != first.scene_count:
            raise self._invalid_response("Layout snapshot was incomplete")
        return first.model_copy(
            update={"items": items, "scenes": list(scenes.values())}
        )

    async def read_items(
        self,
        request: ReadItemsRequest,
        *,
        same_session_native: bool = False,
    ) -> ItemFactsPage:
        items: list[SpatialItem] = []
        scenes: dict[uuid.UUID, SpatialScene] = {}
        cursor = request.after_item_id
        first: ItemFactsPage | None = None
        while True:
            page_request = request.model_copy(update={"after_item_id": cursor})
            page, _ = await self._call(
                "read_spatial_items",
                page_request,
                (
                    page_request.dataset_id,
                    page_request.item_ids,
                    cursor,
                    page_request.page_size,
                ),
                ItemFactsPage,
                same_session_native=same_session_native,
            )
            if first is None:
                first = page
            elif (page.snapshot_digest, page.total) != (
                first.snapshot_digest,
                first.total,
            ):
                raise self._invalid_response("Item facts snapshot changed while paging")
            self._extend_scenes(scenes, page.scenes)
            keys = [item.id for item in page.items]
            if keys != sorted(keys) or any(
                item_id in {item.id for item in items} for item_id in keys
            ):
                raise self._invalid_response(
                    "Item facts page is not ordered and unique"
                )
            if cursor is not None and keys and keys[0] <= cursor:
                raise self._invalid_response("Item facts cursor did not advance")
            if not page.complete and page.next_item_id != page.items[-1].id:
                raise self._invalid_response(
                    "Item facts cursor does not match its page"
                )
            items.extend(page.items)
            if page.complete:
                break
            cursor = page.next_item_id
        assert first is not None
        if len(items) != first.total:
            raise self._invalid_response("Item facts snapshot was incomplete")
        return first.model_copy(
            update={"items": items, "scenes": list(scenes.values())}
        )

    async def read_poses(self, request: ReadPosesRequest) -> list[FramePose]:
        poses: list[FramePose] = []
        cursor = request.after_frame_index
        digest: str | None = None
        total: int | None = None
        while True:
            page_request = request.model_copy(update={"after_frame_index": cursor})
            page, _ = await self._call(
                "read_spatial_poses",
                page_request,
                (
                    page_request.actor_id,
                    page_request.scene_id,
                    cursor,
                    page_request.page_size,
                ),
                PosePage,
            )
            if digest is None:
                digest, total = page.snapshot_digest, page.total
            elif (digest, total) != (page.snapshot_digest, page.total):
                raise self._invalid_response("Pose snapshot changed while paging")
            indexes = [pose.frame_index for pose in page.poses]
            if indexes != sorted(set(indexes)):
                raise self._invalid_response("Pose page is not ordered and unique")
            if cursor is not None and indexes and indexes[0] <= cursor:
                raise self._invalid_response("Pose cursor did not advance")
            if (
                not page.complete
                and page.next_frame_index != page.poses[-1].frame_index
            ):
                raise self._invalid_response("Pose cursor does not match its page")
            poses.extend(page.poses)
            if page.complete:
                break
            cursor = page.next_frame_index
        if total != len(poses):
            raise self._invalid_response("Pose snapshot was incomplete")
        return poses

    async def read_frame_pose(
        self,
        scene_id: uuid.UUID,
        frame_index: int,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> FramePose | None:
        page = await self.read_spatial_poses(
            ReadPosesRequest(
                actor_id=actor_id,
                scene_id=scene_id,
                after_frame_index=frame_index - 1,
                page_size=1,
            )
        )
        return (
            page.poses[0]
            if page.poses and page.poses[0].frame_index == frame_index
            else None
        )

    async def read_calibrations(
        self,
        dataset_item_ids: list[uuid.UUID],
        *,
        include_history: bool = False,
        same_session_native: bool = False,
    ) -> list[CalibrationRecord]:
        records: list[CalibrationRecord] = []
        for start in range(0, len(dataset_item_ids), 5000):
            ids = dataset_item_ids[start : start + 5000]
            if not ids:
                continue
            request = ReadCalibrationsRequest(
                dataset_item_ids=ids, include_history=include_history
            )
            cursor: CalibrationCursor | None = None
            first: CalibrationPage | None = None
            page_records: list[CalibrationRecord] = []
            seen: set[tuple[uuid.UUID, int]] = set()
            while True:
                page_request = request.model_copy(update={"after": cursor})
                page, _ = await self._call(
                    "read_spatial_calibrations",
                    page_request,
                    (
                        page_request.dataset_item_ids,
                        page_request.include_history,
                        cursor.dataset_item_id if cursor else None,
                        cursor.revision if cursor else None,
                        page_request.page_size,
                    ),
                    CalibrationPage,
                    same_session_native=same_session_native,
                )
                if first is None:
                    first = page
                elif (page.snapshot_digest, page.total) != (
                    first.snapshot_digest,
                    first.total,
                ):
                    raise self._invalid_response(
                        "Calibration snapshot changed while paging"
                    )
                identity_keys = [
                    (row.dataset_item_id, row.revision) for row in page.records
                ]
                order_keys = [
                    (row.dataset_item_id, -row.revision) for row in page.records
                ]
                if order_keys != sorted(order_keys) or any(
                    key in seen for key in identity_keys
                ):
                    raise self._invalid_response(
                        "Calibration page is not ordered and unique"
                    )
                if (
                    cursor is not None
                    and order_keys
                    and order_keys[0]
                    <= (
                        cursor.dataset_item_id,
                        -cursor.revision,
                    )
                ):
                    raise self._invalid_response("Calibration cursor did not advance")
                if not page.complete:
                    expected = CalibrationCursor(
                        dataset_item_id=page.records[-1].dataset_item_id,
                        revision=page.records[-1].revision,
                    )
                    if page.next_cursor != expected:
                        raise self._invalid_response(
                            "Calibration cursor does not match its page"
                        )
                seen.update(identity_keys)
                page_records.extend(page.records)
                if page.complete:
                    break
                cursor = page.next_cursor
            assert first is not None
            if len(page_records) != first.total:
                raise self._invalid_response("Calibration snapshot was incomplete")
            records.extend(page_records)
        return records

    async def read_calibration_state(
        self,
        dataset_item_id: uuid.UUID,
        *,
        same_session_native: bool = False,
    ) -> CalibrationState:
        records = await self.read_calibrations(
            [dataset_item_id], same_session_native=same_session_native
        )
        if not records:
            raise SpatialFailure("camera_dataset_item_missing")
        return normalize_calibration_record(records[0])

    async def read_calibration_history(
        self, dataset_item_id: uuid.UUID
    ) -> list[CalibrationState]:
        records = await self.read_calibrations([dataset_item_id], include_history=True)
        if not records:
            raise SpatialFailure("camera_dataset_item_missing")
        return [normalize_calibration_record(record) for record in records]

    async def lock_calibration_facts(
        self,
        request: LockCalibrationFactsRequest,
        *,
        same_session_native: bool = True,
    ) -> CalibrationRecord:
        result, _ = await self._call(
            "lock_spatial_calibration_facts",
            request,
            (
                request.actor_id,
                request.task_id,
                request.camera_role,
                request.dataset_item_id,
                request.expected_source_snapshot,
            ),
            CalibrationRecord,
            same_session_native=same_session_native,
        )
        return result

    async def read_camera_target(
        self, request: ReadCameraTargetRequest
    ) -> CameraTargetResult:
        result, _ = await self._call(
            "read_spatial_camera_target",
            request,
            (request.actor_id, request.task_id, request.camera_role),
            CameraTargetResult,
        )
        return result

    async def mutate_scene(
        self,
        request: MutateSceneRequest,
        *,
        commit: bool = True,
    ) -> SceneMutationResult:
        result, remote = await self._call(
            "mutate_spatial_scene",
            request,
            (
                request.operation_id,
                request.actor_id,
                request.scene_id,
                request.patch.native_payload(),
                request.audit_context,
            ),
            SceneMutationResult,
        )
        if commit and not remote:
            await self.db.commit()
        return result

    async def apply_layout(
        self,
        request: ApplyLayoutRequest,
        *,
        commit: bool = True,
    ) -> LayoutMutationResult:
        result, remote = await self._call(
            "apply_spatial_layout",
            request,
            (
                request.operation_id,
                request.actor_id,
                request.dataset_id,
                request.mode,
                request.dry_run,
                request.plan,
                request.snapshot,
            ),
            LayoutMutationResult,
        )
        if commit and not remote:
            await self.db.commit()
        return result

    async def create_scene(
        self,
        request: CreateSceneRequest,
        *,
        same_session_native: bool = False,
    ) -> SceneMutationResult:
        result, _ = await self._call(
            "create_spatial_scene",
            request,
            (
                request.operation_id,
                request.scene_id,
                request.dataset_id,
                request.name,
                request.source_format,
                request.source_metadata,
                request.created_by,
            ),
            SceneMutationResult,
            same_session_native=same_session_native,
        )
        return result

    async def assign_items(
        self,
        request: AssignItemsRequest,
        *,
        same_session_native: bool = False,
    ) -> AssignmentMutationResult:
        result, _ = await self._call(
            "assign_spatial_items",
            request,
            (request.operation_id, request.dataset_id, request.assignments),
            AssignmentMutationResult,
            same_session_native=same_session_native,
        )
        return result

    async def set_scene_source(
        self,
        request: SetSceneSourceRequest,
        *,
        same_session_native: bool = False,
    ) -> SceneMutationResult:
        result, _ = await self._call(
            "set_spatial_scene_source",
            request,
            (
                request.operation_id,
                request.scene_id,
                request.source_format,
                request.source_metadata,
            ),
            SceneMutationResult,
            same_session_native=same_session_native,
        )
        return result

    async def upsert_poses(
        self,
        request: UpsertPosesRequest,
        *,
        same_session_native: bool = False,
    ) -> PoseMutationResult:
        result, _ = await self._call(
            "upsert_spatial_poses",
            request,
            (request.operation_id, request.scene_id, request.poses),
            PoseMutationResult,
            same_session_native=same_session_native,
        )
        return result

    async def update_calibration(
        self,
        request: UpdateCalibrationRequest,
        *,
        same_session_native: bool = False,
        commit: bool = True,
    ) -> CalibrationMutationResult:
        if not same_session_native and _peer_url() is not None:
            return await self._remote_workflow(
                "update_calibration", request, CalibrationMutationResult
            )
        result = await self._spatial_workflow().update_calibration(
            request, native=SameSessionAssetsSpatialGateway(self.db)
        )
        try:
            require_spatial_scope(request, result)
        except (TypeError, ValueError, KeyError) as exc:
            raise self._invalid_response(
                "Calibration reply escaped request scope"
            ) from exc
        if commit and not same_session_native:
            await self.db.commit()
        return result

    async def apply_pointcloud_preparation(
        self,
        request: ApplyPreparationRequest,
        *,
        commit: bool = True,
    ) -> PreparationMutationResult:
        result, remote = await self._call(
            "apply_pointcloud_preparation",
            request,
            (
                request.creation_operation_id,
                request.snapshot,
                request.calibration_updates,
                request.frame_plan,
            ),
            PreparationMutationResult,
            same_session_native=not commit,
        )
        if commit and not remote:
            await self.db.commit()
        return result

    async def record_calibration_provenance(
        self,
        request: RecordCalibrationProvenanceRequest,
    ) -> ProvenanceMutationResult:
        result, _ = await self._call(
            "record_spatial_calibration_provenance",
            request,
            (request.operation_id, request.dataset_id, request.entries),
            ProvenanceMutationResult,
            same_session_native=True,
        )
        return result

    async def read_layout_for_operation(
        self,
        request: ReadPreparationRequest,
        *,
        same_session_native: bool = False,
    ) -> PreparationPage:
        items: list[SpatialItem] = []
        scenes: dict[uuid.UUID, SpatialScene] = {}
        cursor = request.after_item_id
        first: PreparationPage | None = None
        while True:
            page_request = request.model_copy(update={"after_item_id": cursor})
            page, _ = await self._call(
                "read_pointcloud_preparation",
                page_request,
                (
                    page_request.creation_operation_id,
                    cursor,
                    page_request.page_size,
                ),
                PreparationPage,
                same_session_native=same_session_native,
            )
            if first is None:
                first = page
            elif self._layout_snapshot(page) != self._layout_snapshot(first):
                raise self._invalid_response(
                    "Preparation snapshot changed while paging"
                )
            self._extend_scenes(scenes, page.scenes)
            keys = [item.id for item in page.items]
            if keys != sorted(keys) or any(
                item_id in {item.id for item in items} for item_id in keys
            ):
                raise self._invalid_response(
                    "Preparation item page is not ordered and unique"
                )
            if cursor is not None and keys and keys[0] <= cursor:
                raise self._invalid_response("Preparation item cursor did not advance")
            if not page.complete and page.next_item_id != page.items[-1].id:
                raise self._invalid_response(
                    "Preparation cursor does not match its page"
                )
            items.extend(page.items)
            if page.complete:
                break
            cursor = page.next_item_id
        assert first is not None
        if len(items) != first.total or len(scenes) != first.scene_count:
            raise self._invalid_response("Preparation snapshot was incomplete")
        return first.model_copy(
            update={"items": items, "scenes": list(scenes.values())}
        )

    @staticmethod
    def _layout_snapshot(page: LayoutPage) -> tuple[Any, ...]:
        return (
            page.snapshot_digest,
            page.total,
            page.dataset_name,
            page.is_temporal,
            page.axis_convention,
            page.scene_count,
            page.assigned_count,
        )

    @staticmethod
    def _extend_scenes(
        scenes: dict[uuid.UUID, SpatialScene], incoming: list[SpatialScene]
    ) -> None:
        for scene in incoming:
            existing = scenes.setdefault(scene.id, scene)
            if existing != scene:
                raise AssetsSpatialGateway._invalid_response(
                    "Spatial pages contain inconsistent Scene facts"
                )

    @staticmethod
    def _invalid_response(message: str) -> CatalogGatewayError:
        return CatalogGatewayError("invalid_response", message, 502)


class SameSessionAssetsSpatialGateway(AssetsSpatialGateway):
    """Native spatial binding for workflows that share a caller transaction."""

    async def _call(
        self,
        operation: str,
        request: BaseModel,
        arguments: tuple[Any, ...],
        result_type: type[_RESULT],
        *,
        same_session_native: bool = False,
    ) -> tuple[_RESULT, bool]:
        return await super()._call(
            operation,
            request,
            arguments,
            result_type,
            same_session_native=True,
        )
