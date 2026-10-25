from __future__ import annotations

from dataclasses import dataclass
import uuid

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.orm.util import identity_key

from app.db.models.annotation import Annotation
from app.db.models.task import Task
from app.schemas.multicamera_annotation import (
    CameraAnnotationMemberOut,
    CameraProjectionResidual,
    NormalizedCameraBbox,
)
from aap_assets_catalog.spatial_contract import (
    LockCalibrationFactsRequest,
    ReadItemsRequest,
    SpatialItem,
    SpatialFailure,
    CalibrationState,
    normalize_calibration_record,
)
from app.services.assets_spatial import AssetsSpatialGateway
from app.services.annotation_evidence import record_annotation_actor
from app.services.pointcloud_projection import (
    ProjectionCamera,
    project_iso_box,
    projection_residual,
)
from app.services.labeling_scene import resolve_task_scene_frames
from aap_labeling.annotation_transfer_contract import (
    ANNOTATION_TRANSFER_PROTOCOL,
    CameraTrackSnapshot,
    LockCameraMemberScope,
    MutateCameraMember,
    ReadCameraGraph,
    ReadCameraLinks,
)
from app.services.labeling_annotation_transfer import (
    AnnotationTransferRejected,
    annotation_transfer_command,
)


class MulticameraAnnotationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CameraContext:
    item: SpatialItem
    role: str
    calibration: CalibrationState
    axis_convention: str

    @property
    def projection_camera(self) -> ProjectionCamera:
        if not self.item.width or not self.item.height:
            raise MulticameraAnnotationError(
                "camera_dimensions_missing", "camera image dimensions are required"
            )

        @dataclass(frozen=True)
        class _Camera:
            calibration: object
            width: int
            height: int

        return _Camera(  # type: ignore[return-value]
            self.calibration.calibration,
            int(self.item.width),
            int(self.item.height),
        )


def _annotation(snapshot) -> Annotation:
    return Annotation(**snapshot.model_dump(mode="python"))


def _sync_member_cache(
    db: AsyncSession, snapshot, changed: tuple[str, ...]
) -> Annotation:
    member = db.sync_session.identity_map.get(
        identity_key(Annotation, ident=snapshot.id)
    )
    if isinstance(member, Annotation):
        values = snapshot.model_dump(mode="python")
        for field in changed:
            set_committed_value(member, field, values[field])
        return member
    return _annotation(snapshot)


async def _camera_graph(
    db: AsyncSession,
    *,
    task: Task,
    scene_track_id: uuid.UUID | None,
    include_inactive: bool,
):
    try:
        data = await annotation_transfer_command(
            db,
            ReadCameraGraph(
                schema=ANNOTATION_TRANSFER_PROTOCOL,
                operation="read_camera_graph",
                task_id=task.id,
                project_id=task.project_id,
                scene_track_id=scene_track_id,
                include_inactive=include_inactive,
            ),
        )
    except AnnotationTransferRejected as exc:
        raise MulticameraAnnotationError(exc.reason, str(exc)) from exc
    assert data.camera_graph is not None
    return data.camera_graph


async def _camera_command(db: AsyncSession, request: MutateCameraMember):
    try:
        data = await annotation_transfer_command(db, request)
    except AnnotationTransferRejected as exc:
        raise MulticameraAnnotationError(exc.reason, str(exc)) from exc
    assert data.camera_mutation is not None
    return data.camera_mutation


async def _lock_camera_scope(db: AsyncSession, request: LockCameraMemberScope):
    try:
        data = await annotation_transfer_command(db, request)
    except AnnotationTransferRejected as exc:
        raise MulticameraAnnotationError(exc.reason, str(exc)) from exc
    assert data.camera_lock_scope is not None
    return data.camera_lock_scope


def _camera_track(snapshot: CameraTrackSnapshot):
    @dataclass(frozen=True)
    class _Track:
        id: uuid.UUID
        project_id: uuid.UUID
        scene_id: uuid.UUID
        track_id: str
        class_name: str
        revision: int
        retired_at: object | None

    return _Track(**snapshot.model_dump(mode="python"))


def _normalized_bbox(geometry: dict) -> tuple[float, float, float, float]:
    return (
        float(geometry["x"]),
        float(geometry["y"]),
        float(geometry["w"]),
        float(geometry["h"]),
    )


async def load_camera_context(
    db: AsyncSession, *, task: Task, camera_role: str
) -> CameraContext:
    contexts = await load_camera_contexts(db, task=task, camera_roles={camera_role})
    context = contexts.get(camera_role)
    if context is None:
        raise MulticameraAnnotationError(
            "camera_role_not_linked", "camera role is not linked to this task"
        )
    return context


async def load_linked_camera_item(
    db: AsyncSession, *, task: Task, camera_role: str
) -> SpatialItem:
    if not camera_role.startswith("camera_"):
        raise MulticameraAnnotationError(
            "camera_role_not_linked", "camera role is not linked to this task"
        )
    try:
        data = await annotation_transfer_command(
            db,
            ReadCameraLinks(
                schema=ANNOTATION_TRANSFER_PROTOCOL,
                operation="read_camera_links",
                task_id=task.id,
                roles=[camera_role],
            ),
        )
    except AnnotationTransferRejected as exc:
        raise MulticameraAnnotationError(exc.reason, str(exc)) from exc
    if not data.camera_links:
        raise MulticameraAnnotationError(
            "camera_role_not_linked", "camera role is not linked to this task"
        )
    item_id = data.camera_links[0].dataset_item_id
    page = await AssetsSpatialGateway(db).read_items(
        ReadItemsRequest(item_ids=[item_id])
    )
    item = next((item for item in page.items if item.id == item_id), None)
    if item is None:
        raise MulticameraAnnotationError(
            "camera_role_not_linked", "camera role is not linked to this task"
        )
    return item


async def load_camera_contexts(
    db: AsyncSession, *, task: Task, camera_roles: set[str]
) -> dict[str, CameraContext]:
    if not camera_roles or any(not role.startswith("camera_") for role in camera_roles):
        return {}
    roles = sorted(camera_roles | {"primary_lidar"})
    try:
        link_data = await annotation_transfer_command(
            db,
            ReadCameraLinks(
                schema=ANNOTATION_TRANSFER_PROTOCOL,
                operation="read_camera_links",
                task_id=task.id,
                roles=roles,
            ),
        )
    except AnnotationTransferRejected as exc:
        raise MulticameraAnnotationError(exc.reason, str(exc)) from exc
    links_by_role = {link.role: link for link in link_data.camera_links}
    selected_links = [
        links_by_role[role] for role in camera_roles if role in links_by_role
    ]
    item_ids = {link.dataset_item_id for link in selected_links}
    primary_link = links_by_role.get("primary_lidar")
    if primary_link is not None:
        item_ids.add(primary_link.dataset_item_id)
    item_facts = (
        await AssetsSpatialGateway(db).read_items(
            ReadItemsRequest(item_ids=list(item_ids))
        )
        if item_ids
        else None
    )
    items_by_id = {item.id: item for item in item_facts.items} if item_facts else {}
    axis_convention = "iso_8855"
    if primary_link is not None:
        primary = items_by_id.get(primary_link.dataset_item_id)
        if primary is not None:
            axis_convention = primary.axis_convention or "iso_8855"
    camera_items = [
        item
        for link in selected_links
        if (item := items_by_id.get(link.dataset_item_id)) is not None
    ]
    records = await AssetsSpatialGateway(db).read_calibrations(
        [item.id for item in camera_items]
    )
    states: dict[uuid.UUID, CalibrationState] = {}
    for record in records:
        try:
            states[record.dataset_item_id] = normalize_calibration_record(record)
        except SpatialFailure as exc:
            raise MulticameraAnnotationError(exc.code, exc.code) from exc
    contexts = {}
    for link in selected_links:
        item = items_by_id.get(link.dataset_item_id)
        state = states.get(link.dataset_item_id)
        if item is None or state is None:
            continue
        if not item.width or not item.height:
            raise MulticameraAnnotationError(
                "camera_dimensions_missing", "camera image dimensions are required"
            )
        contexts[link.role] = CameraContext(
            item=item,
            role=link.role,
            calibration=state,
            axis_convention=axis_convention,
        )
    return contexts


async def _lock_camera_calibration(
    db: AsyncSession,
    *,
    task: Task,
    actor_id: uuid.UUID | None,
    context: CameraContext,
) -> CameraContext:
    record = await AssetsSpatialGateway(db).lock_calibration_facts(
        LockCalibrationFactsRequest(
            actor_id=actor_id,
            task_id=task.id if actor_id is not None else None,
            camera_role=context.role if actor_id is not None else None,
            dataset_item_id=context.item.id,
            expected_source_snapshot=context.calibration.source_snapshot or {},
        )
    )
    try:
        state = normalize_calibration_record(record)
    except (SpatialFailure, ValueError, TypeError) as exc:
        code = getattr(exc, "code", "camera_calibration_invalid")
        raise MulticameraAnnotationError(code, code) from exc
    if (
        state.revision != context.calibration.revision
        or state.digest != context.calibration.digest
    ):
        raise MulticameraAnnotationError(
            "calibration_revision_conflict", "camera calibration changed"
        )
    return CameraContext(
        item=context.item,
        role=context.role,
        calibration=state,
        axis_convention=context.axis_convention,
    )


def _assert_calibration(
    state: CalibrationState, expected_revision: int, expected_digest: str
) -> None:
    if state.revision != expected_revision or state.digest != expected_digest.lower():
        raise MulticameraAnnotationError(
            "calibration_revision_conflict", "camera calibration changed"
        )


async def create_camera_member(
    db: AsyncSession,
    *,
    task: Task,
    actor_id: uuid.UUID,
    source_annotation_id: uuid.UUID,
    camera_role: str,
    bbox: NormalizedCameraBbox,
    visibility: str,
    expected_track_revision: int,
    expected_calibration_revision: int,
    expected_calibration_digest: str,
) -> Annotation:
    # A2 · acquire Task evidence before the Annotation and SceneTrack rows.
    await record_annotation_actor(db, task, actor_id)
    await _lock_camera_scope(
        db,
        LockCameraMemberScope(
            schema=ANNOTATION_TRANSFER_PROTOCOL,
            operation="lock_camera_member_scope",
            action="create",
            task_id=task.id,
            project_id=task.project_id,
            actor_id=actor_id,
            member_id=None,
            source_annotation_id=source_annotation_id,
            expected_version=None,
            expected_track_revision=expected_track_revision,
        ),
    )
    context = await load_camera_context(db, task=task, camera_role=camera_role)
    context = await _lock_camera_calibration(
        db, task=task, actor_id=actor_id, context=context
    )
    _assert_calibration(
        context.calibration,
        expected_calibration_revision,
        expected_calibration_digest,
    )
    result = await _camera_command(
        db,
        MutateCameraMember(
            schema=ANNOTATION_TRANSFER_PROTOCOL,
            operation="mutate_camera_member",
            action="create",
            task_id=task.id,
            project_id=task.project_id,
            actor_id=actor_id,
            member_id=uuid.uuid4(),
            source_annotation_id=source_annotation_id,
            camera_role=camera_role,
            camera_item_id=context.item.id,
            bbox=bbox.model_dump(mode="json"),
            visibility=visibility,
            expected_version=None,
            expected_track_revision=expected_track_revision,
            expected_calibration_revision=context.calibration.revision,
            expected_calibration_digest=context.calibration.digest,
        ),
    )
    return _annotation(result.member)


async def update_camera_member(
    db: AsyncSession,
    *,
    task: Task,
    actor_id: uuid.UUID | None = None,
    member_id: uuid.UUID,
    bbox: NormalizedCameraBbox | None,
    visibility: str | None,
    expected_version: int,
    expected_track_revision: int,
    expected_calibration_revision: int,
    expected_calibration_digest: str,
) -> Annotation:
    if actor_id is None:
        raise MulticameraAnnotationError(
            "actor_required", "an active actor is required"
        )
    await record_annotation_actor(db, task, actor_id)
    scope = await _lock_camera_scope(
        db,
        LockCameraMemberScope(
            schema=ANNOTATION_TRANSFER_PROTOCOL,
            operation="lock_camera_member_scope",
            action="update",
            task_id=task.id,
            project_id=task.project_id,
            actor_id=actor_id,
            member_id=member_id,
            source_annotation_id=None,
            expected_version=expected_version,
            expected_track_revision=expected_track_revision,
        ),
    )
    member_snapshot = scope.member
    assert member_snapshot is not None
    context = await load_camera_context(
        db, task=task, camera_role=member_snapshot.sensor_role
    )
    context = await _lock_camera_calibration(
        db, task=task, actor_id=actor_id, context=context
    )
    _assert_calibration(
        context.calibration,
        expected_calibration_revision,
        expected_calibration_digest,
    )
    result = await _camera_command(
        db,
        MutateCameraMember(
            schema=ANNOTATION_TRANSFER_PROTOCOL,
            operation="mutate_camera_member",
            action="update",
            task_id=task.id,
            project_id=task.project_id,
            actor_id=actor_id,
            member_id=member_id,
            source_annotation_id=None,
            camera_role=member_snapshot.sensor_role,
            camera_item_id=context.item.id,
            bbox=bbox.model_dump(mode="json") if bbox is not None else None,
            visibility=visibility,
            expected_version=expected_version,
            expected_track_revision=expected_track_revision,
            expected_calibration_revision=context.calibration.revision,
            expected_calibration_digest=context.calibration.digest,
        ),
    )
    return _sync_member_cache(
        db,
        result.member,
        (
            "geometry",
            "sensor_visibility",
            "calibration_revision",
            "calibration_digest",
            "version",
            "updated_at",
        ),
    )


async def delete_camera_member(
    db: AsyncSession,
    *,
    task: Task,
    actor_id: uuid.UUID | None = None,
    member_id: uuid.UUID,
    expected_version: int,
    expected_track_revision: int,
) -> Annotation:
    if actor_id is None:
        raise MulticameraAnnotationError(
            "actor_required", "an active actor is required"
        )
    await record_annotation_actor(db, task, actor_id)
    result = await _camera_command(
        db,
        MutateCameraMember(
            schema=ANNOTATION_TRANSFER_PROTOCOL,
            operation="mutate_camera_member",
            action="delete",
            task_id=task.id,
            project_id=task.project_id,
            actor_id=actor_id,
            member_id=member_id,
            source_annotation_id=None,
            camera_role=None,
            camera_item_id=None,
            bbox=None,
            visibility=None,
            expected_version=expected_version,
            expected_track_revision=expected_track_revision,
            expected_calibration_revision=None,
            expected_calibration_digest=None,
        ),
    )
    return _sync_member_cache(db, result.member, ("is_active", "version", "updated_at"))


async def restore_camera_member(
    db: AsyncSession,
    *,
    task: Task,
    actor_id: uuid.UUID | None = None,
    member_id: uuid.UUID,
    expected_version: int,
    expected_track_revision: int,
    expected_calibration_revision: int,
    expected_calibration_digest: str,
) -> Annotation:
    if actor_id is None:
        raise MulticameraAnnotationError(
            "actor_required", "an active actor is required"
        )
    await record_annotation_actor(db, task, actor_id)
    scope = await _lock_camera_scope(
        db,
        LockCameraMemberScope(
            schema=ANNOTATION_TRANSFER_PROTOCOL,
            operation="lock_camera_member_scope",
            action="restore",
            task_id=task.id,
            project_id=task.project_id,
            actor_id=actor_id,
            member_id=member_id,
            source_annotation_id=None,
            expected_version=expected_version,
            expected_track_revision=expected_track_revision,
        ),
    )
    member_snapshot = scope.member
    assert member_snapshot is not None
    context = await load_camera_context(
        db, task=task, camera_role=member_snapshot.sensor_role
    )
    context = await _lock_camera_calibration(
        db, task=task, actor_id=actor_id, context=context
    )
    _assert_calibration(
        context.calibration,
        expected_calibration_revision,
        expected_calibration_digest,
    )
    result = await _camera_command(
        db,
        MutateCameraMember(
            schema=ANNOTATION_TRANSFER_PROTOCOL,
            operation="mutate_camera_member",
            action="restore",
            task_id=task.id,
            project_id=task.project_id,
            actor_id=actor_id,
            member_id=member_id,
            source_annotation_id=None,
            camera_role=member_snapshot.sensor_role,
            camera_item_id=context.item.id,
            bbox=None,
            visibility=None,
            expected_version=expected_version,
            expected_track_revision=expected_track_revision,
            expected_calibration_revision=context.calibration.revision,
            expected_calibration_digest=context.calibration.digest,
        ),
    )
    return _sync_member_cache(
        db,
        result.member,
        (
            "is_active",
            "calibration_revision",
            "calibration_digest",
            "version",
            "updated_at",
        ),
    )


async def camera_member_out(
    db: AsyncSession,
    *,
    task: Task,
    member: Annotation,
    track=None,
    context: CameraContext | None = None,
    source: Annotation | None = None,
) -> CameraAnnotationMemberOut:
    assert member.scene_track_id is not None
    assert member.track_id is not None
    assert member.sensor_role is not None
    assert member.sensor_dataset_item_id is not None
    if track is None or context is None or source is None:
        graph = await _camera_graph(
            db,
            task=task,
            scene_track_id=member.scene_track_id,
            include_inactive=True,
        )
        if track is None:
            track_row = next(
                (row for row in graph.tracks if row.id == member.scene_track_id), None
            )
            track = _camera_track(track_row) if track_row is not None else None
        if source is None:
            source_row = next(
                (
                    row
                    for row in graph.sources
                    if row.scene_track_id == member.scene_track_id
                ),
                None,
            )
            source = _annotation(source_row) if source_row is not None else None
        if context is None:
            context = (
                await load_camera_contexts(
                    db, task=task, camera_roles={member.sensor_role}
                )
            ).get(member.sensor_role)
    if track is None:
        raise MulticameraAnnotationError(
            "scene_track_missing", "active SceneTrack was not found"
        )
    if context is None:
        raise MulticameraAnnotationError(
            "camera_role_not_linked", "camera role is not linked to this task"
        )
    residual_out = None
    if source is not None:
        geometry = source.geometry or {}
        projected = project_iso_box(
            {
                "center": [float(value) for value in geometry["center"]],
                "size": [float(value) for value in geometry["size"]],
                "rotation": [float(value) for value in geometry["rotation"]],
            },
            camera=context.projection_camera,
            axis_convention=context.axis_convention,
        )
        if projected is not None:
            residual = projection_residual(
                _normalized_bbox(member.geometry),
                projected.pixel_bbox,
                width=int(context.item.width or 0),
                height=int(context.item.height or 0),
            )
            residual_out = CameraProjectionResidual(
                iou=residual.iou,
                max_edge_residual_px=residual.max_edge_residual_px,
                mean_edge_residual_px=residual.mean_edge_residual_px,
                max_edge_residual_ratio=residual.max_edge_residual_ratio,
                projected_bbox=NormalizedCameraBbox(
                    x=projected.normalized_bbox[0],
                    y=projected.normalized_bbox[1],
                    w=projected.normalized_bbox[2],
                    h=projected.normalized_bbox[3],
                ),
            )
    relation_status = (
        "current"
        if member.calibration_digest == context.calibration.digest
        else "stale"
    )
    geometry = member.geometry or {}
    return CameraAnnotationMemberOut(
        id=member.id,
        task_id=member.task_id,
        scene_track_id=member.scene_track_id,
        track_id=member.track_id,
        class_name=member.class_name,
        camera_dataset_item_id=member.sensor_dataset_item_id,
        camera_role=member.sensor_role,
        bbox=NormalizedCameraBbox(
            x=float(geometry["x"]),
            y=float(geometry["y"]),
            w=float(geometry["w"]),
            h=float(geometry["h"]),
        ),
        visibility=member.sensor_visibility or "unknown",
        version=int(member.version or 1),
        is_active=member.is_active,
        calibration_revision=int(member.calibration_revision or 1),
        calibration_digest=member.calibration_digest or "",
        current_calibration_revision=context.calibration.revision,
        current_calibration_digest=context.calibration.digest,
        relation_status=relation_status,
        track_revision=track.revision,
        residual=residual_out,
        created_at=member.created_at,
        updated_at=member.updated_at or member.created_at,
    )


async def list_camera_members(
    db: AsyncSession,
    *,
    task: Task,
    scene_track_id: uuid.UUID | None,
    include_inactive: bool,
    projection_camera_role: str | None = None,
) -> tuple[list[CameraAnnotationMemberOut], int | None, NormalizedCameraBbox | None]:
    graph = await _camera_graph(
        db,
        task=task,
        scene_track_id=scene_track_id,
        include_inactive=include_inactive,
    )
    rows = [_annotation(row) for row in graph.members]
    tracks_by_id = {row.id: _camera_track(row) for row in graph.tracks}
    sources_by_track = {
        row.scene_track_id: _annotation(row)
        for row in graph.sources
        if row.scene_track_id is not None
    }
    track = tracks_by_id.get(scene_track_id) if scene_track_id is not None else None
    if scene_track_id is not None:
        task_scene = (await resolve_task_scene_frames(db, [task.id]))[task.id]
        task_scene_id = task_scene.scene_id
    else:
        task_scene_id = None
    if scene_track_id is not None and (
        track is None
        or track.project_id != task.project_id
        or track.scene_id != task_scene_id
        or track.retired_at is not None
    ):
        raise MulticameraAnnotationError(
            "scene_track_missing", "active SceneTrack was not found"
        )
    camera_roles = {row.sensor_role for row in rows if row.sensor_role is not None}
    if projection_camera_role is not None:
        camera_roles.add(projection_camera_role)
    contexts = await load_camera_contexts(
        db,
        task=task,
        camera_roles=camera_roles,
    )
    items = [
        await camera_member_out(
            db,
            task=task,
            member=row,
            track=tracks_by_id.get(row.scene_track_id),
            context=contexts.get(row.sensor_role or ""),
            source=sources_by_track.get(row.scene_track_id),
        )
        for row in rows
    ]
    projected_bbox = None
    if scene_track_id is not None and projection_camera_role is not None:
        context = contexts.get(projection_camera_role)
        if context is None:
            raise MulticameraAnnotationError(
                "camera_role_not_linked", "camera role is not linked to this task"
            )
        source = sources_by_track.get(scene_track_id)
        if source is not None:
            geometry = source.geometry or {}
            projected = project_iso_box(
                {
                    "center": [float(value) for value in geometry["center"]],
                    "size": [float(value) for value in geometry["size"]],
                    "rotation": [float(value) for value in geometry["rotation"]],
                },
                camera=context.projection_camera,
                axis_convention=context.axis_convention,
            )
            if projected is not None:
                projected_bbox = NormalizedCameraBbox(
                    x=projected.normalized_bbox[0],
                    y=projected.normalized_bbox[1],
                    w=projected.normalized_bbox[2],
                    h=projected.normalized_bbox[3],
                )
    return items, track.revision if track is not None else None, projected_bbox
