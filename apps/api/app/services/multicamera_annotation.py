from __future__ import annotations

from dataclasses import dataclass
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.scene_track import SceneTrack
from app.db.models.task import Task
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.schemas.multicamera_annotation import (
    CameraAnnotationMemberOut,
    CameraProjectionResidual,
    NormalizedCameraBbox,
)
from app.services.annotation_evidence import record_annotation_actor
from app.services.pointcloud_projection import (
    ProjectionCamera,
    project_iso_box,
    projection_residual,
)
from app.services.sensor_calibration import (
    SensorCalibrationState,
    resolve_calibration_states,
)


class MulticameraAnnotationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CameraContext:
    item: DatasetItem
    role: str
    calibration: SensorCalibrationState
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


def _normalized_bbox(geometry: dict) -> tuple[float, float, float, float]:
    return (
        float(geometry["x"]),
        float(geometry["y"]),
        float(geometry["w"]),
        float(geometry["h"]),
    )


def _bbox_geometry(bbox: NormalizedCameraBbox) -> dict:
    return {"type": "bbox", **bbox.model_dump()}


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
) -> DatasetItem:
    if not camera_role.startswith("camera_"):
        raise MulticameraAnnotationError(
            "camera_role_not_linked", "camera role is not linked to this task"
        )
    link = (
        await db.execute(
            select(TaskDatasetItemLink)
            .where(TaskDatasetItemLink.task_id == task.id)
            .where(TaskDatasetItemLink.role == camera_role)
        )
    ).scalar_one_or_none()
    item = await db.get(DatasetItem, link.dataset_item_id) if link is not None else None
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
    links = list(
        (
            await db.execute(
                select(TaskDatasetItemLink).where(
                    TaskDatasetItemLink.task_id == task.id
                )
            )
        ).scalars()
    )
    links_by_role = {link.role: link for link in links}
    selected_links = [
        links_by_role[role] for role in camera_roles if role in links_by_role
    ]
    item_ids = {link.dataset_item_id for link in selected_links}
    primary_link = links_by_role.get("primary_lidar")
    if primary_link is not None:
        item_ids.add(primary_link.dataset_item_id)
    items = list(
        (
            await db.execute(select(DatasetItem).where(DatasetItem.id.in_(item_ids)))
        ).scalars()
    )
    items_by_id = {item.id: item for item in items}
    axis_convention = "iso_8855"
    if primary_link is not None:
        primary = items_by_id.get(primary_link.dataset_item_id)
        if primary is not None:
            dataset = await db.get(Dataset, primary.dataset_id)
            if dataset is not None:
                axis_convention = str(
                    (dataset.metadata_ or {}).get("axis_convention") or "iso_8855"
                )
    camera_items = [
        item
        for link in selected_links
        if (item := items_by_id.get(link.dataset_item_id)) is not None
    ]
    states = await resolve_calibration_states(db, camera_items)
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


async def _load_source_box(
    db: AsyncSession,
    *,
    task: Task,
    annotation_id: uuid.UUID,
    for_update: bool,
) -> Annotation:
    query = select(Annotation).where(Annotation.id == annotation_id)
    if for_update:
        query = query.with_for_update().execution_options(populate_existing=True)
    annotation = (await db.execute(query)).scalar_one_or_none()
    if (
        annotation is None
        or annotation.task_id != task.id
        or not annotation.is_active
        or annotation.was_cancelled
        or (annotation.geometry or {}).get("type") != "box_3d"
        or annotation.scene_track_id is None
        or annotation.track_id is None
    ):
        raise MulticameraAnnotationError(
            "source_box_invalid", "an active SceneTrack 3D box is required"
        )
    return annotation


async def _lock_track(
    db: AsyncSession, scene_track_id: uuid.UUID, expected_revision: int
) -> SceneTrack:
    track = (
        await db.execute(
            select(SceneTrack)
            .where(SceneTrack.id == scene_track_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if track is None or track.retired_at is not None:
        raise MulticameraAnnotationError(
            "scene_track_missing", "active SceneTrack was not found"
        )
    if track.revision != expected_revision:
        raise MulticameraAnnotationError(
            "track_revision_conflict", "SceneTrack changed"
        )
    return track


def _assert_calibration(
    state: SensorCalibrationState, expected_revision: int, expected_digest: str
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
    # A2 · lock the task before the source/SceneTrack rows are locked.
    await record_annotation_actor(db, task, actor_id)
    source = await _load_source_box(
        db,
        task=task,
        annotation_id=source_annotation_id,
        for_update=True,
    )
    track = await _lock_track(db, source.scene_track_id, expected_track_revision)
    context = await load_camera_context(db, task=task, camera_role=camera_role)
    _assert_calibration(
        context.calibration,
        expected_calibration_revision,
        expected_calibration_digest,
    )
    existing = await db.scalar(
        select(Annotation.id)
        .where(Annotation.task_id == task.id)
        .where(Annotation.scene_track_id == track.id)
        .where(Annotation.sensor_role == camera_role)
        .where(Annotation.is_active.is_(True))
        .where(Annotation.was_cancelled.is_(False))
        .limit(1)
    )
    if existing is not None:
        raise MulticameraAnnotationError(
            "camera_member_exists", "camera member already exists"
        )
    annotation = Annotation(
        id=uuid.uuid4(),
        task_id=task.id,
        project_id=task.project_id,
        user_id=actor_id,
        source="manual",
        annotation_type="bbox",
        tool_unit_id=source.tool_unit_id,
        class_name=source.class_name,
        geometry=_bbox_geometry(bbox),
        track_id=source.track_id,
        scene_track_id=source.scene_track_id,
        temporal_role="sample",
        sensor_dataset_item_id=context.item.id,
        sensor_role=camera_role,
        sensor_visibility=visibility,
        calibration_revision=context.calibration.revision,
        calibration_digest=context.calibration.digest,
        attributes={},
        attributes_meta={},
    )
    db.add(annotation)
    track.revision += 1
    await db.flush()
    await db.refresh(annotation)
    return annotation


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
    # A2 · lock the task before the member/SceneTrack rows are locked.
    await record_annotation_actor(db, task, actor_id)
    member = (
        await db.execute(
            select(Annotation)
            .where(Annotation.id == member_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if (
        member is None
        or member.task_id != task.id
        or not member.is_active
        or member.was_cancelled
        or member.sensor_role is None
        or member.scene_track_id is None
    ):
        raise MulticameraAnnotationError(
            "camera_member_missing", "active camera member was not found"
        )
    if member.version != expected_version:
        raise MulticameraAnnotationError(
            "annotation_version_conflict", "camera member changed"
        )
    track = await _lock_track(db, member.scene_track_id, expected_track_revision)
    context = await load_camera_context(db, task=task, camera_role=member.sensor_role)
    _assert_calibration(
        context.calibration,
        expected_calibration_revision,
        expected_calibration_digest,
    )
    if bbox is not None:
        member.geometry = _bbox_geometry(bbox)
    if visibility is not None:
        member.sensor_visibility = visibility
    member.calibration_revision = context.calibration.revision
    member.calibration_digest = context.calibration.digest
    member.version += 1
    track.revision += 1
    await db.flush()
    await db.refresh(member)
    return member


async def delete_camera_member(
    db: AsyncSession,
    *,
    task: Task,
    actor_id: uuid.UUID | None = None,
    member_id: uuid.UUID,
    expected_version: int,
    expected_track_revision: int,
) -> Annotation:
    # A2 · lock the task before the member/SceneTrack rows are locked.
    await record_annotation_actor(db, task, actor_id)
    member = (
        await db.execute(
            select(Annotation)
            .where(Annotation.id == member_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if (
        member is None
        or member.task_id != task.id
        or not member.is_active
        or member.sensor_role is None
        or member.scene_track_id is None
    ):
        raise MulticameraAnnotationError(
            "camera_member_missing", "active camera member was not found"
        )
    if member.version != expected_version:
        raise MulticameraAnnotationError(
            "annotation_version_conflict", "camera member changed"
        )
    track = await _lock_track(db, member.scene_track_id, expected_track_revision)
    member.is_active = False
    member.version += 1
    track.revision += 1
    await db.flush()
    await db.refresh(member)
    return member


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
    # A2 · lock the task before the member/SceneTrack rows are locked.
    await record_annotation_actor(db, task, actor_id)
    member = (
        await db.execute(
            select(Annotation)
            .where(Annotation.id == member_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if (
        member is None
        or member.task_id != task.id
        or member.is_active
        or member.was_cancelled
        or member.sensor_role is None
        or member.scene_track_id is None
    ):
        raise MulticameraAnnotationError(
            "camera_member_missing", "inactive camera member was not found"
        )
    if member.version != expected_version:
        raise MulticameraAnnotationError(
            "annotation_version_conflict", "camera member changed"
        )
    track = await _lock_track(db, member.scene_track_id, expected_track_revision)
    context = await load_camera_context(db, task=task, camera_role=member.sensor_role)
    _assert_calibration(
        context.calibration,
        expected_calibration_revision,
        expected_calibration_digest,
    )
    duplicate = await db.scalar(
        select(Annotation.id)
        .where(Annotation.task_id == task.id)
        .where(Annotation.scene_track_id == member.scene_track_id)
        .where(Annotation.sensor_role == member.sensor_role)
        .where(Annotation.is_active.is_(True))
        .where(Annotation.was_cancelled.is_(False))
        .limit(1)
    )
    if duplicate is not None:
        raise MulticameraAnnotationError(
            "camera_member_exists", "another active camera member already exists"
        )
    member.is_active = True
    member.calibration_revision = context.calibration.revision
    member.calibration_digest = context.calibration.digest
    member.version += 1
    track.revision += 1
    await db.flush()
    await db.refresh(member)
    return member


async def camera_member_out(
    db: AsyncSession,
    *,
    task: Task,
    member: Annotation,
    track: SceneTrack | None = None,
    context: CameraContext | None = None,
    source: Annotation | None = None,
) -> CameraAnnotationMemberOut:
    assert member.scene_track_id is not None
    assert member.track_id is not None
    assert member.sensor_role is not None
    assert member.sensor_dataset_item_id is not None
    if track is None:
        track = await db.get(SceneTrack, member.scene_track_id)
    if track is None:
        raise MulticameraAnnotationError(
            "scene_track_missing", "active SceneTrack was not found"
        )
    if context is None:
        context = await load_camera_context(
            db, task=task, camera_role=member.sensor_role
        )
    if source is None:
        source = (
            await db.execute(
                select(Annotation)
                .where(Annotation.task_id == task.id)
                .where(Annotation.scene_track_id == member.scene_track_id)
                .where(Annotation.sensor_role.is_(None))
                .where(Annotation.annotation_type == "box_3d")
                .where(Annotation.is_active.is_(True))
                .where(Annotation.was_cancelled.is_(False))
                .limit(1)
            )
        ).scalar_one_or_none()
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
    query = (
        select(Annotation)
        .where(Annotation.task_id == task.id)
        .where(Annotation.sensor_role.is_not(None))
        .where(Annotation.was_cancelled.is_(False))
        .order_by(Annotation.sensor_role, Annotation.created_at, Annotation.id)
    )
    if scene_track_id is not None:
        query = query.where(Annotation.scene_track_id == scene_track_id)
    if not include_inactive:
        query = query.where(Annotation.is_active.is_(True))
    rows = list((await db.execute(query)).scalars())
    track_ids = {row.scene_track_id for row in rows if row.scene_track_id is not None}
    if scene_track_id is not None:
        track_ids.add(scene_track_id)
    tracks = list(
        (
            await db.execute(select(SceneTrack).where(SceneTrack.id.in_(track_ids)))
        ).scalars()
    )
    tracks_by_id = {track.id: track for track in tracks}
    track = tracks_by_id.get(scene_track_id) if scene_track_id is not None else None
    if scene_track_id is not None and (
        track is None
        or track.project_id != task.project_id
        or track.retired_at is not None
    ):
        raise MulticameraAnnotationError(
            "scene_track_missing", "active SceneTrack was not found"
        )
    sources = list(
        (
            await db.execute(
                select(Annotation)
                .where(Annotation.task_id == task.id)
                .where(Annotation.scene_track_id.in_(track_ids))
                .where(Annotation.sensor_role.is_(None))
                .where(Annotation.annotation_type == "box_3d")
                .where(Annotation.is_active.is_(True))
                .where(Annotation.was_cancelled.is_(False))
            )
        ).scalars()
    )
    sources_by_track = {source.scene_track_id: source for source in sources}
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
