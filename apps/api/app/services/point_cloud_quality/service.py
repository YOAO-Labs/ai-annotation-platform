from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.annotation import Annotation
from app.db.models.async_job import AsyncJob, AsyncJobKind
from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset, Scene
from app.db.models.point_cloud_quality import (
    PointCloudQualityIssue,
    PointCloudQualityRun,
)
from app.db.models.project import Project
from app.db.models.scene_pose import SceneFramePose
from app.db.models.scene_track import SceneTrack, SceneTrackInterval
from app.db.models.task import Task
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.schemas.point_cloud_quality import (
    PointCloudQualityConfig,
    PointCloudQualityRunRequest,
)
from app.services.async_job import create_job
from app.services.mask_qc.service import canonical_digest
from app.services.point_cloud_quality.config import (
    effective_thresholds,
    load_point_cloud_quality_config,
    point_cloud_quality_config_digest,
)
from app.services.scene import resolve_task_scene_frames
from app.services.sensor_calibration import (
    SensorCalibrationError,
    resolve_calibration_states,
)
from app.services.point_cloud_quality import originals


MAX_QUALITY_TASKS = 5_000
MAX_QUALITY_ANNOTATIONS = 20_000


class PointCloudQualityError(RuntimeError):
    def __init__(self, status_code: int, reason: str, **detail: Any):
        super().__init__(reason)
        self.status_code = status_code
        self.detail = {"reason": reason, **detail}


def _box_geometry(geometry: dict) -> bool:
    return geometry.get("type") == "box_3d"


async def build_source_snapshot(
    db: AsyncSession,
    *,
    project: Project,
    request: PointCloudQualityRunRequest,
    validate_expected: bool = True,
    original_contract: bool = False,
) -> tuple[list[dict], dict]:
    tasks = list(
        (
            await db.execute(
                select(Task)
                .execution_options(populate_existing=True)
                .where(Task.project_id == project.id)
                .order_by(Task.id)
            )
        )
        .scalars()
        .all()
    )
    task_by_id = {row.id: row for row in tasks}
    scene_frames = await resolve_task_scene_frames(db, list(task_by_id))

    project_scene_ids = {
        value.scene_id for value in scene_frames.values() if value.scene_id is not None
    }
    track_scene_ids = set(
        (
            await db.execute(
                select(SceneTrack.scene_id).where(SceneTrack.project_id == project.id)
            )
        ).scalars()
    )
    project_scene_ids.update(track_scene_ids)

    if request.scope == "scene_ids":
        requested_scene_ids = set(request.scene_ids)
        linked_scene_ids = set(
            (
                await db.execute(
                    select(Scene.id)
                    .join(ProjectDataset, ProjectDataset.dataset_id == Scene.dataset_id)
                    .where(
                        ProjectDataset.project_id == project.id,
                        Scene.id.in_(requested_scene_ids),
                    )
                )
            ).scalars()
        )
        if linked_scene_ids != requested_scene_ids:
            raise PointCloudQualityError(404, "point_cloud_quality_scene_not_found")
        selected_scene_ids = requested_scene_ids
        selected_task_ids = {
            task_id
            for task_id, value in scene_frames.items()
            if value.scene_id in selected_scene_ids
        }
    elif request.scope == "task_ids":
        selected_task_ids = set(request.task_ids)
        if not selected_task_ids.issubset(task_by_id):
            raise PointCloudQualityError(404, "point_cloud_quality_task_not_found")
        selected_scene_ids = {
            scene_frames[task_id].scene_id
            for task_id in selected_task_ids
            if scene_frames[task_id].scene_id is not None
        }
    elif request.scope == "project":
        selected_task_ids = set(task_by_id)
        selected_scene_ids = project_scene_ids
    else:
        selected_task_ids = set()
        selected_scene_ids = set()

    annotation_stmt = (
        select(Annotation)
        .execution_options(populate_existing=True)
        .where(
            Annotation.project_id == project.id,
            Annotation.annotation_type == "box_3d",
            Annotation.is_active.is_(True),
            Annotation.was_cancelled.is_(False),
        )
    )
    if request.scope == "annotation_ids":
        annotation_stmt = annotation_stmt.where(
            Annotation.id.in_(set(request.annotation_ids))
        )
    else:
        annotation_stmt = annotation_stmt.where(
            Annotation.task_id.in_(selected_task_ids)
        )
    annotations = list(
        (await db.execute(annotation_stmt.order_by(Annotation.id))).scalars().all()
    )
    if request.scope == "annotation_ids":
        if {row.id for row in annotations} != set(request.annotation_ids):
            raise PointCloudQualityError(
                404, "point_cloud_quality_annotation_not_found"
            )
        selected_task_ids = {row.task_id for row in annotations}
        selected_scene_ids = {
            scene_frames[row.task_id].scene_id
            for row in annotations
            if scene_frames[row.task_id].scene_id is not None
        }
    missing_scene_task_ids = {
        task_id
        for task_id in selected_task_ids
        if scene_frames[task_id].scene_id is None
        or scene_frames[task_id].frame_index is None
    }
    if missing_scene_task_ids and request.scope in {"task_ids", "annotation_ids"}:
        raise PointCloudQualityError(
            422,
            "point_cloud_quality_scene_required",
            task_ids=sorted(str(value) for value in missing_scene_task_ids),
        )
    if missing_scene_task_ids:
        selected_task_ids.difference_update(missing_scene_task_ids)
        annotations = [row for row in annotations if row.task_id in selected_task_ids]
    if len(selected_task_ids) > MAX_QUALITY_TASKS:
        raise PointCloudQualityError(
            422, "point_cloud_quality_task_budget_exceeded", limit=MAX_QUALITY_TASKS
        )
    if len(annotations) > MAX_QUALITY_ANNOTATIONS:
        raise PointCloudQualityError(
            422,
            "point_cloud_quality_annotation_budget_exceeded",
            limit=MAX_QUALITY_ANNOTATIONS,
        )
    if not selected_scene_ids:
        raise PointCloudQualityError(422, "point_cloud_quality_scene_required")

    current_versions = {str(row.id): row.version for row in annotations}
    if validate_expected:
        conflicts = {
            key: {"expected": expected, "actual": current_versions.get(key)}
            for key, expected in request.expected_versions.items()
            if current_versions.get(key) != expected
        }
        if conflicts:
            raise PointCloudQualityError(
                409, "point_cloud_quality_version_conflict", conflicts=conflicts
            )

    selected_track_ids = {
        row.scene_track_id for row in annotations if row.scene_track_id
    }
    camera_annotation_stmt = (
        select(Annotation)
        .execution_options(populate_existing=True)
        .where(
            Annotation.project_id == project.id,
            Annotation.sensor_role.is_not(None),
            Annotation.is_active.is_(True),
            Annotation.was_cancelled.is_(False),
            Annotation.task_id.in_(selected_task_ids),
        )
    )
    if request.scope in {"task_ids", "annotation_ids"}:
        camera_annotation_stmt = camera_annotation_stmt.where(
            Annotation.scene_track_id.in_(selected_track_ids)
        )
    camera_annotations = list(
        (await db.execute(camera_annotation_stmt.order_by(Annotation.id))).scalars()
    )
    if len(annotations) + len(camera_annotations) > MAX_QUALITY_ANNOTATIONS:
        raise PointCloudQualityError(
            422,
            "point_cloud_quality_annotation_budget_exceeded",
            limit=MAX_QUALITY_ANNOTATIONS,
        )
    track_stmt = (
        select(SceneTrack)
        .execution_options(populate_existing=True)
        .where(
            SceneTrack.project_id == project.id,
            SceneTrack.scene_id.in_(selected_scene_ids),
            SceneTrack.retired_at.is_(None),
        )
    )
    if request.scope in {"task_ids", "annotation_ids"}:
        track_stmt = track_stmt.where(SceneTrack.id.in_(selected_track_ids))
    tracks = list((await db.execute(track_stmt.order_by(SceneTrack.id))).scalars())
    intervals = list(
        (
            await db.execute(
                select(SceneTrackInterval)
                .execution_options(populate_existing=True)
                .where(
                    SceneTrackInterval.scene_track_id.in_([row.id for row in tracks])
                )
                .order_by(
                    SceneTrackInterval.scene_track_id,
                    SceneTrackInterval.start_frame,
                )
            )
        ).scalars()
    )
    poses = list(
        (
            await db.execute(
                select(SceneFramePose)
                .execution_options(populate_existing=True)
                .where(SceneFramePose.scene_id.in_(selected_scene_ids))
                .order_by(SceneFramePose.scene_id, SceneFramePose.frame_index)
            )
        ).scalars()
    )

    selected_tasks = [task_by_id[value] for value in sorted(selected_task_ids, key=str)]
    direct_item_ids = {
        task.dataset_item_id for task in selected_tasks if task.dataset_item_id
    }
    all_link_rows = list(
        await db.execute(
            select(
                TaskDatasetItemLink.task_id,
                TaskDatasetItemLink.dataset_item_id,
                TaskDatasetItemLink.role,
            ).where(TaskDatasetItemLink.task_id.in_(selected_task_ids))
        )
    )
    linked_item_by_task = {
        task_id: item_id
        for task_id, item_id, role in all_link_rows
        if role == "primary_lidar"
    }
    camera_links_by_task: dict[uuid.UUID, list[tuple[str, uuid.UUID]]] = {}
    for task_id, item_id, role in all_link_rows:
        if role.startswith("camera_"):
            camera_links_by_task.setdefault(task_id, []).append((role, item_id))
    item_ids = direct_item_ids | {item_id for _, item_id, _ in all_link_rows}
    items = list(
        (
            await db.execute(
                select(DatasetItem)
                .execution_options(populate_existing=True)
                .where(DatasetItem.id.in_(item_ids))
            )
        ).scalars()
    )
    item_by_id = {row.id: row for row in items}
    datasets = list(
        (
            await db.execute(
                select(Dataset)
                .execution_options(populate_existing=True)
                .where(Dataset.id.in_({row.dataset_id for row in items}))
            )
        ).scalars()
    )
    dataset_by_id = {row.id: row for row in datasets}
    calibration_by_item: dict[uuid.UUID, dict] = {}
    camera_item_ids = {
        item_id for links in camera_links_by_task.values() for _, item_id in links
    }
    camera_items = [
        item
        for item_id in camera_item_ids
        if (item := item_by_id.get(item_id)) is not None
    ]
    try:
        calibration_states = await resolve_calibration_states(db, camera_items)
    except SensorCalibrationError:
        # A malformed legacy item must not prevent the remaining scene snapshot.
        calibration_states = {}
        for item in camera_items:
            try:
                calibration_states.update(await resolve_calibration_states(db, [item]))
            except SensorCalibrationError:
                continue
    for item_id, state in calibration_states.items():
        calibration_by_item[item_id] = {
            "revision": state.revision,
            "digest": state.digest,
            "calibration": state.calibration.model_dump(mode="json"),
        }

    records: list[dict] = []
    for task in selected_tasks:
        scene_frame = scene_frames[task.id]
        item_id = task.dataset_item_id or linked_item_by_task.get(task.id)
        item = item_by_id.get(item_id)
        records.append(
            {
                "kind": "task",
                "task_id": str(task.id),
                "task_version": task.version,
                "scene_id": str(scene_frame.scene_id),
                "frame_index": scene_frame.frame_index,
                "pointcloud": (
                    {
                        "item_id": str(item.id),
                        "file_path": item.file_path,
                        "content_hash": item.content_hash,
                        "file_size": item.file_size,
                        "axis_convention": (
                            dataset_by_id[item.dataset_id].metadata_ or {}
                        ).get("axis_convention", "iso_8855"),
                    }
                    if item is not None and item.dataset_id in dataset_by_id
                    else None
                ),
                "cameras": [
                    {
                        "role": role,
                        "item_id": str(camera_item.id),
                        "width": camera_item.width,
                        "height": camera_item.height,
                        **calibration_by_item[camera_item.id],
                    }
                    for role, camera_item_id in sorted(
                        camera_links_by_task.get(task.id, [])
                    )
                    if (camera_item := item_by_id.get(camera_item_id)) is not None
                    and camera_item.id in calibration_by_item
                ],
            }
        )
    for row in annotations:
        scene_frame = scene_frames[row.task_id]
        if scene_frame.scene_id is None or scene_frame.frame_index is None:
            continue
        records.append(
            {
                "kind": "annotation",
                "annotation_id": str(row.id),
                "annotation_version": row.version,
                "task_id": str(row.task_id),
                "scene_id": str(scene_frame.scene_id),
                "frame_index": scene_frame.frame_index,
                "class_name": row.class_name,
                "track_id": row.track_id,
                "scene_track_id": str(row.scene_track_id)
                if row.scene_track_id
                else None,
                "geometry": row.geometry,
                "geometry_digest": canonical_digest(row.geometry),
            }
        )
    for row in camera_annotations:
        scene_frame = scene_frames[row.task_id]
        if scene_frame.scene_id is None or scene_frame.frame_index is None:
            continue
        records.append(
            {
                "kind": "camera_annotation",
                "annotation_id": str(row.id),
                "annotation_version": row.version,
                "task_id": str(row.task_id),
                "scene_id": str(scene_frame.scene_id),
                "frame_index": scene_frame.frame_index,
                "class_name": row.class_name,
                "track_id": row.track_id,
                "scene_track_id": (
                    str(row.scene_track_id) if row.scene_track_id else None
                ),
                "sensor_role": row.sensor_role,
                "sensor_dataset_item_id": str(row.sensor_dataset_item_id),
                "sensor_visibility": row.sensor_visibility,
                "calibration_revision": row.calibration_revision,
                "calibration_digest": row.calibration_digest,
                "geometry": row.geometry,
                "geometry_digest": canonical_digest(row.geometry),
            }
        )
    for row in tracks:
        records.append(
            {
                "kind": "track",
                "scene_track_id": str(row.id),
                "scene_id": str(row.scene_id),
                "track_id": row.track_id,
                "class_name": row.class_name,
                "presence_mode": row.presence_mode,
                "revision": row.revision,
            }
        )
    for row in intervals:
        records.append(
            {
                "kind": "interval",
                "interval_id": str(row.id),
                "scene_track_id": str(row.scene_track_id),
                "start_frame": row.start_frame,
                "end_frame": row.end_frame,
                "version": row.version,
            }
        )
    for row in poses:
        records.append(
            {
                "kind": "pose",
                "scene_id": str(row.scene_id),
                "frame_index": row.frame_index,
                "ego_translation": row.ego_translation,
                "ego_rotation": row.ego_rotation,
            }
        )
    if original_contract:
        records = [
            await originals.pin(db, record) if record["kind"] == "task" else record
            for record in records
        ]
    records.sort(key=lambda value: canonical_digest(value))
    scope_json = {
        "scope": request.scope,
        "scene_ids": sorted(str(value) for value in selected_scene_ids),
        "task_ids": sorted(str(value) for value in selected_task_ids),
        "annotation_ids": sorted(str(row.id) for row in annotations),
        "expected_versions": current_versions,
    }
    return records, scope_json


def _request_from_scope(scope: dict) -> PointCloudQualityRunRequest:
    original_scope = scope["scope"]
    kwargs: dict[str, Any] = {"scope": original_scope}
    if original_scope != "project":
        kwargs[original_scope] = scope[original_scope]
    return PointCloudQualityRunRequest(**kwargs)


async def current_source_digest(
    db: AsyncSession, run: PointCloudQualityRun, project: Project
) -> str:
    snapshot, _scope = await build_source_snapshot(
        db,
        project=project,
        request=_request_from_scope(run.scope_json),
        validate_expected=False,
        original_contract=run.scope_json.get("original_contract") == 1,
    )
    return canonical_digest(snapshot)


async def create_quality_run(
    db: AsyncSession,
    *,
    project: Project,
    actor_id: uuid.UUID,
    request: PointCloudQualityRunRequest,
    http_request,
) -> tuple[PointCloudQualityRun, AsyncJob | None, bool]:
    if project.data_type != "lidar":
        raise PointCloudQualityError(422, "point_cloud_quality_lidar_project_required")
    authority = await originals.capture_authority(db, actor_id, http_request)
    config = load_point_cloud_quality_config(project.point_cloud_quality_config)
    if not config.enabled:
        raise PointCloudQualityError(409, "point_cloud_quality_disabled")
    snapshot, scope_json = await build_source_snapshot(
        db, project=project, request=request, original_contract=True
    )
    scope_json = {**scope_json, "original_contract": 1}
    await originals.check_authority(
        db, authority, project.id, scope_json, all_tasks=True
    )
    source_digest = canonical_digest(snapshot)
    config_digest = point_cloud_quality_config_digest(config)
    singleflight_key = canonical_digest(
        {
            "scope": scope_json,
            "source_snapshot_digest": source_digest,
            "config_digest": config_digest,
            "authority": originals.authorization(authority),
        }
    )
    existing = (
        await db.execute(
            select(PointCloudQualityRun)
            .where(
                PointCloudQualityRun.project_id == project.id,
                PointCloudQualityRun.singleflight_key == singleflight_key,
                PointCloudQualityRun.status.in_(("pending", "running", "completed")),
            )
            .order_by(PointCloudQualityRun.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, None, False

    celery_task_id = str(uuid.uuid4())
    try:
        async with db.begin_nested():
            job = await create_job(
                db,
                kind=AsyncJobKind.POINT_CLOUD_QUALITY.value,
                user_id=actor_id,
                project_id=project.id,
                payload={
                    "scope": scope_json,
                    "source_snapshot_digest": source_digest,
                    originals.AUTHORITY: originals.authorization(authority),
                },
                celery_task_id=celery_task_id,
            )
            run = PointCloudQualityRun(
                project_id=project.id,
                async_job_id=job.id,
                requested_by_id=actor_id,
                scope_json=scope_json,
                config_revision=config.config_revision,
                config_digest=config_digest,
                config_snapshot=config.model_dump(mode="json"),
                source_snapshot=snapshot,
                source_snapshot_digest=source_digest,
                singleflight_key=singleflight_key,
                summary={
                    "task_count": len(scope_json["task_ids"]),
                    "annotation_count": len(scope_json["annotation_ids"]),
                    "scene_count": len(scope_json["scene_ids"]),
                },
            )
            db.add(run)
            await db.flush()
    except IntegrityError:
        existing = (
            await db.execute(
                select(PointCloudQualityRun).where(
                    PointCloudQualityRun.project_id == project.id,
                    PointCloudQualityRun.singleflight_key == singleflight_key,
                    PointCloudQualityRun.status.in_(("pending", "running")),
                )
            )
        ).scalar_one()
        return existing, None, False
    await originals.check_authority(
        db, authority, project.id, scope_json, all_tasks=True
    )
    return run, job, True


async def dispatch_quality_run(
    db: AsyncSession, *, run_id: uuid.UUID, async_job_id: uuid.UUID
) -> None:
    run = await db.get(PointCloudQualityRun, run_id)
    job = await db.get(AsyncJob, async_job_id)
    if run is None or job is None or not job.celery_task_id:
        raise PointCloudQualityError(500, "point_cloud_quality_dispatch_invalid")
    try:
        from app.workers.point_cloud_quality import run_point_cloud_quality

        run_point_cloud_quality.apply_async(
            args=[str(run.id)], task_id=job.celery_task_id
        )
    except Exception as exc:
        raise PointCloudQualityError(
            503, "point_cloud_quality_dispatch_failed", message=str(exc)
        ) from exc


async def refresh_issue_staleness(
    db: AsyncSession, issue: PointCloudQualityIssue
) -> bool:
    return (await refresh_issue_staleness_bulk(db, [issue]))[issue.id]


async def refresh_issue_staleness_bulk(
    db: AsyncSession, issues: list[PointCloudQualityIssue]
) -> dict[uuid.UUID, bool]:
    if not issues:
        return {}
    project_ids = {issue.project_id for issue in issues}
    projects = {
        row.id: row
        for row in (
            await db.execute(select(Project).where(Project.id.in_(project_ids)))
        ).scalars()
    }
    run_ids = {
        run_id
        for issue in issues
        if (run_id := issue.last_seen_run_id or issue.run_id) is not None
    }
    runs = {
        row.id: row
        for row in (
            await db.execute(
                select(PointCloudQualityRun).where(PointCloudQualityRun.id.in_(run_ids))
            )
        ).scalars()
    }
    annotation_ids = {
        uuid.UUID(raw_id)
        for issue in issues
        for raw_id in (issue.source_versions or {})
    }
    annotations = {
        row.id: row
        for row in (
            await db.execute(
                select(Annotation)
                .execution_options(populate_existing=True)
                .where(Annotation.id.in_(annotation_ids))
            )
        ).scalars()
    }
    task_ids = {issue.task_id for issue in issues if issue.task_id is not None}
    tasks = {
        row.id: row
        for row in (
            await db.execute(
                select(Task)
                .execution_options(populate_existing=True)
                .where(Task.id.in_(task_ids))
            )
        ).scalars()
    }
    missing_direct_task_ids = {
        task_id for task_id, task in tasks.items() if task.dataset_item_id is None
    }
    linked_item_by_task = dict(
        (
            await db.execute(
                select(
                    TaskDatasetItemLink.task_id,
                    TaskDatasetItemLink.dataset_item_id,
                ).where(
                    TaskDatasetItemLink.task_id.in_(missing_direct_task_ids),
                    TaskDatasetItemLink.role == "primary_lidar",
                )
            )
        ).all()
    )
    pointcloud_item_ids = {
        uuid.UUID(str(source_item_id))
        for issue in issues
        if (source_item_id := (issue.evidence or {}).get("pointcloud_item_id"))
    }
    camera_item_ids = {
        uuid.UUID(str(source_item_id))
        for issue in issues
        if (source_item_id := (issue.evidence or {}).get("camera_item_id"))
    }
    item_ids = pointcloud_item_ids | camera_item_ids
    items = {
        row.id: row
        for row in (
            await db.execute(
                select(DatasetItem)
                .execution_options(populate_existing=True)
                .where(DatasetItem.id.in_(item_ids))
            )
        ).scalars()
    }
    pointcloud_datasets = (
        {
            row.id: row
            for row in await db.scalars(
                select(Dataset)
                .where(Dataset.id.in_({item.dataset_id for item in items.values()}))
                .execution_options(populate_existing=True)
            )
        }
        if pointcloud_item_ids
        else {}
    )
    current_originals = {}
    track_ids = {
        issue.scene_track_id for issue in issues if issue.scene_track_id is not None
    }
    tracks = {
        row.id: row
        for row in (
            await db.execute(
                select(SceneTrack)
                .execution_options(populate_existing=True)
                .where(SceneTrack.id.in_(track_ids))
            )
        ).scalars()
    }
    camera_link_by_task_role: dict[tuple[uuid.UUID, str], uuid.UUID] = {}
    if camera_item_ids:
        camera_link_by_task_role = {
            (task_id, role): item_id
            for task_id, item_id, role in (
                await db.execute(
                    select(
                        TaskDatasetItemLink.task_id,
                        TaskDatasetItemLink.dataset_item_id,
                        TaskDatasetItemLink.role,
                    ).where(
                        TaskDatasetItemLink.task_id.in_(task_ids),
                        TaskDatasetItemLink.role.like("camera_%"),
                    )
                )
            ).all()
        }
    camera_items = [items[item_id] for item_id in camera_item_ids if item_id in items]
    try:
        camera_calibration_states = await resolve_calibration_states(db, camera_items)
    except SensorCalibrationError:
        # Keep malformed legacy camera metadata local to that camera while retaining
        # one-query behavior for the normal path.
        camera_calibration_states = {}
        for item in camera_items:
            try:
                camera_calibration_states.update(
                    await resolve_calibration_states(db, [item])
                )
            except SensorCalibrationError:
                continue
    camera_calibration_digests: dict[uuid.UUID, str | None] = {
        item_id: (
            state.digest
            if (state := camera_calibration_states.get(item_id)) is not None
            else None
        )
        for item_id in camera_item_ids
    }
    config_digests = {
        project_id: point_cloud_quality_config_digest(
            load_point_cloud_quality_config(project.point_cloud_quality_config)
        )
        for project_id, project in projects.items()
    }

    result: dict[uuid.UUID, bool] = {}
    for issue in issues:
        stale = False
        run_id = issue.last_seen_run_id or issue.run_id
        if run_id is not None:
            run = runs.get(run_id)
            stale = (
                run is None
                or issue.project_id not in projects
                or config_digests.get(issue.project_id) != run.config_digest
            )
        for raw_id, version in (issue.source_versions or {}).items():
            if stale:
                break
            annotation = annotations.get(uuid.UUID(raw_id))
            stale = bool(
                annotation is None
                or not annotation.is_active
                or annotation.was_cancelled
                or annotation.version != int(version)
            )
        source_item_id = (issue.evidence or {}).get("pointcloud_item_id")
        if not stale and source_item_id is not None:
            expected_item_id = uuid.UUID(str(source_item_id))
            task = tasks.get(issue.task_id) if issue.task_id is not None else None
            current_item_id = (
                task.dataset_item_id
                if task is not None and task.dataset_item_id is not None
                else linked_item_by_task.get(issue.task_id)
            )
            item = items.get(expected_item_id)
            stale = bool(
                current_item_id != expected_item_id
                or item is None
                or item.content_hash
                != (issue.evidence or {}).get("pointcloud_content_hash")
                or item.file_path != (issue.evidence or {}).get("pointcloud_file_path")
            )
            if not stale:
                evidence = issue.evidence or {}
                dataset = pointcloud_datasets.get(item.dataset_id)
                convention = (
                    ((dataset.metadata_ or {}).get("axis_convention") or "iso_8855")
                    if dataset
                    else None
                )
                stale = evidence.get(
                    "pointcloud_contract"
                ) != 1 or convention != evidence.get("axis_convention")
                if not stale:
                    if issue.task_id not in current_originals:
                        try:
                            current_originals[issue.task_id] = await originals.pin(
                                db,
                                {
                                    "task_id": str(issue.task_id),
                                    "pointcloud": {
                                        "item_id": str(item.id),
                                        "file_path": item.file_path,
                                    },
                                },
                                lock=False,
                            )
                        except originals.ContentError:
                            current_originals[issue.task_id] = None
                    current = current_originals[issue.task_id]
                    stale = current is None or (
                        current["pointcloud"].get("original")
                        != evidence.get("pointcloud_original")
                        or current["pointcloud"].get("original_error")
                        != evidence.get("pointcloud_original_error")
                    )
        camera_item_id = (issue.evidence or {}).get("camera_item_id")
        camera_role = (issue.evidence or {}).get("camera_role")
        if not stale and camera_item_id is not None and camera_role is not None:
            expected_camera_item_id = uuid.UUID(str(camera_item_id))
            stale = bool(
                issue.task_id is None
                or camera_link_by_task_role.get((issue.task_id, str(camera_role)))
                != expected_camera_item_id
                or camera_calibration_digests.get(expected_camera_item_id)
                != (issue.evidence or {}).get("camera_calibration_digest")
            )
        if not stale and issue.scene_track_id is not None:
            track = tracks.get(issue.scene_track_id)
            stale = track is None or track.revision != issue.track_revision
        if stale and issue.status != "stale":
            issue.status = "stale"
            issue.resolved_at = None
            issue.resolved_by_id = None
        result[issue.id] = stale
    return result


async def list_issues(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    status: str | None = None,
    severity: str | None = None,
    code: str | None = None,
    scene_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    annotation_id: uuid.UUID | None = None,
    scene_track_id: uuid.UUID | None = None,
    frame: int | None = None,
    allowed_task_ids: set[uuid.UUID] | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[PointCloudQualityIssue], int]:
    filters = [PointCloudQualityIssue.project_id == project_id]
    if allowed_task_ids is not None:
        filters.append(PointCloudQualityIssue.task_id.in_(allowed_task_ids))
    if severity:
        filters.append(PointCloudQualityIssue.severity == severity)
    if code:
        filters.append(PointCloudQualityIssue.code == code)
    if scene_id:
        filters.append(PointCloudQualityIssue.scene_id == scene_id)
    if task_id:
        filters.append(PointCloudQualityIssue.task_id == task_id)
    if annotation_id:
        filters.append(
            or_(
                PointCloudQualityIssue.annotation_id == annotation_id,
                PointCloudQualityIssue.related_annotation_ids.any(annotation_id),
            )
        )
    if scene_track_id:
        filters.append(PointCloudQualityIssue.scene_track_id == scene_track_id)
    if frame is not None:
        filters.extend(
            [
                PointCloudQualityIssue.frame_start <= frame,
                PointCloudQualityIssue.frame_end >= frame,
            ]
        )
    if status:
        refresh_filters = list(filters)
        if status == "stale":
            refresh_filters.append(PointCloudQualityIssue.status != "stale")
        else:
            refresh_filters.append(PointCloudQualityIssue.status == status)
        candidates = list(
            (
                await db.execute(select(PointCloudQualityIssue).where(*refresh_filters))
            ).scalars()
        )
        await refresh_issue_staleness_bulk(db, candidates)
        await db.flush()
        filters.append(PointCloudQualityIssue.status == status)
    rows = list(
        (
            await db.execute(
                select(PointCloudQualityIssue)
                .where(*filters)
                .order_by(
                    PointCloudQualityIssue.severity_rank,
                    PointCloudQualityIssue.created_at.desc(),
                    PointCloudQualityIssue.id,
                )
                .offset(offset)
                .limit(limit)
            )
        ).scalars()
    )
    if not status:
        await refresh_issue_staleness_bulk(db, rows)
        await db.flush()
    total = int(
        (
            await db.execute(
                select(func.count()).select_from(PointCloudQualityIssue).where(*filters)
            )
        ).scalar_one()
    )
    return rows, total


def thresholds_from_config(
    config: PointCloudQualityConfig, class_name: str | None = None
):
    from app.services.point_cloud_quality.kernel import QualityThresholds

    return QualityThresholds(**effective_thresholds(config, class_name).model_dump())


def issue_dedupe_key(
    *,
    project_id: uuid.UUID,
    code: str,
    scene_id: uuid.UUID,
    annotation_id: uuid.UUID | None,
    scene_track_id: uuid.UUID | None,
    frame_start: int | None,
    frame_end: int | None,
    related_annotation_ids: list[uuid.UUID],
) -> str:
    return canonical_digest(
        {
            "project_id": str(project_id),
            "code": code,
            "scene_id": str(scene_id),
            "annotation_id": str(annotation_id) if annotation_id else None,
            "scene_track_id": str(scene_track_id) if scene_track_id else None,
            "frame_start": frame_start,
            "frame_end": frame_end,
            "related_annotation_ids": sorted(
                str(value) for value in related_annotation_ids
            ),
        }
    )
