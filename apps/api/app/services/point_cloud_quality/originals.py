"""Frozen PCD ownership, reviewer authority and drained physical inputs."""

import asyncio
from dataclasses import dataclass, field
import hashlib
import threading
import uuid

from fastapi import HTTPException
import numpy as np
from sqlalchemy import select

from app.db.enums import PlatformRole
from app.db.models.async_job import AsyncJob
from app.db.models.dataset import Dataset
from app.db.models.point_cloud_quality import PointCloudQualityRun
from app.db.models.project import Project
from app.db.models.user import User
from app.services import comment_attachment_authority as credentials
from app.services import original_content_access as access
from app.services import original_sources, storage_access
from app.services.async_job import lock_job
from app.services.axis_convention import R_NORM
from app.services.content_snapshot import ContentError
from app.services.mask_qc.service import canonical_digest
from app.services.original_binary_response import close_stage
from app.services import managed_original_reader as reader
from app.services.point_cloud_quality.kernel import parse_pcd_positions
from app.services.project_access import (
    ProjectCapability,
    assert_capability,
    resolve_project_access,
)
from app.services.sample_evaluation_originals import bounded_thread

MAX_BYTES = 256 * 1024 * 1024
AUTHORITY = "point_cloud_quality_authority"
_slots = threading.BoundedSemaphore(2)
_FIELDS = {"schema", "owner_kind", "content_id", "sha256", "size_bytes", "location_id"}


class FencedOut(Exception):
    pass


class Unavailable(ValueError):
    pass


@dataclass(frozen=True)
class Authority:
    actor_id: uuid.UUID
    key_id: uuid.UUID | None
    key: tuple | None = field(repr=False)
    proof: object = field(default=None, repr=False)


@dataclass(frozen=True)
class Context:
    run_id: uuid.UUID
    job_id: uuid.UUID
    project_id: uuid.UUID
    task_id: str
    source_digest: str
    config_digest: str
    scope: dict
    authority: Authority


def fingerprint(key):
    return canonical_digest(
        [key[0], list(key[1]), key[2].isoformat() if key[2] else None]
        if key is not None
        else None
    )


async def actor_key(db, actor_id, key_id, proof=None):
    for attempt in range(4):
        try:
            connection = await db.connection()
            async with connection.begin_nested():
                actor = (
                    await credentials.reauthorize(db, proof)
                    if proof is not None
                    else await credentials._actor(db, actor_id, frozenset(PlatformRole))
                )
                key = await credentials._key(db, actor_id, key_id)
                return actor, key
        except HTTPException as exc:
            mapped = access.credential_error(exc)
            if (
                proof is not None
                or attempt == 3
                or mapped.status_code != 409
                or not isinstance(mapped.detail, dict)
                or mapped.detail.get("code") != "content_busy"
            ):
                raise mapped from None
            await asyncio.sleep(0.05 * (attempt + 1))


async def capture_authority(db, actor_id, request):
    actor = await db.get(User, actor_id)
    if actor is None:
        raise HTTPException(403, {"code": "content_authority_revoked"})
    proof = await access.capture_credentials(db, actor, request)
    if proof is None:
        raise HTTPException(403, {"code": "content_authority_revoked"})
    return Authority(actor_id, proof.key_id, proof.key_proof, proof)


def authorization(authority):
    return {
        "schema": 1,
        "actor_id": str(authority.actor_id),
        "api_key_id": str(authority.key_id) if authority.key_id else None,
        "key_fingerprint": fingerprint(authority.key),
    }


async def _check_authority(
    db, authority, project_id, scope, *, task_id=None, all_tasks=False
):
    actor, key = await actor_key(
        db, authority.actor_id, authority.key_id, authority.proof
    )
    if key != authority.key:
        raise HTTPException(403, {"code": "api_key_authority_revoked"})
    project = await access._row(db, Project, project_id, lock=True)
    if project is None:
        raise HTTPException(404, "Project not found")
    # Take the bounded membership lock before resolving the existing capability.
    from app.db.models.project_member import ProjectMember

    await db.scalar(
        select(ProjectMember)
        .where(
            ProjectMember.project_id == project_id, ProjectMember.user_id == actor.id
        )
        .with_for_update(read=True, nowait=True)
        .execution_options(populate_existing=True)
    )
    resolved = await resolve_project_access(
        db, user=actor, project=project, lock_membership=True
    )
    assert_capability(resolved, ProjectCapability.REVIEW_WRITE.value)
    if scope["scope"] in {"project", "scene_ids"} and not resolved.is_manager:
        raise HTTPException(
            403, "project and scene quality scans require project owner"
        )
    identifiers = (
        scope.get("task_ids", []) if all_tasks else ([str(task_id)] if task_id else [])
    )
    for identifier in sorted(identifiers):
        task = await access.visible_task(db, uuid.UUID(identifier), actor, lock=True)
        if task.project_id != project_id:
            raise HTTPException(404, "Task not found")
    access.check_clock(authority.proof, key)


async def check_authority(
    db, authority, project_id, scope, *, task_id=None, all_tasks=False
):
    async with original_sources._reads(db, True):
        await _check_authority(
            db, authority, project_id, scope, task_id=task_id, all_tasks=all_tasks
        )


async def delegated(db, run):
    job = (
        await db.get(AsyncJob, run.async_job_id, populate_existing=True)
        if run.async_job_id
        else None
    )
    value = (job.payload or {}).get(AUTHORITY) if job else None
    if (
        type(run.scope_json.get("original_contract")) is not int
        or run.scope_json.get("original_contract") != 1
        or job is None
        or job.kind != "point_cloud_quality"
        or job.project_id != run.project_id
        or job.user_id != run.requested_by_id
        or not job.celery_task_id
        or not isinstance(value, dict)
        or set(value) != {"schema", "actor_id", "api_key_id", "key_fingerprint"}
        or type(value["schema"]) is not int
        or value["schema"] != 1
        or value["actor_id"] != str(run.requested_by_id)
    ):
        raise ContentError("original_input_unrecorded")
    try:
        actor_id = uuid.UUID(value["actor_id"])
        key_id = uuid.UUID(value["api_key_id"]) if value["api_key_id"] else None
    except (TypeError, ValueError, AttributeError):
        raise ContentError("original_input_unrecorded") from None
    _actor, key = await actor_key(db, actor_id, key_id)
    if value["key_fingerprint"] != fingerprint(key):
        raise HTTPException(403, {"code": "api_key_authority_revoked"})
    context = Context(
        run.id,
        job.id,
        run.project_id,
        job.celery_task_id,
        run.source_snapshot_digest,
        run.config_digest,
        dict(run.scope_json),
        Authority(actor_id, key_id, key),
    )
    from app.services.point_cloud_quality.config import (
        load_point_cloud_quality_config,
        point_cloud_quality_config_digest,
    )

    if (
        canonical_digest(run.source_snapshot) != context.source_digest
        or point_cloud_quality_config_digest(
            load_point_cloud_quality_config(run.config_snapshot)
        )
        != context.config_digest
    ):
        raise ContentError("quality_source_changed")
    await check_authority(
        db, context.authority, run.project_id, context.scope, all_tasks=True
    )
    return context


async def guard(db, context, *, lock=False):
    job = (
        (await lock_job(db, context.job_id))
        if lock
        else await db.get(AsyncJob, context.job_id, populate_existing=True)
    )
    statement = select(PointCloudQualityRun).where(
        PointCloudQualityRun.id == context.run_id
    )
    if lock:
        statement = statement.with_for_update()
    run = await db.scalar(statement.execution_options(populate_existing=True))
    if (
        job
        and (job.status == "cancelled" or (job.payload or {}).get("cancel_requested"))
    ) or (run and run.status == "cancelled"):
        raise asyncio.CancelledError
    if (
        run is None
        or job is None
        or run.status != "running"
        or job.status != "running"
        or run.async_job_id != context.job_id
        or run.project_id != context.project_id
        or run.requested_by_id != context.authority.actor_id
        or run.source_snapshot_digest != context.source_digest
        or run.config_digest != context.config_digest
        or run.scope_json != context.scope
        or job.project_id != context.project_id
        or job.user_id != context.authority.actor_id
        or job.kind != "point_cloud_quality"
        or job.celery_task_id != context.task_id
        or (job.payload or {}).get(AUTHORITY) != authorization(context.authority)
    ):
        raise FencedOut
    return run, job


def receipt(source):
    if (
        source.media_type != "point_cloud"
        or source.bucket is None
        or source.location_id is None
        or not 0 < source.size_bytes <= MAX_BYTES
    ):
        raise ContentError("pointcloud_original_unsupported")
    return {
        "schema": 1,
        "owner_kind": source.owner_kind,
        "content_id": str(source.content_id),
        "sha256": source.sha256,
        "size_bytes": source.size_bytes,
        "location_id": str(source.location_id),
    }


def validate_receipt(value):
    try:
        valid = (
            isinstance(value, dict)
            and set(value) == _FIELDS
            and type(value["schema"]) is int
            and value["schema"] == 1
            and value["owner_kind"] in {"dataset_asset", "task_original"}
            and str(uuid.UUID(value["content_id"])) == value["content_id"]
            and str(uuid.UUID(value["location_id"])) == value["location_id"]
            and type(value["size_bytes"]) is int
            and 0 < value["size_bytes"] <= MAX_BYTES
            and isinstance(value["sha256"], str)
            and len(value["sha256"]) == 64
            and all(c in "0123456789abcdef" for c in value["sha256"])
        )
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise ContentError("original_input_unrecorded")
    return value


async def axis(db, source, *, lock):
    if source.item_facts is None:
        return "iso_8855"
    dataset = await access._row(db, Dataset, source.item_facts.dataset_id, lock=lock)
    if dataset is None:
        raise ContentError("content_binding_changed")
    return (dataset.metadata_ or {}).get("axis_convention") or "iso_8855"


async def pin(db, record, *, lock=True):
    if record.get("pointcloud") is None:
        return record
    pointcloud = dict(record["pointcloud"])
    pointcloud["axis_convention"] = pointcloud.get("axis_convention") or "iso_8855"
    try:
        source = await original_sources.resolve_task(
            db, uuid.UUID(record["task_id"]), lock=lock
        )
        if (
            source.item_id is None
            or str(source.item_id) != pointcloud["item_id"]
            or source.object_key != pointcloud["file_path"]
        ):
            raise ContentError("content_binding_changed")
        pointcloud.update(original=receipt(source), original_error=None)
    except ContentError as exc:
        if exc.code.endswith("busy") or getattr(
            exc, "storage_operation_unsettled", False
        ):
            raise
        pointcloud.update(original=None, original_error=exc.code)
    return {**record, "pointcloud": pointcloud}


async def freeze(db, context, record):
    await check_authority(
        db,
        context.authority,
        context.project_id,
        context.scope,
        task_id=record["task_id"],
    )
    await guard(db, context)
    pointcloud = record.get("pointcloud")
    if pointcloud is None or pointcloud.get("original") is None:
        await db.commit()
        access.check_clock(context.authority.proof, context.authority.key)
        return None
    expected = validate_receipt(pointcloud["original"])
    source = await original_sources.resolve_task(
        db, uuid.UUID(record["task_id"]), lock=True
    )
    if (
        receipt(source) != expected
        or source.relation.project_id != context.project_id
        or str(source.item_id) != pointcloud["item_id"]
        or source.object_key != pointcloud["file_path"]
        or await axis(db, source, lock=True) != pointcloud["axis_convention"]
    ):
        raise ContentError("content_binding_changed")
    await db.commit()
    access.check_clock(context.authority.proof, context.authority.key)
    return source


async def recheck(db, context, source, convention, *, commit=True):
    await check_authority(
        db,
        context.authority,
        context.project_id,
        context.scope,
        task_id=source.relation.task_id,
    )
    await guard(db, context)
    current = await original_sources.resolve_task(
        db, source.relation.task_id, lock=True
    )
    if current != source or await axis(db, current, lock=True) != convention:
        raise ContentError("content_binding_changed")
    await original_sources.recheck_read_route(db, source)
    if commit:
        await db.commit()
    access.check_clock(context.authority.proof, context.authority.key)


def normalize_points(points, axis_convention):
    convention = axis_convention or "iso_8855"
    matrix = R_NORM.get(convention)
    if matrix is None:
        raise ValueError(f"unsupported axis convention: {convention}")
    return (
        np.asarray(points, dtype=np.float32)
        @ np.asarray(matrix, dtype=np.float32).reshape(3, 3).T
    )


def parse_points(stage, convention):
    stage.file.seek(0)
    payload = stage.file.read(MAX_BYTES + 1)
    if (
        len(payload) != stage.size
        or hashlib.sha256(payload).hexdigest() != stage.sha256
    ):
        raise ContentError("source_changed")
    return normalize_points(parse_pcd_positions(payload), convention)


async def read_points(db, context, source, convention):
    plan = original_sources.read_plan(source)
    permit = await storage_access.acquire(
        db,
        plan.location_id,
        plan.purpose("point_cloud_quality_original"),
        expected_revision=plan.maintenance_revision,
    )
    async with permit:
        stage, failure, unavailable, points = None, None, None, None
        try:
            await recheck(db, context, source, convention)
            try:
                stage = await reader.capture(
                    plan.spec,
                    max_bytes=MAX_BYTES,
                    timeout_seconds=120,
                    admission=_slots,
                    transport_timeout_seconds=30,
                    **(await original_sources.read_options(db, source)),
                )
                points = await bounded_thread(parse_points, stage, convention)
            except (ValueError, OSError) as exc:
                if getattr(exc, "storage_operation_unsettled", False):
                    raise
                unavailable = exc
        except BaseException as exc:
            failure = exc
            raise
        finally:
            if stage is not None:
                try:
                    await close_stage(stage)
                except BaseException as closing:
                    if getattr(failure, "storage_operation_unsettled", False):
                        storage_access.mark_unsettled(closing)
                    raise
        await recheck(db, context, source, convention)
        if unavailable is not None:
            raise Unavailable(
                getattr(unavailable, "code", type(unavailable).__name__)
            ) from unavailable
        return points


async def publication(db, run, context, observed):
    """Retain all final authority/source locks for the caller's issue transaction."""
    from app.db.models.annotation import Annotation
    from app.db.models.dataset import DatasetItem, Scene
    from app.db.models.scene_pose import SceneFramePose
    from app.db.models.scene_track import SceneTrack, SceneTrackInterval
    from app.db.models.task import Task
    from app.db.models.task_dataset_item_link import TaskDatasetItemLink
    from app.services.point_cloud_quality import service
    from app.services.point_cloud_quality.config import (
        load_point_cloud_quality_config,
        point_cloud_quality_config_digest,
    )

    await check_authority(
        db, context.authority, context.project_id, context.scope, all_tasks=True
    )
    current, _job = await guard(db, context, lock=True)
    task_ids = [uuid.UUID(v) for v in context.scope["task_ids"]]
    scene_ids = [uuid.UUID(v) for v in context.scope["scene_ids"]]
    # Existing writers serialize Task -> Scene/Track/Annotation mutations. The
    # canonical resolver below additionally locks Item/Asset/Location/Gate facts.
    statements = [
        select(Task.id).where(Task.id.in_(task_ids)).order_by(Task.id),
        select(Scene.id).where(Scene.id.in_(scene_ids)).order_by(Scene.id),
        select(Annotation.id)
        .where(Annotation.task_id.in_(task_ids))
        .order_by(Annotation.id),
        select(SceneTrack.id)
        .where(SceneTrack.scene_id.in_(scene_ids))
        .order_by(SceneTrack.id),
        select(SceneTrackInterval.id)
        .where(
            SceneTrackInterval.scene_track_id.in_(
                select(SceneTrack.id).where(SceneTrack.scene_id.in_(scene_ids))
            )
        )
        .order_by(SceneTrackInterval.id),
        select(SceneFramePose.id)
        .where(SceneFramePose.scene_id.in_(scene_ids))
        .order_by(SceneFramePose.id),
    ]
    for statement in statements:
        await db.execute(statement.with_for_update(read=True, nowait=True))
    item_ids = set(
        await db.scalars(select(Task.dataset_item_id).where(Task.id.in_(task_ids)))
    )
    item_ids.update(
        await db.scalars(
            select(TaskDatasetItemLink.dataset_item_id).where(
                TaskDatasetItemLink.task_id.in_(task_ids)
            )
        )
    )
    await db.execute(
        select(Dataset.id)
        .where(
            Dataset.id.in_(
                select(DatasetItem.dataset_id).where(
                    DatasetItem.id.in_([v for v in item_ids if v])
                )
            )
        )
        .order_by(Dataset.id)
        .with_for_update(read=True, nowait=True)
    )
    project = await access._row(db, Project, context.project_id, lock=True)
    if (
        canonical_digest(current.source_snapshot) != context.source_digest
        or current.scope_json != context.scope
        or point_cloud_quality_config_digest(
            load_point_cloud_quality_config(current.config_snapshot)
        )
        != context.config_digest
        or point_cloud_quality_config_digest(
            load_point_cloud_quality_config(project.point_cloud_quality_config)
        )
        != context.config_digest
        or await service.current_source_digest(db, current, project)
        != context.source_digest
    ):
        raise ContentError("quality_source_changed")
    for source, convention in observed.values():
        await recheck(db, context, source, convention, commit=False)
    await guard(db, context, lock=True)
    access.check_clock(context.authority.proof, context.authority.key)
