"""Point-cloud quality scan persistence and run execution state machine."""

from __future__ import annotations

import asyncio
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.async_job import AsyncJob, AsyncJobStatus
from app.services.project_access import get_workspace_project
from app.db.models.point_cloud_quality import (
    PointCloudQualityIssue,
    PointCloudQualityRun,
)
from aap_workspace.point_quality_configuration_values import PointCloudQualityConfig
from aap_assets_catalog.spatial_values import SensorCalibration
from app.services.jobs_lifecycle_data import job_lock_statement
from app.services.point_cloud_quality import originals
from app.services.point_cloud_quality.config import severity_for_rule
from app.services.point_cloud_quality.kernel import (
    Box3D,
    QualityFinding,
    TrackInterval,
    TrackMember,
    evaluate_box,
    evaluate_projection_residual,
    evaluate_track,
)
from app.services.pointcloud_projection import project_iso_box, projection_residual
from app.services.point_cloud_quality.service import (
    current_source_digest,
    issue_dedupe_key,
    thresholds_from_config,
)
from app.services.quality_job_lifecycle import QualityJobLifecycle
from app.services.labeling_spatial_quality import quality_row, spatial_quality_data


SEVERITY_RANK = {"blocker": 0, "warning": 1, "info": 2}


@dataclass(frozen=True)
class _ProjectionCamera:
    calibration: SensorCalibration
    width: int
    height: int


async def _update_run(
    db, run: PointCloudQualityRun, status: str, **values
) -> PointCloudQualityRun:
    result = await spatial_quality_data(
        db, "update_point_run", run_id=run.id, status=status, **values
    )
    if not result.get("row"):
        raise RuntimeError("point cloud quality run disappeared during update")
    run.status = (
        "running"
        if status == "start"
        else status
        if status != "progress"
        else run.status
    )
    if status == "start":
        run.progress_pct = 1
    for name, value in values.items():
        if name.endswith("_at") and isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        setattr(run, name, value)
    return run


def _box(value: dict) -> Box3D:
    return Box3D(
        center=tuple(float(item) for item in value["center"]),
        size=tuple(float(item) for item in value["size"]),
        rotation=tuple(float(item) for item in value["rotation"]),
    )


async def _cancel_requested(db, run: PointCloudQualityRun) -> bool:
    if run.async_job_id is None:
        return False
    job = (
        await db.execute(
            select(AsyncJob.status, AsyncJob.payload).from_statement(
                text(
                    "SELECT status, payload FROM aap_jobs_v1.read_job("
                    "CAST(:job_id AS pg_catalog.uuid))"
                )
            ),
            {"job_id": run.async_job_id},
        )
    ).one_or_none()
    return bool(
        job is not None
        and (
            job.status == AsyncJobStatus.CANCELLED.value
            or bool((job.payload or {}).get("cancel_requested"))
        )
    )


def _locator(
    *,
    scene_id: uuid.UUID,
    task_id: uuid.UUID | None,
    frame_index: int | None,
    annotation_id: uuid.UUID | None,
    scene_track_id: uuid.UUID | None,
    code: str,
) -> dict:
    auxiliary_layers = []
    if code == "ground_clearance":
        auxiliary_layers.append("ground")
    if code == "temporal_jump":
        auxiliary_layers.append("neighbor_frames")
    if code.startswith("track_") or code == "duplicate_track_member":
        auxiliary_layers.append("track")
    return {
        "scene_id": str(scene_id),
        "frame_index": frame_index,
        "task_id": str(task_id) if task_id else None,
        "annotation_id": str(annotation_id) if annotation_id else None,
        "scene_track_id": str(scene_track_id) if scene_track_id else None,
        "camera": None,
        "auxiliary_layers": auxiliary_layers,
    }


async def _upsert_finding(
    db,
    *,
    run: PointCloudQualityRun,
    finding: QualityFinding,
    config: PointCloudQualityConfig,
    scene_id: uuid.UUID,
    task_id: uuid.UUID | None,
    annotation_id: uuid.UUID | None,
    annotation_version: int | None,
    scene_track_id: uuid.UUID | None,
    track_revision: int | None,
    source_versions: dict[str, int],
    class_name: str | None,
    source_evidence: dict | None = None,
) -> PointCloudQualityIssue | None:
    severity = severity_for_rule(config, finding.code)
    if severity is None:
        return None
    if finding.code not in config.severity_overrides:
        severity = finding.severity
    related_ids = sorted(set(finding.annotation_ids), key=str)
    frame_start = finding.frame_start
    frame_end = finding.frame_end
    key = issue_dedupe_key(
        project_id=run.project_id,
        code=finding.code,
        scene_id=scene_id,
        annotation_id=annotation_id,
        scene_track_id=scene_track_id,
        frame_start=frame_start,
        frame_end=frame_end,
        related_annotation_ids=related_ids,
    )
    locator = _locator(
        scene_id=scene_id,
        task_id=task_id,
        frame_index=frame_start,
        annotation_id=annotation_id,
        scene_track_id=scene_track_id,
        code=finding.code,
    )
    locator["camera"] = finding.evidence.get("camera_role")
    if locator["camera"]:
        locator["auxiliary_layers"].append("camera_projection")
    result = await spatial_quality_data(
        db,
        "upsert_point_issue",
        new_issue_id=uuid.uuid4(),
        run_id=run.id,
        project_id=run.project_id,
        scene_id=scene_id,
        task_id=task_id,
        annotation_id=annotation_id,
        annotation_version=annotation_version,
        scene_track_id=scene_track_id,
        track_revision=track_revision,
        related_annotation_ids=related_ids,
        source_versions=source_versions,
        class_name=class_name,
        code=finding.code,
        rule_version=1,
        severity=severity,
        severity_rank=SEVERITY_RANK[severity],
        frame_start=frame_start,
        frame_end=frame_end,
        metric=finding.metric,
        threshold=finding.threshold,
        evidence={**finding.evidence, **(source_evidence or {})},
        locator=locator,
        suggested_command=finding.suggestion,
        dedupe_key=key,
    )
    await db.flush()
    return quality_row(PointCloudQualityIssue, result["row"])


async def execute_scan(
    db: AsyncSession,
    run: PointCloudQualityRun,
    *,
    job_lifecycle: QualityJobLifecycle,
    context: originals.Context | None = None,
) -> dict:
    context = context or await originals.delegated(db, run)
    await originals.guard(db, context)
    await db.commit()
    pending: list[dict] = []
    observed: dict[str, tuple[object, str]] = {}
    config = PointCloudQualityConfig.model_validate(run.config_snapshot)
    records = list(run.source_snapshot or [])
    tasks = {row["task_id"]: row for row in records if row["kind"] == "task"}
    annotations = sorted(
        (row for row in records if row["kind"] == "annotation"),
        key=lambda row: (row["task_id"], row["annotation_id"]),
    )
    camera_annotations = sorted(
        (row for row in records if row["kind"] == "camera_annotation"),
        key=lambda row: (row["task_id"], row["annotation_id"]),
    )
    all_tracks = [row for row in records if row["kind"] == "track"]
    complete_track_scope = run.scope_json.get("scope") in {"project", "scene_ids"}
    tracks = all_tracks if complete_track_scope else []
    intervals_by_track: dict[str, list[dict]] = defaultdict(list)
    for row in records:
        if row["kind"] == "interval":
            intervals_by_track[row["scene_track_id"]].append(row)
    poses = {
        (row["scene_id"], int(row["frame_index"])): row
        for row in records
        if row["kind"] == "pose"
    }
    task_by_scene_frame = {
        (row["scene_id"], int(row["frame_index"])): row["task_id"]
        for row in tasks.values()
        if row["frame_index"] is not None
    }
    camera_annotations_by_track: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in camera_annotations:
        if row["scene_track_id"]:
            camera_annotations_by_track[(row["task_id"], row["scene_track_id"])].append(
                row
            )
    track_revision_by_id = {
        row["scene_track_id"]: int(row["revision"]) for row in all_tracks
    }
    sizes_by_class: dict[str, list[tuple[float, float, float]]] = defaultdict(list)
    for row in annotations:
        geometry = row["geometry"]
        if geometry.get("type") == "box_3d":
            sizes_by_class[row["class_name"]].append(
                tuple(float(value) for value in geometry["size"])
            )

    skip_counts: Counter[str] = Counter()
    if all_tracks and not complete_track_scope:
        skip_counts["track_rules:scope_incomplete"] = len(all_tracks)
    pointcloud_cache_key: tuple[str, str] | None = None
    pointcloud_cache_value: np.ndarray | None = None
    total_units = max(1, len(annotations) + len(tracks))
    processed = 0
    for row in annotations:
        if await _cancel_requested(db, run):
            raise asyncio.CancelledError
        task = tasks[row["task_id"]]
        pointcloud = task.get("pointcloud")
        points: np.ndarray | None = None
        source = await originals.freeze(db, context, task)
        if source is not None:
            convention = pointcloud.get("axis_convention") or "iso_8855"
            observed[row["task_id"]] = (source, convention)
            cache_key = (originals.canonical_digest(pointcloud["original"]), convention)
            if cache_key != pointcloud_cache_key:
                try:
                    pointcloud_cache_value = await originals.read_points(
                        db, context, source, convention
                    )
                except originals.Unavailable as exc:
                    pointcloud_cache_value = None
                    skip_counts[f"pointcloud:{exc}"] += 1
                pointcloud_cache_key = cache_key
            else:
                await originals.recheck(db, context, source, convention)
            points = pointcloud_cache_value
        else:
            skip_counts[
                "pointcloud:" + ((pointcloud or {}).get("original_error") or "missing")
            ] += 1
        findings = evaluate_box(
            points,
            _box(row["geometry"]),
            thresholds=thresholds_from_config(config, row["class_name"]),
            size_samples=sizes_by_class[row["class_name"]],
        )
        for finding in findings:
            pending.append(
                {
                    "run": run,
                    "finding": finding,
                    "config": config,
                    "scene_id": uuid.UUID(row["scene_id"]),
                    "task_id": uuid.UUID(row["task_id"]),
                    "annotation_id": uuid.UUID(row["annotation_id"]),
                    "annotation_version": int(row["annotation_version"]),
                    "scene_track_id": (
                        uuid.UUID(row["scene_track_id"])
                        if row["scene_track_id"]
                        else None
                    ),
                    "track_revision": track_revision_by_id.get(row["scene_track_id"]),
                    "source_versions": {
                        row["annotation_id"]: int(row["annotation_version"])
                    },
                    "class_name": row["class_name"],
                    "source_evidence": (
                        {
                            "pointcloud_item_id": pointcloud["item_id"],
                            "pointcloud_contract": 1,
                            "pointcloud_original": pointcloud.get("original"),
                            "pointcloud_original_error": pointcloud.get(
                                "original_error"
                            ),
                            "pointcloud_verified": points is not None,
                            "pointcloud_content_hash": pointcloud.get("content_hash"),
                            "pointcloud_file_path": pointcloud["file_path"],
                            "axis_convention": pointcloud.get("axis_convention")
                            or "iso_8855",
                        }
                        if pointcloud
                        else None
                    ),
                }
            )
        task_cameras = {
            camera["role"]: camera for camera in (task.get("cameras") or [])
        }
        for member in camera_annotations_by_track.get(
            (row["task_id"], row["scene_track_id"]), []
        ):
            camera_data = task_cameras.get(member["sensor_role"])
            if (
                camera_data is None
                or not camera_data.get("width")
                or not camera_data.get("height")
            ):
                skip_counts["projection:camera_unavailable"] += 1
                continue
            camera = _ProjectionCamera(
                calibration=SensorCalibration.model_validate(
                    camera_data["calibration"]
                ),
                width=int(camera_data["width"]),
                height=int(camera_data["height"]),
            )
            projected = project_iso_box(
                {
                    "center": [float(value) for value in row["geometry"]["center"]],
                    "size": [float(value) for value in row["geometry"]["size"]],
                    "rotation": [float(value) for value in row["geometry"]["rotation"]],
                },
                camera=camera,
                axis_convention=(pointcloud or {}).get("axis_convention") or "iso_8855",
            )
            if projected is None:
                skip_counts["projection:not_visible"] += 1
                continue
            geometry = member["geometry"]
            manual_bbox = tuple(float(geometry[key]) for key in ("x", "y", "w", "h"))
            residual = projection_residual(
                manual_bbox,
                projected.pixel_bbox,
                width=camera.width,
                height=camera.height,
            )
            finding = evaluate_projection_residual(
                iou=residual.iou,
                max_edge_residual_px=residual.max_edge_residual_px,
                mean_edge_residual_px=residual.mean_edge_residual_px,
                max_edge_residual_ratio=residual.max_edge_residual_ratio,
                projected_bbox=projected.normalized_bbox,
                manual_bbox=manual_bbox,
                camera_role=member["sensor_role"],
                calibration_digest=camera_data["digest"],
                member_calibration_digest=member["calibration_digest"],
                frame_index=int(row["frame_index"]),
                annotation_ids=(
                    uuid.UUID(row["annotation_id"]),
                    uuid.UUID(member["annotation_id"]),
                ),
                thresholds=thresholds_from_config(config, row["class_name"]),
            )
            if finding is None:
                continue
            pending.append(
                {
                    "run": run,
                    "finding": finding,
                    "config": config,
                    "scene_id": uuid.UUID(row["scene_id"]),
                    "task_id": uuid.UUID(row["task_id"]),
                    "annotation_id": uuid.UUID(row["annotation_id"]),
                    "annotation_version": int(row["annotation_version"]),
                    "scene_track_id": uuid.UUID(row["scene_track_id"]),
                    "track_revision": track_revision_by_id.get(row["scene_track_id"]),
                    "source_versions": {
                        row["annotation_id"]: int(row["annotation_version"]),
                        member["annotation_id"]: int(member["annotation_version"]),
                    },
                    "class_name": row["class_name"],
                    "source_evidence": {
                        "camera_item_id": camera_data["item_id"],
                        "camera_role": member["sensor_role"],
                        "camera_calibration_revision": camera_data["revision"],
                        "camera_calibration_digest": camera_data["digest"],
                    },
                }
            )
        processed += 1
        await _update_run(
            db, run, "progress", progress_pct=min(95, int(processed * 95 / total_units))
        )
        if run.async_job_id:
            await job_lifecycle.update_progress(run.async_job_id, run.progress_pct)
        await db.commit()

    annotations_by_track: dict[str, list[dict]] = defaultdict(list)
    for row in annotations:
        if row["scene_track_id"]:
            annotations_by_track[row["scene_track_id"]].append(row)
    for track in tracks:
        if await _cancel_requested(db, run):
            raise asyncio.CancelledError
        track_id = track["scene_track_id"]
        members: list[TrackMember] = []
        for row in annotations_by_track.get(track_id, []):
            pose = poses.get((row["scene_id"], int(row["frame_index"])))
            members.append(
                TrackMember(
                    annotation_id=uuid.UUID(row["annotation_id"]),
                    frame_index=int(row["frame_index"]),
                    class_name=row["class_name"],
                    track_id=row["track_id"],
                    box=_box(row["geometry"]),
                    annotation_version=int(row["annotation_version"]),
                    ego_translation=(
                        tuple(float(value) for value in pose["ego_translation"])
                        if pose
                        else None
                    ),
                    ego_rotation=(
                        tuple(float(value) for value in pose["ego_rotation"])
                        if pose
                        else None
                    ),
                )
            )
        intervals = [
            TrackInterval(
                start_frame=int(value["start_frame"]),
                end_frame=int(value["end_frame"]),
                version=int(value["version"]),
            )
            for value in intervals_by_track.get(track_id, [])
            if value["end_frame"] is not None
        ]
        findings = evaluate_track(
            scene_track_id=uuid.UUID(track_id),
            authoritative_class=track["class_name"],
            authoritative_track_id=track["track_id"],
            track_revision=int(track["revision"]),
            presence_mode=track["presence_mode"],
            intervals=intervals,
            members=members,
            thresholds=thresholds_from_config(config, track["class_name"]),
        )
        member_by_id = {member.annotation_id: member for member in members}
        for finding in findings:
            primary_id = finding.annotation_ids[0] if finding.annotation_ids else None
            primary = member_by_id.get(primary_id) if primary_id else None
            frame = finding.frame_start
            raw_task_id = task_by_scene_frame.get((track["scene_id"], frame))
            source_versions = {
                str(value.annotation_id): value.annotation_version
                for value in members
                if not finding.annotation_ids
                or value.annotation_id in finding.annotation_ids
            }
            pending.append(
                {
                    "run": run,
                    "finding": finding,
                    "config": config,
                    "scene_id": uuid.UUID(track["scene_id"]),
                    "task_id": uuid.UUID(raw_task_id) if raw_task_id else None,
                    "annotation_id": primary.annotation_id if primary else None,
                    "annotation_version": (
                        primary.annotation_version if primary else None
                    ),
                    "scene_track_id": uuid.UUID(track_id),
                    "track_revision": int(track["revision"]),
                    "source_versions": source_versions,
                    "class_name": track["class_name"],
                }
            )
        processed += 1
        await _update_run(
            db, run, "progress", progress_pct=min(95, int(processed * 95 / total_units))
        )
        if run.async_job_id:
            await job_lifecycle.update_progress(run.async_job_id, run.progress_pct)
        await db.commit()

    await db.commit()
    await originals.publication(db, run, context, observed)
    issue_counts: Counter[str] = Counter()
    for values in pending:
        issue = await _upsert_finding(db, **values)
        if issue is not None:
            issue_counts[values["finding"].code] += 1
    summary = {
        **(run.summary or {}),
        "issue_count": sum(issue_counts.values()),
        "issues_by_code": dict(sorted(issue_counts.items())),
        "skips": dict(sorted(skip_counts.items())),
    }
    originals.access.check_clock(context.authority.proof, context.authority.key)
    return summary


async def claim(
    db: AsyncSession,
    run_id: uuid.UUID,
    celery_task_id: str | None,
    *,
    job_lifecycle: QualityJobLifecycle,
):
    result = await spatial_quality_data(db, "get_point_run", run_id=run_id)
    run = quality_row(PointCloudQualityRun, result["row"]) if result["row"] else None
    if run is None:
        return None
    contract = (run.scope_json or {}).get("original_contract")
    if run.status == "running" and (type(contract) is not int or contract != 1):
        raise originals.ContentError("original_input_unrecorded")
    if run.status != "pending":
        return None
    context = await originals.delegated(db, run)
    job = await db.scalar(
        job_lock_statement(context.job_id, mode="update", populate_existing=True)
    )
    run = await db.scalar(
        select(PointCloudQualityRun)
        .where(PointCloudQualityRun.id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        run is None
        or job is None
        or run.status != "pending"
        or job.status != "pending"
        or (celery_task_id is not None and celery_task_id != context.task_id)
    ):
        await db.rollback()
        return None
    run = await _update_run(
        db, run, "start", started_at=datetime.now(timezone.utc).isoformat()
    )
    await job_lifecycle.mark_running(job.id, celery_task_id=context.task_id)
    await db.commit()
    return run, context


async def terminate(
    db: AsyncSession,
    run_id: uuid.UUID,
    celery_task_id: str | None,
    status: str,
    message: str | None,
    *,
    job_lifecycle: QualityJobLifecycle,
) -> None:
    await db.rollback()
    result = await spatial_quality_data(db, "get_point_run", run_id=run_id)
    located = (
        quality_row(PointCloudQualityRun, result["row"]) if result["row"] else None
    )
    if located is None or located.async_job_id is None:
        return
    job = await db.scalar(
        job_lock_statement(located.async_job_id, mode="update", populate_existing=True)
    )
    run = await db.scalar(
        select(PointCloudQualityRun)
        .where(PointCloudQualityRun.id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        job is None
        or run is None
        or run.status not in {"pending", "running"}
        or (celery_task_id is not None and job.celery_task_id != celery_task_id)
    ):
        return
    if job.status == AsyncJobStatus.CANCELLED.value or (job.payload or {}).get(
        "cancel_requested"
    ):
        status = "cancelled"
    completed_at = datetime.now(timezone.utc).isoformat()
    if status == "cancelled":
        await _update_run(db, run, "cancelled", completed_at=completed_at)
    elif status == "stale":
        await _update_run(
            db,
            run,
            "stale",
            error_message=message or "quality_source_changed",
            completed_at=completed_at,
        )
    else:
        await _update_run(
            db,
            run,
            "failed",
            error_message=message or "point_cloud_quality_failed",
            completed_at=completed_at,
        )
    if status == "cancelled":
        await job_lifecycle.mark_cancelled(
            job.id, result={"reason": "cancelled_by_user"}
        )
    elif status == "stale":
        await job_lifecycle.mark_complete(
            job.id, result={"run_id": str(run_id), "status": status}
        )
    else:
        await job_lifecycle.mark_failed(job.id, error=message or "")
    await job_lifecycle.notify_terminal(job_id=job.id)
    await db.commit()


async def execute_run(
    db: AsyncSession,
    *,
    run_id: uuid.UUID,
    celery_task_id: str | None,
    job_lifecycle: QualityJobLifecycle,
) -> None:
    try:
        claimed = await claim(db, run_id, celery_task_id, job_lifecycle=job_lifecycle)
        if claimed is None:
            return
        run, context = claimed
        project = await get_workspace_project(db, project_id=run.project_id)
        if (
            project is None
            or await current_source_digest(db, run, project)
            != run.source_snapshot_digest
        ):
            raise originals.ContentError("quality_source_changed")
        await db.commit()
        summary = await execute_scan(
            db, run, job_lifecycle=job_lifecycle, context=context
        )
        run = await _update_run(
            db,
            run,
            "completed",
            progress_pct=100,
            summary=summary,
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
        await job_lifecycle.mark_complete(
            context.job_id,
            result={"run_id": str(run.id), "status": "completed", **summary},
        )
        await job_lifecycle.notify_terminal(job_id=context.job_id)
        await originals.revalidate(db, context.authority)
        await db.commit()
    except originals.FencedOut:
        await db.rollback()
    except asyncio.CancelledError:
        await terminate(
            db,
            run_id,
            celery_task_id,
            "cancelled",
            None,
            job_lifecycle=job_lifecycle,
        )
    except originals.ContentError as exc:
        status = (
            "stale"
            if exc.code
            in {
                "quality_source_changed",
                "original_input_unrecorded",
                "content_binding_changed",
            }
            else "failed"
        )
        await terminate(
            db,
            run_id,
            celery_task_id,
            status,
            exc.code,
            job_lifecycle=job_lifecycle,
        )
    except Exception as exc:
        await terminate(
            db,
            run_id,
            celery_task_id,
            "failed",
            f"{type(exc).__name__}: {exc}"[:4000],
            job_lifecycle=job_lifecycle,
        )
