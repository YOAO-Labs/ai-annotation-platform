from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.orm.util import identity_key

from app.db.models.async_job import AsyncJob, AsyncJobKind
from app.db.models.point_cloud_quality import (
    PointCloudQualityIssue,
    PointCloudQualityRun,
)
from app.db.models.project import Project
from aap_workspace.point_quality_configuration_values import PointCloudQualityConfig
from app.schemas.point_cloud_quality import (
    PointCloudQualityRunRequest,
)
from app.services.quality_job_dispatch import QualityJobDispatcher
from app.services.async_job import create_job, get_job
from app.services.async_job_terminal import JobTerminal
from app.services.mask_qc.service import canonical_digest
from app.services.point_cloud_quality.config import (
    effective_thresholds,
    load_point_cloud_quality_config,
    point_cloud_quality_config_digest,
)
from app.services.labeling_scene import resolve_task_scene_frames
from app.services.labeling_scene import read_spatial_item_facts
from aap_assets_catalog.spatial_contract import (
    GetSceneRequest,
    ReadPosesRequest,
    SpatialFailure,
    normalize_calibration_record,
)
from app.services.assets_spatial import AssetsSpatialGateway, AssetsSpatialGatewayError
from app.services.labeling_spatial_quality import (
    quality_records,
    quality_row,
    quality_rows,
    spatial_quality_data,
)
from app.services.workspace_read_relations import project_rows


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
) -> tuple[list[dict], dict]:
    source = await spatial_quality_data(db, "read_point_source", project_id=project.id)
    tasks = quality_records(source.get("tasks"))
    task_by_id = {row.id: row for row in tasks}
    scene_frames = await resolve_task_scene_frames(db, list(task_by_id))

    project_scene_ids = {
        value.scene_id for value in scene_frames.values() if value.scene_id is not None
    }
    track_scene_ids = {
        uuid.UUID(str(value)) for value in source.get("track_scene_ids", [])
    }
    dataset_ids = {uuid.UUID(str(value)) for value in source.get("dataset_ids", [])}
    project_scene_ids.update(track_scene_ids)

    if request.scope == "scene_ids":
        requested_scene_ids = set(request.scene_ids)
        gateway = AssetsSpatialGateway(db)
        linked_scene_ids = set()
        for scene_id in requested_scene_ids:
            try:
                scene = await gateway.get_scene(
                    GetSceneRequest(actor_id=None, scene_id=scene_id)
                )
            except AssetsSpatialGatewayError as exc:
                if exc.code == "scene_missing":
                    continue
                raise
            if scene.scene.dataset_id in dataset_ids:
                linked_scene_ids.add(scene_id)
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

    if request.scope == "annotation_ids":
        initial_annotation_scope = "annotation_ids"
    elif request.scope == "project":
        initial_annotation_scope = "all"
    else:
        initial_annotation_scope = "task_ids"
    initial_details = await spatial_quality_data(
        db,
        "read_point_details",
        project_id=project.id,
        task_ids=sorted(selected_task_ids, key=str),
        scene_ids=sorted(selected_scene_ids, key=str),
        track_ids=[],
        annotation_ids=list(request.annotation_ids),
        annotation_scope=initial_annotation_scope,
    )
    annotations = quality_records(initial_details.get("annotations"))
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

    selected_track_ids = {
        row.scene_track_id for row in annotations if row.scene_track_id
    }
    if request.scope == "project":
        details = initial_details
    else:
        details = await spatial_quality_data(
            db,
            "read_point_details",
            project_id=project.id,
            task_ids=sorted(selected_task_ids, key=str),
            scene_ids=sorted(selected_scene_ids, key=str),
            track_ids=sorted(selected_track_ids, key=str),
            annotation_ids=list(request.annotation_ids),
            annotation_scope=initial_annotation_scope,
        )
    annotations = quality_records(details.get("annotations"))
    camera_annotations = quality_records(details.get("camera_annotations"))
    tracks = quality_records(details.get("tracks"))
    intervals = quality_records(details.get("intervals"))
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
    if len(annotations) + len(camera_annotations) > MAX_QUALITY_ANNOTATIONS:
        raise PointCloudQualityError(
            422,
            "point_cloud_quality_annotation_budget_exceeded",
            limit=MAX_QUALITY_ANNOTATIONS,
        )
    gateway = AssetsSpatialGateway(db)
    poses = [
        (scene_id, pose)
        for scene_id in sorted(selected_scene_ids, key=str)
        for pose in await gateway.read_poses(
            ReadPosesRequest(actor_id=None, scene_id=scene_id)
        )
    ]

    selected_tasks = [task_by_id[value] for value in sorted(selected_task_ids, key=str)]
    direct_item_ids = {
        task.dataset_item_id for task in selected_tasks if task.dataset_item_id
    }
    all_link_rows = quality_records(details.get("links"))
    linked_item_by_task = {
        row.task_id: row.dataset_item_id
        for row in all_link_rows
        if row.role == "primary_lidar"
    }
    camera_links_by_task: dict[uuid.UUID, list[tuple[str, uuid.UUID]]] = {}
    for row in all_link_rows:
        if row.role.startswith("camera_"):
            camera_links_by_task.setdefault(row.task_id, []).append(
                (row.role, row.dataset_item_id)
            )
    item_ids = direct_item_ids | {row.dataset_item_id for row in all_link_rows}
    item_by_id, _ = await read_spatial_item_facts(db, list(item_ids))
    calibration_by_item: dict[uuid.UUID, dict] = {}
    camera_item_ids = {
        item_id for links in camera_links_by_task.values() for _, item_id in links
    }
    calibration_states = {}
    for record in await gateway.read_calibrations(
        [item_id for item_id in camera_item_ids if item_id in item_by_id]
    ):
        try:
            calibration_states[record.dataset_item_id] = normalize_calibration_record(
                record
            )
        except SpatialFailure:
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
                        "axis_convention": item.axis_convention or "iso_8855",
                    }
                    if item is not None
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
    for scene_id, row in poses:
        records.append(
            {
                "kind": "pose",
                "scene_id": str(scene_id),
                "frame_index": row.frame_index,
                "ego_translation": row.ego_translation,
                "ego_rotation": row.ego_rotation,
            }
        )
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
    )
    return canonical_digest(snapshot)


async def create_quality_run(
    db: AsyncSession,
    *,
    project: Project,
    actor_id: uuid.UUID,
    request: PointCloudQualityRunRequest,
) -> tuple[PointCloudQualityRun, AsyncJob | None, bool]:
    if project.data_type != "lidar":
        raise PointCloudQualityError(422, "point_cloud_quality_lidar_project_required")
    config = load_point_cloud_quality_config(project.point_cloud_quality_config)
    if not config.enabled:
        raise PointCloudQualityError(409, "point_cloud_quality_disabled")
    snapshot, scope_json = await build_source_snapshot(
        db, project=project, request=request
    )
    if not await AssetsSpatialGateway(db).lock_scene_source_references(
        [uuid.UUID(value) for value in scope_json["scene_ids"]]
    ):
        raise PointCloudQualityError(409, "point_cloud_quality_source_changed")
    source_digest = canonical_digest(snapshot)
    config_digest = point_cloud_quality_config_digest(config)
    singleflight_key = canonical_digest(
        {
            "scope": scope_json,
            "source_snapshot_digest": source_digest,
            "config_digest": config_digest,
        }
    )
    result = await spatial_quality_data(
        db,
        "find_point_run",
        project_id=project.id,
        singleflight_key=singleflight_key,
        statuses=["pending", "running", "completed"],
    )
    existing = (
        quality_row(PointCloudQualityRun, result["row"]) if result["row"] else None
    )
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
                },
                celery_task_id=celery_task_id,
            )
            run_id = uuid.uuid4()
            run_result = await spatial_quality_data(
                db,
                "create_point_run",
                new_run_id=run_id,
                project_id=project.id,
                async_job_id=job.id,
                actor_id=actor_id,
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
            await db.flush()
            run = quality_row(PointCloudQualityRun, run_result["row"])
    except IntegrityError:
        result = await spatial_quality_data(
            db,
            "find_point_run",
            project_id=project.id,
            singleflight_key=singleflight_key,
            statuses=["pending", "running"],
        )
        existing = (
            quality_row(PointCloudQualityRun, result["row"]) if result["row"] else None
        )
        if existing is None:
            raise
        return existing, None, False
    return run, job, True


async def dispatch_quality_run(
    db: AsyncSession,
    *,
    run_id: uuid.UUID,
    async_job_id: uuid.UUID,
    dispatcher: QualityJobDispatcher,
) -> None:
    result = await spatial_quality_data(db, "get_point_run", run_id=run_id)
    run = quality_row(PointCloudQualityRun, result["row"]) if result["row"] else None
    job = await get_job(db, async_job_id)
    if run is None or job is None or not job.celery_task_id:
        raise PointCloudQualityError(500, "point_cloud_quality_dispatch_invalid")
    try:
        dispatcher.dispatch_run(
            "point_cloud_quality", run.id, execution_id=job.celery_task_id
        )
    except Exception as exc:
        raise PointCloudQualityError(
            503, "point_cloud_quality_dispatch_failed", message=str(exc)
        ) from exc


async def record_dispatch_failure(
    db: AsyncSession, *, run_id: uuid.UUID, error: str
) -> None:
    """Stage the original dispatch-failure fields; the caller owns commit."""
    await spatial_quality_data(
        db,
        "update_point_run",
        run_id=run_id,
        status="failed",
        error_message=error[:4000],
        completed_at=datetime.now(timezone.utc).isoformat(),
    )


async def refresh_issue_staleness(
    db: AsyncSession, issue: PointCloudQualityIssue
) -> bool:
    return (await refresh_issue_staleness_bulk(db, [issue]))[issue.id]


async def decide_issue(
    db: AsyncSession,
    *,
    issue: PointCloudQualityIssue,
    expected_task_id: uuid.UUID | None,
    actor_id: uuid.UUID,
    status: str,
    reason: str | None,
    review_verdict: str | None,
    review_note: str | None,
) -> None:
    """Apply a manual review decision to the current, freshly locked issue."""

    if issue.task_id != expected_task_id or await refresh_issue_staleness(db, issue):
        raise PointCloudQualityError(409, "point_cloud_quality_issue_stale")
    resolved_at = datetime.now(timezone.utc) if status != "open" else None
    reviewed_at = (
        datetime.now(timezone.utc)
        if status != "open" and review_verdict is not None
        else None
    )
    result = await spatial_quality_data(
        db,
        "decide_point_issue",
        issue_id=issue.id,
        actor_id=actor_id,
        review_status=status,
        expected_task_id=expected_task_id,
        resolution_reason=(reason or "").strip() or None,
        review_verdict=review_verdict,
        review_note=(review_note or "").strip() or None,
        resolved_at=resolved_at.isoformat() if resolved_at else None,
        reviewed_at=reviewed_at.isoformat() if reviewed_at else None,
    )
    if not result.get("row"):
        raise PointCloudQualityError(409, "point_cloud_quality_issue_stale")
    issue.status = status
    issue.resolution_reason = (reason or "").strip() or None
    issue.resolved_by_id = actor_id if status != "open" else None
    issue.resolved_at = resolved_at
    issue.review_verdict = review_verdict if status != "open" else None
    issue.review_note = (
        (review_note or "").strip() or None if status != "open" else None
    )
    issue.reviewed_by_id = actor_id if reviewed_at is not None else None
    issue.reviewed_at = reviewed_at


async def refresh_issue_staleness_bulk(
    db: AsyncSession, issues: list[PointCloudQualityIssue]
) -> dict[uuid.UUID, bool]:
    if not issues:
        return {}
    project_ids = {issue.project_id for issue in issues}
    project_rows_alias = aliased(Project, project_rows(), adapt_on_names=True)
    projects = {
        row.id: row
        for row in (
            await db.execute(
                select(project_rows_alias).where(project_rows_alias.id.in_(project_ids))
            )
        ).scalars()
    }
    run_ids = {
        run_id
        for issue in issues
        if (run_id := issue.last_seen_run_id or issue.run_id) is not None
    }
    annotation_ids = {
        uuid.UUID(raw_id)
        for issue in issues
        for raw_id in (issue.source_versions or {})
    }
    task_ids = {issue.task_id for issue in issues if issue.task_id is not None}
    track_ids = {
        issue.scene_track_id for issue in issues if issue.scene_track_id is not None
    }
    facts = await spatial_quality_data(
        db,
        "read_quality_facts",
        annotation_ids=sorted(annotation_ids, key=str),
        task_ids=sorted(task_ids, key=str),
        track_ids=sorted(track_ids, key=str),
        run_ids=sorted(run_ids, key=str),
    )
    runs = {row.id: row for row in quality_records(facts.get("runs"))}
    annotations = {row.id: row for row in quality_records(facts.get("annotations"))}
    tasks = {row.id: row for row in quality_records(facts.get("tasks"))}
    tracks = {row.id: row for row in quality_records(facts.get("tracks"))}
    links = quality_records(facts.get("links"))
    missing_direct_task_ids = {
        task_id for task_id, task in tasks.items() if task.dataset_item_id is None
    }
    linked_item_by_task = {
        row.task_id: row.dataset_item_id
        for row in links
        if row.task_id in missing_direct_task_ids and row.role == "primary_lidar"
    }
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
    items, _ = await read_spatial_item_facts(db, list(item_ids))
    camera_link_by_task_role: dict[tuple[uuid.UUID, str], uuid.UUID] = {}
    if camera_item_ids:
        camera_link_by_task_role = {
            (row.task_id, row.role): row.dataset_item_id
            for row in links
            if row.task_id in task_ids and row.role.startswith("camera_")
        }
    camera_calibration_states = {}
    for record in await AssetsSpatialGateway(db).read_calibrations(
        [item_id for item_id in camera_item_ids if item_id in items]
    ):
        try:
            camera_calibration_states[record.dataset_item_id] = (
                normalize_calibration_record(record)
            )
        except SpatialFailure:
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
    stale_issue_ids: list[uuid.UUID] = []
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
            stale_issue_ids.append(issue.id)
        result[issue.id] = stale
    if stale_issue_ids:
        await spatial_quality_data(
            db,
            "mark_point_issues_stale",
            stale_issue_ids=sorted(stale_issue_ids, key=str),
        )
        stale_ids = set(stale_issue_ids)
        for issue in issues:
            if issue.id not in stale_ids:
                continue
            cached = db.identity_map.get(identity_key(PointCloudQualityIssue, issue.id))
            # Update cached identities after the owner write without ORM UPDATEs.
            for target in (issue, cached):
                if target is not None:
                    set_committed_value(target, "status", "stale")
                    set_committed_value(target, "resolved_at", None)
                    set_committed_value(target, "resolved_by_id", None)
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
    values = {
        "project_id": project_id,
        "limit": limit,
        "offset": offset,
        "review_status": status,
        "severity_filter": severity,
        "code_filter": code,
        "scene_filter": scene_id,
        "task_filter": task_id,
        "annotation_filter": annotation_id,
        "track_filter": scene_track_id,
        "frame_filter": frame,
    }
    if allowed_task_ids is not None:
        values["allowed_task_ids"] = sorted(allowed_task_ids, key=str)
    data = await spatial_quality_data(db, "list_point_issues", **values)
    rows = quality_rows(PointCloudQualityIssue, data.get("rows"))
    if status:
        await refresh_issue_staleness_bulk(db, rows)
        await db.flush()
        data = await spatial_quality_data(db, "list_point_issues", **values)
        return quality_rows(PointCloudQualityIssue, data.get("rows")), int(
            data.get("total", 0)
        )
    await refresh_issue_staleness_bulk(db, rows)
    await db.flush()
    data = await spatial_quality_data(db, "list_point_issues", **values)
    return quality_rows(PointCloudQualityIssue, data.get("rows")), int(
        data.get("total", 0)
    )


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


async def reconcile_job_terminal(db: AsyncSession, terminal: JobTerminal) -> None:
    """Reconcile the quality ledger without changing a terminal run."""
    await spatial_quality_data(
        db,
        "reconcile_point_run",
        async_job_id=terminal.job_id,
        status="cancelled" if terminal.cancelled else "failed",
        completed_at=datetime.now(timezone.utc).isoformat(),
        **(
            {}
            if terminal.cancelled
            else {"error_message": (terminal.error or "")[:4000]}
        ),
    )
