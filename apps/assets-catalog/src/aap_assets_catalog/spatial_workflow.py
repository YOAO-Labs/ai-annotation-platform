"""Framework-free orchestration for the complete Assets spatial boundary.

The workflow owns only detached contract values. A native binding may be the
Catalog's independent database adapter or a caller-owned same-transaction
capability; the workflow never opens, commits, or stores a database session.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Protocol, TypeVar
from uuid import UUID, uuid5

from aap_assets_source.frame_layout import (
    PreparedDatasetItem,
    group_frames,
    source_ingest_logical_path,
)
from aap_assets_source.scene_layout import (
    IngestScenePlan,
    SceneLayoutInput,
    plan_scenes,
)
from pydantic import BaseModel, ValidationError

from . import spatial_contract as spatial
from .axis_sniff import AxisObservation, sniff_axis_convention
from .object_signer import (
    SourceObjectFacts,
    SourceReadError,
)
from .spatial_pages import SpatialPage, SpatialPageError, collect_spatial_pages
from .spatial_values import SensorCalibration

logger = logging.getLogger("aap_assets_catalog.spatial")


class SpatialNativeBinding(Protocol):
    """Exact request/result capability surface, with no framework dependency."""

    async def guard_spatial_actor(
        self, request: spatial.GuardSpatialActorRequest
    ) -> spatial.SpatialActorResult: ...

    async def list_spatial_scenes(
        self, request: spatial.ListScenesRequest
    ) -> spatial.ScenePage: ...

    async def get_spatial_scene(
        self, request: spatial.GetSceneRequest
    ) -> spatial.SceneResult: ...

    async def read_spatial_layout(
        self, request: spatial.ReadLayoutRequest
    ) -> spatial.LayoutPage: ...

    async def read_spatial_items(
        self, request: spatial.ReadItemsRequest
    ) -> spatial.ItemFactsPage: ...

    async def read_spatial_poses(
        self, request: spatial.ReadPosesRequest
    ) -> spatial.PosePage: ...

    async def read_spatial_calibrations(
        self, request: spatial.ReadCalibrationsRequest
    ) -> spatial.CalibrationPage: ...

    async def lock_spatial_calibration_facts(
        self, request: spatial.LockCalibrationFactsRequest
    ) -> spatial.CalibrationRecord: ...

    async def mutate_spatial_scene(
        self, request: spatial.MutateSceneRequest
    ) -> spatial.SceneMutationResult: ...

    async def apply_spatial_layout(
        self, request: spatial.ApplyLayoutRequest
    ) -> spatial.LayoutMutationResult: ...

    async def upsert_spatial_poses(
        self, request: spatial.UpsertPosesRequest
    ) -> spatial.PoseMutationResult: ...

    async def create_spatial_scene(
        self, request: spatial.CreateSceneRequest
    ) -> spatial.SceneMutationResult: ...

    async def assign_spatial_items(
        self, request: spatial.AssignItemsRequest
    ) -> spatial.AssignmentMutationResult: ...

    async def set_spatial_scene_source(
        self, request: spatial.SetSceneSourceRequest
    ) -> spatial.SceneMutationResult: ...

    async def update_spatial_calibration(
        self, request: spatial.ApplyCalibrationRequest
    ) -> spatial.CalibrationMutationResult: ...

    async def read_pointcloud_preparation(
        self, request: spatial.ReadPreparationRequest
    ) -> spatial.PreparationPage: ...

    async def apply_pointcloud_preparation(
        self, request: spatial.ApplyPreparationRequest
    ) -> spatial.PreparationMutationResult: ...

    async def read_spatial_camera_target(
        self, request: spatial.ReadCameraTargetRequest
    ) -> spatial.CameraTargetResult: ...

    async def record_spatial_calibration_provenance(
        self, request: spatial.RecordCalibrationProvenanceRequest
    ) -> spatial.ProvenanceMutationResult: ...


class CalibrationSourceReader(Protocol):
    """Read one object from a preconfigured, fixed datasets bucket."""

    def read_calibration_source(
        self,
        key: str,
        *,
        expected_etag: str | None = None,
        expected_size: int | None = None,
    ) -> tuple[SourceObjectFacts, bytes]: ...


@dataclass(frozen=True, slots=True)
class SpatialLayoutFacts:
    actor_id: UUID | None
    dataset_id: UUID
    creation_operation_id: UUID | None
    dataset_name: str
    is_temporal: bool
    axis_convention: spatial.LidarAxisConvention | None
    scene_count: int
    assigned_count: int
    snapshot_digest: str
    items: tuple[spatial.SpatialItem, ...]
    scenes: tuple[spatial.SpatialScene, ...]


@dataclass(frozen=True, slots=True)
class SpatialItemFacts:
    dataset_id: UUID | None
    item_ids: tuple[UUID, ...] | None
    snapshot_digest: str
    items: tuple[spatial.SpatialItem, ...]
    scenes: tuple[spatial.SpatialScene, ...]


@dataclass(frozen=True, slots=True)
class PointcloudPreparationCapture:
    """Complete accepted preparation facts captured before external I/O."""

    layout: SpatialLayoutFacts
    frame_plan: spatial.SpatialLayoutPlan
    calibration_items: tuple[tuple[str, spatial.SpatialItem], ...]
    camera_items_by_role: dict[str, tuple[spatial.SpatialItem, ...]]


_Request = TypeVar("_Request", bound=BaseModel)
_Result = TypeVar("_Result", bound=BaseModel)


class SpatialWorkflow:
    def __init__(
        self,
        native: SpatialNativeBinding,
        source_reader: CalibrationSourceReader,
    ) -> None:
        self._native = native
        self._source_reader = source_reader

    async def invoke_native(
        self,
        operation: str,
        request: _Request,
        *,
        native: SpatialNativeBinding | None = None,
    ) -> _Result:
        """Invoke one frozen native operation and validate its scoped result."""
        pair = spatial.SPATIAL_OPERATIONS.get(operation)
        if pair is None:
            raise ValueError(f"Unknown Assets spatial operation: {operation}")
        request_type, result_type = pair
        if not isinstance(request, request_type):
            raise TypeError(f"Invalid request model for {operation}")
        binding = native or self._native
        method = getattr(binding, operation, None)
        if method is None:
            raise TypeError(f"Native binding does not implement {operation}")
        result = await method(request)
        if isinstance(result, result_type):
            decoded = result
        else:
            try:
                decoded = result_type.model_validate(result)
            except (TypeError, ValueError, ValidationError) as exc:
                raise ValueError(
                    f"Native binding returned an invalid {operation} result"
                ) from exc
        spatial.require_spatial_scope(request, decoded)
        return decoded  # type: ignore[return-value]

    async def list_spatial_scenes(
        self,
        request: spatial.ListScenesRequest,
        *,
        native: SpatialNativeBinding | None = None,
    ) -> tuple[spatial.SpatialScene, ...]:
        expected_scope = (request.actor_id, request.dataset_id)

        async def fetch(cursor: spatial.SceneCursor | None) -> spatial.ScenePage:
            return await self.invoke_native(
                "list_spatial_scenes",
                request.model_copy(update={"after": cursor}),
                native=native,
            )

        rows = await collect_spatial_pages(
            fetch,
            decode_page=lambda page: SpatialPage(
                schema=spatial.SPATIAL_SCHEMA,
                action="list_spatial_scenes",
                scope=(page.actor_id, page.dataset_id),
                snapshot=page.snapshot_digest,
                cursor=page.after,
                rows=tuple(page.scenes),
                next_cursor=page.next_cursor,
                complete=page.complete,
                total=page.total,
            ),
            expected_schema=spatial.SPATIAL_SCHEMA,
            expected_action="list_spatial_scenes",
            expected_scope=expected_scope,
            row_identity=lambda scene: scene.id,
            cursor_for_row=lambda scene: spatial.SceneCursor(
                created_at=scene.created_at, scene_id=scene.id
            ),
            cursor_order_key=lambda cursor: (cursor.created_at, str(cursor.scene_id)),
        )
        return tuple(rows)

    async def read_spatial_layout(
        self,
        request: spatial.ReadLayoutRequest,
        *,
        native: SpatialNativeBinding | None = None,
    ) -> SpatialLayoutFacts:
        return await self._read_layout(request, native=native)

    async def read_spatial_items(
        self,
        request: spatial.ReadItemsRequest,
        *,
        native: SpatialNativeBinding | None = None,
    ) -> SpatialItemFacts:
        selector = (
            ("dataset", request.dataset_id)
            if request.dataset_id is not None
            else ("items", tuple(sorted(request.item_ids or (), key=str)))
        )
        expected_scope = selector
        snapshot_digest: str | None = None

        async def fetch(cursor: UUID | None) -> spatial.ItemFactsPage:
            return await self.invoke_native(
                "read_spatial_items",
                request.model_copy(update={"after_item_id": cursor}),
                native=native,
            )

        scene_by_id: dict[UUID, spatial.SpatialScene] = {}

        async def checked_fetch(cursor: UUID | None) -> spatial.ItemFactsPage:
            nonlocal snapshot_digest
            page = await fetch(cursor)
            if snapshot_digest is None:
                snapshot_digest = page.snapshot_digest
            for scene in page.scenes:
                previous = scene_by_id.get(scene.id)
                if previous is not None and previous != scene:
                    raise SpatialPageError("scene facts changed across item pages")
                scene_by_id[scene.id] = scene
            return page

        rows = await collect_spatial_pages(
            checked_fetch,
            decode_page=lambda page: SpatialPage(
                schema=spatial.SPATIAL_SCHEMA,
                action="read_spatial_items",
                scope=(
                    ("dataset", page.dataset_id)
                    if page.dataset_id is not None
                    else ("items", tuple(sorted(page.item_ids or (), key=str)))
                ),
                snapshot=page.snapshot_digest,
                cursor=page.after_item_id,
                rows=tuple(page.items),
                next_cursor=page.next_item_id,
                complete=page.complete,
                total=page.total,
            ),
            expected_schema=spatial.SPATIAL_SCHEMA,
            expected_action="read_spatial_items",
            expected_scope=expected_scope,
            row_identity=lambda item: item.id,
            cursor_for_row=lambda item: item.id,
            cursor_order_key=str,
        )
        return SpatialItemFacts(
            dataset_id=request.dataset_id,
            item_ids=tuple(request.item_ids) if request.item_ids is not None else None,
            snapshot_digest=_require_snapshot(snapshot_digest),
            items=tuple(rows),
            scenes=tuple(scene_by_id.values()),
        )

    async def read_spatial_poses(
        self,
        request: spatial.ReadPosesRequest,
        *,
        native: SpatialNativeBinding | None = None,
    ) -> tuple[spatial.WireFramePose, ...]:
        expected_scope = (request.actor_id, request.scene_id)

        async def fetch(cursor: int | None) -> spatial.PosePage:
            return await self.invoke_native(
                "read_spatial_poses",
                request.model_copy(update={"after_frame_index": cursor}),
                native=native,
            )

        rows = await collect_spatial_pages(
            fetch,
            decode_page=lambda page: SpatialPage(
                schema=spatial.SPATIAL_SCHEMA,
                action="read_spatial_poses",
                scope=(page.actor_id, page.scene_id),
                snapshot=page.snapshot_digest,
                cursor=page.after_frame_index,
                rows=tuple(page.poses),
                next_cursor=page.next_frame_index,
                complete=page.complete,
                total=page.total,
            ),
            expected_schema=spatial.SPATIAL_SCHEMA,
            expected_action="read_spatial_poses",
            expected_scope=expected_scope,
            row_identity=lambda pose: pose.frame_index,
            cursor_for_row=lambda pose: pose.frame_index,
            cursor_order_key=lambda value: value,
        )
        return tuple(rows)

    async def read_spatial_calibrations(
        self,
        request: spatial.ReadCalibrationsRequest,
        *,
        native: SpatialNativeBinding | None = None,
    ) -> tuple[spatial.CalibrationState, ...]:
        expected_scope = (
            tuple(sorted(request.dataset_item_ids, key=str)),
            request.include_history,
        )

        async def fetch(
            cursor: spatial.CalibrationCursor | None,
        ) -> spatial.CalibrationPage:
            return await self.invoke_native(
                "read_spatial_calibrations",
                request.model_copy(update={"after": cursor}),
                native=native,
            )

        rows = await collect_spatial_pages(
            fetch,
            decode_page=lambda page: SpatialPage(
                schema=spatial.SPATIAL_SCHEMA,
                action="read_spatial_calibrations",
                scope=(
                    tuple(sorted(page.dataset_item_ids, key=str)),
                    page.include_history,
                ),
                snapshot=page.snapshot_digest,
                cursor=page.after,
                rows=tuple(page.records),
                next_cursor=page.next_cursor,
                complete=page.complete,
                total=page.total,
            ),
            expected_schema=spatial.SPATIAL_SCHEMA,
            expected_action="read_spatial_calibrations",
            expected_scope=expected_scope,
            row_identity=lambda record: (record.dataset_item_id, record.revision),
            cursor_for_row=lambda record: spatial.CalibrationCursor(
                dataset_item_id=record.dataset_item_id, revision=record.revision
            ),
            cursor_order_key=lambda cursor: (
                str(cursor.dataset_item_id),
                -cursor.revision,
            ),
        )
        return tuple(spatial.normalize_calibration_record(record) for record in rows)

    async def update_calibration(
        self,
        request: spatial.UpdateCalibrationRequest,
        *,
        native: SpatialNativeBinding | None = None,
    ) -> spatial.CalibrationMutationResult:
        """Resolve target and source facts, then perform one native CAS mutation.

        The final native operation owns receipt lookup before revision/snapshot
        validation. This workflow therefore does not reject a stale read before
        giving the native capability the stable operation intent.
        """
        target = await self.invoke_native(
            "read_spatial_camera_target",
            spatial.ReadCameraTargetRequest(
                actor_id=request.actor_id,
                task_id=request.task_id,
                camera_role=request.camera_role,
            ),
            native=native,
        )
        item_facts = await self.read_spatial_items(
            spatial.ReadItemsRequest(item_ids=[target.dataset_item_id]),
            native=native,
        )
        item = next(
            (row for row in item_facts.items if row.id == target.dataset_item_id),
            None,
        )
        if item is None:
            raise spatial.SpatialFailure("camera_dataset_item_missing")
        current = await self.invoke_native(
            "lock_spatial_calibration_facts",
            spatial.LockCalibrationFactsRequest(
                actor_id=request.actor_id,
                task_id=request.task_id,
                camera_role=request.camera_role,
                dataset_item_id=target.dataset_item_id,
                expected_source_snapshot=item.source_snapshot,
            ),
            native=native,
        )
        current_document = ""
        if current.calibration is not None:
            try:
                current_calibration = SensorCalibration.model_validate(
                    current.calibration
                )
                current_document = spatial.canonical_calibration_document(
                    current_calibration
                )
            except (TypeError, ValueError, ValidationError):
                # The native mutation will report invalid current facts unless
                # this stable operation already has a committed receipt.
                current_document = ""
        apply_request = spatial.ApplyCalibrationRequest(
            **request.model_dump(),
            canonical_document=spatial.canonical_calibration_document(
                request.calibration
            ),
            current_canonical_document=current_document,
            source_snapshot=current.source_snapshot,
        )
        return await self.invoke_native(
            "update_spatial_calibration", apply_request, native=native
        )

    async def record_calibration_provenance(
        self,
        request: spatial.RecordCalibrationProvenanceRequest,
        *,
        native: SpatialNativeBinding | None = None,
    ) -> spatial.ProvenanceMutationResult:
        return await self.invoke_native(
            "record_spatial_calibration_provenance", request, native=native
        )

    async def backfill_scenes(
        self,
        request: spatial.BackfillScenesRequest,
        *,
        native: SpatialNativeBinding | None = None,
    ) -> spatial.LayoutMutationResult:
        if request.actor_id is not None:
            await self.invoke_native(
                "guard_spatial_actor",
                spatial.GuardSpatialActorRequest(
                    actor_id=request.actor_id, access="manager_write"
                ),
                native=native,
            )
        layout = await self._read_layout(
            spatial.ReadLayoutRequest(
                actor_id=request.actor_id,
                dataset_id=request.dataset_id,
            ),
            native=native,
        )
        planner_items = tuple(_planner_item(item) for item in layout.items)
        try:
            planned = plan_scenes(
                SceneLayoutInput(
                    dataset_name=layout.dataset_name,
                    items=planner_items,
                    scene_count=layout.scene_count,
                    assigned_count=layout.assigned_count,
                    item_count=len(layout.items),
                    mode=request.mode,
                    dry_run=request.dry_run,
                )
            )
        except ValueError as exc:
            raise spatial.SpatialFailure("invalid_request") from exc

        plan = _to_spatial_layout_plan(
            planned,
            dataset_id=request.dataset_id,
            operation_id=request.operation_id,
        )
        return await self.invoke_native(
            "apply_spatial_layout",
            spatial.ApplyLayoutRequest(
                operation_id=request.operation_id,
                actor_id=request.actor_id,
                dataset_id=request.dataset_id,
                mode=request.mode,
                dry_run=request.dry_run,
                plan=plan,
                snapshot={"snapshot_digest": layout.snapshot_digest},
            ),
            native=native,
        )

    async def sniff_axis(
        self,
        request: spatial.SniffAxisRequest,
        *,
        native: SpatialNativeBinding | None = None,
    ) -> spatial.AxisSniffResult:
        await self.invoke_native(
            "guard_spatial_actor",
            spatial.GuardSpatialActorRequest(
                actor_id=request.actor_id, access="manager_read"
            ),
            native=native,
        )
        facts = await self.read_spatial_items(
            spatial.ReadItemsRequest(dataset_id=request.dataset_id),
            native=native,
        )
        observations = _axis_observations(facts.items)
        inferred = sniff_axis_convention(observations)
        if inferred is None:
            result = None
        else:
            result = spatial.AxisInferenceResult(
                best=inferred.best,
                score=inferred.score,
                candidates=[
                    spatial.AxisCandidate(
                        convention=candidate.convention,
                        score=candidate.score,
                    )
                    for candidate in inferred.candidates
                ],
                source=inferred.source,
                camera_role=inferred.camera_role,
                camera_item_id=inferred.camera_item_id,
                per_camera=[
                    spatial.AxisCameraResult(
                        camera_role=camera.camera_role,
                        best=camera.best,
                        score=camera.score,
                    )
                    for camera in inferred.per_camera
                ],
                agreement=inferred.agreement,
            )
        return spatial.AxisSniffResult(
            actor_id=request.actor_id,
            dataset_id=request.dataset_id,
            result=result,
        )

    async def capture_pointcloud_preparation(
        self,
        request: spatial.PreparePointcloudRequest,
        *,
        native: SpatialNativeBinding | None = None,
    ) -> PointcloudPreparationCapture:
        layout = await self._read_preparation_layout(
            request.creation_operation_id,
            native=native,
        )
        plan = _plan_preparation_layout(layout, request.creation_operation_id)
        item_ids_by_scene = {item.id: item.scene_id for item in layout.items}
        item_ids_by_scene.update(
            {assignment.item_id: assignment.scene_id for assignment in plan.assignments}
        )
        grouped_items = tuple(
            PreparedDatasetItem(
                id=item.id,
                file_name=item.file_name,
                file_path=item.file_path,
                file_type=item.file_type,
                scene_id=item_ids_by_scene[item.id],
                frame_index=item.frame_index,
                logical_path=source_ingest_logical_path(item.metadata),
            )
            for item in layout.items
        )
        grouped_frames, calibration_by_role = group_frames(grouped_items)
        camera_items_by_role: dict[str, list[spatial.SpatialItem]] = {}
        item_by_id = {item.id: item for item in layout.items}
        for frame in grouped_frames.values():
            for role, camera_item in frame.cameras:
                camera_items_by_role.setdefault(role, []).append(
                    item_by_id[camera_item.id]
                )
        return PointcloudPreparationCapture(
            layout=layout,
            frame_plan=plan,
            calibration_items=tuple(
                (role, item_by_id[item.id])
                for role, item in calibration_by_role.items()
            ),
            camera_items_by_role={
                role: tuple(items) for role, items in camera_items_by_role.items()
            },
        )

    async def read_pointcloud_calibration_sources(
        self,
        capture: PointcloudPreparationCapture,
    ) -> spatial.ApplyPreparationRequest:
        """Read accepted calibration source objects outside native SQL calls."""
        item_by_id = {item.id: item for item in capture.layout.items}
        updates: list[spatial.CalibrationImportUpdate] = []
        known_fields = set(SensorCalibration.model_fields)
        for role, source_item in capture.calibration_items:
            cameras = capture.camera_items_by_role.get(role, ())
            if not cameras:
                continue
            try:
                facts, body = await asyncio.to_thread(
                    self._source_reader.read_calibration_source,
                    source_item.file_path,
                    expected_size=source_item.file_size,
                )
                raw = json.loads(body)
                if not isinstance(raw, dict):
                    continue
                parsed = SensorCalibration.model_validate(
                    {key: value for key, value in raw.items() if key in known_fields}
                )
                calibration = spatial.WireCalibration.model_validate(
                    parsed.model_dump(mode="json")
                )
            except (
                SourceReadError,
                UnicodeDecodeError,
                json.JSONDecodeError,
                TypeError,
                ValueError,
            ) as exc:
                logger.warning(
                    "skipping point-cloud calibration source item %s (%s)",
                    source_item.id,
                    type(exc).__name__,
                )
                continue
            except Exception as exc:
                logger.warning(
                    "skipping point-cloud calibration source item %s (%s)",
                    source_item.id,
                    type(exc).__name__,
                )
                continue

            source_snapshot = item_by_id[source_item.id].source_snapshot
            for camera in cameras:
                updates.append(
                    spatial.CalibrationImportUpdate(
                        dataset_item_id=camera.id,
                        source_item_id=source_item.id,
                        source_snapshot=source_snapshot,
                        source_etag=facts.etag,
                        source_size=facts.size,
                        calibration=calibration,
                    )
                )
        return spatial.ApplyPreparationRequest(
            creation_operation_id=_require_creation_operation_id(capture),
            snapshot={"snapshot_digest": capture.layout.snapshot_digest},
            calibration_updates=updates,
            frame_plan=capture.frame_plan,
        )

    async def apply_pointcloud_preparation_request(
        self,
        request: spatial.ApplyPreparationRequest,
        *,
        native: SpatialNativeBinding | None = None,
    ) -> spatial.PreparationMutationResult:
        return await self.invoke_native(
            "apply_pointcloud_preparation", request, native=native
        )

    async def prepare_pointcloud(
        self,
        request: spatial.PreparePointcloudRequest,
        *,
        native: SpatialNativeBinding | None = None,
    ) -> spatial.PreparationMutationResult:
        capture = await self.capture_pointcloud_preparation(request, native=native)
        apply_request = await self.read_pointcloud_calibration_sources(capture)
        return await self.apply_pointcloud_preparation_request(
            apply_request, native=native
        )

    async def _read_layout(
        self,
        request: spatial.ReadLayoutRequest,
        *,
        native: SpatialNativeBinding | None,
    ) -> SpatialLayoutFacts:
        expected_scope = (
            request.actor_id,
            request.dataset_id,
            request.creation_operation_id,
        )
        header: tuple[Any, ...] | None = None
        snapshot_digest: str | None = None
        scenes: dict[UUID, spatial.SpatialScene] = {}

        async def fetch(cursor: UUID | None) -> spatial.LayoutPage:
            nonlocal header, snapshot_digest
            page = await self.invoke_native(
                "read_spatial_layout",
                request.model_copy(update={"after_item_id": cursor}),
                native=native,
            )
            if snapshot_digest is None:
                snapshot_digest = page.snapshot_digest
            actual_header = (
                page.dataset_name,
                page.is_temporal,
                page.axis_convention,
                page.scene_count,
                page.assigned_count,
            )
            if header is None:
                header = actual_header
            elif actual_header != header:
                raise SpatialPageError("layout facts changed across pages")
            for scene in page.scenes:
                previous = scenes.get(scene.id)
                if previous is not None and previous != scene:
                    raise SpatialPageError("scene facts changed across layout pages")
                scenes[scene.id] = scene
            return page

        rows = await collect_spatial_pages(
            fetch,
            decode_page=lambda page: SpatialPage(
                schema=spatial.SPATIAL_SCHEMA,
                action="read_spatial_layout",
                scope=(page.actor_id, page.dataset_id, page.creation_operation_id),
                snapshot=page.snapshot_digest,
                cursor=page.after_item_id,
                rows=tuple(page.items),
                next_cursor=page.next_item_id,
                complete=page.complete,
                total=page.total,
            ),
            expected_schema=spatial.SPATIAL_SCHEMA,
            expected_action="read_spatial_layout",
            expected_scope=expected_scope,
            row_identity=lambda item: item.id,
            cursor_for_row=lambda item: item.id,
            cursor_order_key=str,
        )
        missing_scenes = {
            item.scene_id
            for item in rows
            if item.scene_id is not None and item.scene_id not in scenes
        }
        if missing_scenes:
            raise SpatialPageError("layout references an omitted Scene")
        assert header is not None
        dataset_name, is_temporal, axis_convention, scene_count, assigned_count = header
        return SpatialLayoutFacts(
            actor_id=request.actor_id,
            dataset_id=request.dataset_id,
            creation_operation_id=request.creation_operation_id,
            dataset_name=dataset_name,
            is_temporal=is_temporal,
            axis_convention=axis_convention,
            scene_count=scene_count,
            assigned_count=assigned_count,
            snapshot_digest=_require_snapshot(snapshot_digest),
            items=tuple(rows),
            scenes=tuple(scenes.values()),
        )

    async def _read_preparation_layout(
        self,
        creation_operation_id: UUID,
        *,
        native: SpatialNativeBinding | None,
    ) -> SpatialLayoutFacts:
        expected_scope = (creation_operation_id,)
        header: tuple[Any, ...] | None = None
        dataset_id: UUID | None = None
        snapshot_digest: str | None = None
        scenes: dict[UUID, spatial.SpatialScene] = {}

        async def fetch(cursor: UUID | None) -> spatial.PreparationPage:
            nonlocal header, dataset_id, snapshot_digest
            page = await self.invoke_native(
                "read_pointcloud_preparation",
                spatial.ReadPreparationRequest(
                    creation_operation_id=creation_operation_id,
                    after_item_id=cursor,
                ),
                native=native,
            )
            if snapshot_digest is None:
                snapshot_digest = page.snapshot_digest
            if dataset_id is None:
                dataset_id = page.dataset_id
            elif page.dataset_id != dataset_id:
                raise SpatialPageError("preparation dataset changed across pages")
            actual_header = (
                page.dataset_name,
                page.is_temporal,
                page.axis_convention,
                page.scene_count,
                page.assigned_count,
            )
            if header is None:
                header = actual_header
            elif actual_header != header:
                raise SpatialPageError("preparation facts changed across pages")
            for scene in page.scenes:
                previous = scenes.get(scene.id)
                if previous is not None and previous != scene:
                    raise SpatialPageError(
                        "scene facts changed across preparation pages"
                    )
                scenes[scene.id] = scene
            return page

        rows = await collect_spatial_pages(
            fetch,
            decode_page=lambda page: SpatialPage(
                schema=spatial.SPATIAL_SCHEMA,
                action="read_pointcloud_preparation",
                scope=(page.creation_operation_id,),
                snapshot=page.snapshot_digest,
                cursor=page.after_item_id,
                rows=tuple(page.items),
                next_cursor=page.next_item_id,
                complete=page.complete,
                total=page.total,
            ),
            expected_schema=spatial.SPATIAL_SCHEMA,
            expected_action="read_pointcloud_preparation",
            expected_scope=expected_scope,
            row_identity=lambda item: item.id,
            cursor_for_row=lambda item: item.id,
            cursor_order_key=str,
        )
        missing_scenes = {
            item.scene_id
            for item in rows
            if item.scene_id is not None and item.scene_id not in scenes
        }
        if missing_scenes:
            raise SpatialPageError("preparation references an omitted Scene")
        assert dataset_id is not None and header is not None
        dataset_name, is_temporal, axis_convention, scene_count, assigned_count = header
        return SpatialLayoutFacts(
            actor_id=None,
            dataset_id=dataset_id,
            creation_operation_id=creation_operation_id,
            dataset_name=dataset_name,
            is_temporal=is_temporal,
            axis_convention=axis_convention,
            scene_count=scene_count,
            assigned_count=assigned_count,
            snapshot_digest=_require_snapshot(snapshot_digest),
            items=tuple(rows),
            scenes=tuple(scenes.values()),
        )


def _planner_item(item: spatial.SpatialItem) -> PreparedDatasetItem:
    return PreparedDatasetItem(
        id=item.id,
        file_name=item.file_name,
        file_path=item.file_path,
        file_type=item.file_type,
        scene_id=item.scene_id,
        frame_index=item.frame_index,
        logical_path=source_ingest_logical_path(item.metadata),
    )


def _plan_preparation_layout(
    layout: SpatialLayoutFacts, operation_id: UUID
) -> spatial.SpatialLayoutPlan:
    try:
        planned = plan_scenes(
            SceneLayoutInput(
                dataset_name=layout.dataset_name,
                items=tuple(_planner_item(item) for item in layout.items),
                scene_count=layout.scene_count,
                assigned_count=layout.assigned_count,
                item_count=len(layout.items),
                mode="single",
            )
        )
    except ValueError as exc:
        raise spatial.SpatialFailure("invalid_request") from exc
    return _to_spatial_layout_plan(
        planned,
        dataset_id=layout.dataset_id,
        operation_id=operation_id,
    )


def _to_spatial_layout_plan(
    planned: IngestScenePlan, *, dataset_id: UUID, operation_id: UUID
) -> spatial.SpatialLayoutPlan:
    scene_id_map = {
        row.id: uuid5(
            operation_id,
            f"{dataset_id}:{row.source_metadata.get('group_key', row.name)}",
        )
        for row in planned.scenes
    }
    return spatial.SpatialLayoutPlan(
        scenes=[
            spatial.PlannedScene(
                id=scene_id_map[row.id],
                name=row.name,
                source_format=row.source_format,
                source_metadata=row.source_metadata,
            )
            for row in planned.scenes
        ],
        assignments=[
            spatial.SceneAssignment(
                item_id=row.item_id,
                scene_id=scene_id_map[row.scene_id],
                frame_index=row.frame_index,
            )
            for row in planned.assignments
        ],
        created_scenes=planned.created_scenes,
        assigned_items=planned.assigned_items,
        skipped_items=planned.skipped_items,
        dry_run=planned.dry_run,
        notes=list(planned.notes),
    )


def _axis_observations(
    items: tuple[spatial.SpatialItem, ...],
) -> tuple[AxisObservation, ...]:
    linked: list[AxisObservation] = []
    for item in items:
        calibration = _metadata_calibration(item)
        for role in item.camera_roles:
            if role.startswith("camera_"):
                linked.append(
                    AxisObservation(
                        item_id=item.id,
                        source="task_link",
                        camera_role=role,
                        file_name=item.file_name or "",
                        file_path=item.file_path or "",
                        created_at=item.created_at,
                        calibration=calibration,
                    )
                )
    if linked:
        return tuple(linked)

    fallback: list[AxisObservation] = []
    for item in items:
        if item.file_type != "image":
            continue
        calibration = _metadata_calibration(item)
        if calibration is None:
            continue
        fallback.append(
            AxisObservation(
                item_id=item.id,
                source="dataset_item",
                camera_role=_camera_role_from_path(item),
                file_name=item.file_name or "",
                file_path=item.file_path or "",
                created_at=item.created_at,
                calibration=calibration,
            )
        )
    return tuple(fallback)


def _metadata_calibration(item: spatial.SpatialItem) -> SensorCalibration | None:
    raw = item.metadata.get("calibration") if isinstance(item.metadata, dict) else None
    if raw is None:
        return None
    try:
        return SensorCalibration.model_validate(raw)
    except (TypeError, ValueError, ValidationError):
        return None


def _camera_role_from_path(item: spatial.SpatialItem) -> str | None:
    parts = PurePosixPath(item.file_path or item.file_name).parts
    for index in range(len(parts) - 1, -1, -1):
        if parts[index] == "camera" and index + 1 < len(parts):
            return f"camera_{parts[index + 1]}"
    return None


def _require_creation_operation_id(
    capture: PointcloudPreparationCapture,
) -> UUID:
    operation_id = capture.layout.creation_operation_id
    if operation_id is None:
        raise ValueError("Preparation capture lost its accepted operation ID")
    return operation_id


def _require_snapshot(value: str | None) -> str:
    if value is None:
        raise SpatialPageError("spatial snapshot was not captured")
    return value
