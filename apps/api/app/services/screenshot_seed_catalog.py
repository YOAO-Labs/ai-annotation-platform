"""Resolve the screenshot seed profile to runtime IDs and validate readiness."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
from app.db.models.image_pyramid import ImagePyramidAsset, ImagePyramidGeneration
from app.db.models.prediction import Prediction
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.user import User
from app.schemas._jsonb_types import SensorCalibration
from app.services.ml_backend import MLBackendService
from app.services.screenshot_seed_backends import (
    backend_requirement_issues,
    selected_tracker,
)
from app.services.screenshot_seed_spec import (
    BACKEND_REQUIREMENTS,
    PROJECT_SPECS,
    SEED_MANAGED_BY,
    SEED_REVISION,
    USER_SPECS,
    ProjectSpec,
    RecordingAnchorSpec,
    TaskSpec,
    resolve_project_spec,
    parse_backend_requirements,
)
from app.services.storage import storage_service


SCHEMA_VERSION = 1
LARGE_IMAGE_PROJECT_DISPLAY_ID = "P-LARGE-IMG"
LARGE_IMAGE_DATASET_DISPLAY_ID = "DS-LARGE-IMG"
LARGE_IMAGE_SEED_MANAGED_BY = "large-image-seed"
LARGE_IMAGE_TASK_KEYS = {
    "nasa-cosmic-cliffs": "cosmic_cliffs",
    "nasa-bmng-december": "blue_marble",
    "nasa-cosmic-reionization": "cosmic_reionization",
}


class ScreenshotSeedCatalogError(RuntimeError):
    def __init__(self, issues: list[str]):
        super().__init__("screenshot seed profile is not ready")
        self.issues = issues


def _recording_anchor_payload(anchor: RecordingAnchorSpec) -> dict[str, Any]:
    payload = {
        "schema_version": 1,
        "coordinate_space": "normalized_media",
        "label": anchor.label,
        "bbox": list(anchor.bbox),
        "point": list(anchor.point),
        "additional_points": [list(point) for point in anchor.additional_points],
        "polygon": [list(point) for point in anchor.polygon],
        "polyline": [list(point) for point in anchor.polyline],
        "brush_strokes": [
            [list(point) for point in stroke] for stroke in anchor.brush_strokes
        ],
        "positive_stroke": [list(point) for point in anchor.positive_stroke],
        "negative_stroke": [list(point) for point in anchor.negative_stroke],
        "negative_point": (
            list(anchor.negative_point) if anchor.negative_point is not None else None
        ),
        "provenance": anchor.provenance,
    }
    if anchor.frame_index is not None:
        payload["frame_index"] = anchor.frame_index
    return payload


def _task_payload(task: Task, *, spec: TaskSpec | None = None) -> dict[str, Any]:
    payload = {
        "id": str(task.id),
        "display_id": task.display_id,
        "file_name": task.file_name,
        "file_path": task.file_path,
        "status": task.status,
    }
    if spec is not None and spec.recording_anchors:
        payload["recording_anchors"] = {
            anchor.key: _recording_anchor_payload(anchor)
            for anchor in spec.recording_anchors
        }
    return payload


def _storage_object_digest(file_path: str) -> str:
    response = storage_service.client.get_object(
        Bucket=storage_service.datasets_bucket,
        Key=file_path,
    )
    body = response["Body"]
    digest = hashlib.sha256()
    try:
        for chunk in iter(lambda: body.read(1024 * 1024), b""):
            digest.update(chunk)
    finally:
        body.close()
    return digest.hexdigest()


async def verified_video_seed_digest(db, item) -> str:
    """Validate the canonical seed original and its saved-owner poster."""
    from app.services import video_derivative_originals as originals
    from app.services import video_derivatives
    from app.services.content_snapshot import ContentError

    source = await originals.pin(db, "dataset_item", item.id)
    authority = await originals.delegated(
        db, originals.system_authorization("dataset_item", item.id, source)
    )
    async with originals.media(db, authority, source):
        pass
    checked = await video_derivatives.thumbnail(db, item_id=item.id)
    if checked is None or checked[0] != item.thumbnail_path:
        raise ContentError("video_metadata_unpinned", 409)
    return source.sha256


async def _resolve_backend(
    db: AsyncSession,
    project: Project,
    spec: ProjectSpec,
    issues: list[str],
) -> dict[str, Any] | None:
    if spec.required_backend is None:
        return None

    prefix = project.display_id
    requirement = BACKEND_REQUIREMENTS[spec.required_backend]
    if not project.ai_enabled:
        issues.append(f"{prefix}: ai_enabled must be true")
    if requirement.interactive and not project.ai_interactive_enabled:
        issues.append(f"{prefix}: ai_interactive_enabled must be true")
    if project.ml_backend_pool_id is None:
        issues.append(f"{prefix}: primary ML backend is not bound")

    service = MLBackendService(db)
    backend = await service.get_project_backend(project.id)
    if backend is None:
        issues.append(f"{prefix}: no enabled project ML backend resolves")
        return None
    # v0.23.3 ADR-0050 · 项目主绑定存 pool id; 经 pool.legacy_instance_id 解析后比较 registry id。
    declared_registry: uuid.UUID | None = None
    if project.ml_backend_pool_id is not None:
        from app.db.models.ml_backend_pool import MLBackendServicePool

        pool = await db.get(MLBackendServicePool, project.ml_backend_pool_id)
        declared_registry = pool.legacy_instance_id if pool is not None else None
    if declared_registry != backend.id:
        issues.append(
            f"{prefix}: resolved ML backend is not the declared primary backend"
        )
    capabilities = (backend.health_meta or {}).get("capabilities")
    capabilities = capabilities if isinstance(capabilities, dict) else {}
    for detail in backend_requirement_issues(backend, requirement):
        issues.append(f"{prefix}: ML backend {detail}")
    tracker = selected_tracker(capabilities, requirement)
    if tracker is not None:
        if await service.get_tracker_backend(project.id, tracker) is None:
            issues.append(
                f"{prefix}: tracker capability does not resolve through project binding"
            )

    return {
        "id": str(backend.id),
        "name": backend.name,
        "state": backend.state,
        "is_interactive": backend.is_interactive,
        "requirement": requirement.key,
        "selected_tracker": tracker,
        "capabilities": capabilities,
    }


async def _resolve_dataset_and_tasks(
    db: AsyncSession,
    project: Project,
    logical_key: str,
    spec: ProjectSpec,
    users: dict[str, User],
    issues: list[str],
) -> tuple[Dataset | None, dict[str, Task], dict[str, dict[str, Any]]]:
    prefix = project.display_id
    datasets = list(
        (
            await db.execute(
                select(Dataset)
                .join(ProjectDataset, ProjectDataset.dataset_id == Dataset.id)
                .where(ProjectDataset.project_id == project.id)
            )
        ).scalars()
    )
    expected = [
        dataset for dataset in datasets if dataset.display_id == spec.dataset_display_id
    ]
    if len(datasets) != 1 or len(expected) != 1:
        issues.append(
            f"{prefix}: expected exactly dataset {spec.dataset_display_id} and no other links"
        )
        return None, {}, {}
    dataset = expected[0]
    try:
        spec = resolve_project_spec(spec, dataset.id, dataset.metadata_)
    except ValueError as exc:
        issues.append(f"{prefix}: {exc}")
        return dataset, {}, {}
    marker = (dataset.metadata_ or {}).get("seed")
    if not isinstance(marker, dict) or any(
        (
            marker.get("managed_by") != SEED_MANAGED_BY,
            marker.get("profile") != "screenshots",
            marker.get("logical_key") != logical_key,
            marker.get("revision") != SEED_REVISION,
            not isinstance(marker.get("asset_sha256"), str),
            len(marker.get("asset_sha256", "")) != 64,
        )
    ):
        issues.append(
            f"{spec.dataset_display_id}: screenshot seed ownership marker is stale"
        )
    if (
        spec.axis_convention
        and (dataset.metadata_ or {}).get("axis_convention") != spec.axis_convention
    ):
        issues.append(
            f"{spec.dataset_display_id}: axis_convention must be {spec.axis_convention}"
        )

    dataset_items = list(
        (
            await db.execute(
                select(DatasetItem).where(DatasetItem.dataset_id == dataset.id)
            )
        ).scalars()
    )
    expected_media_paths = set(spec.media_paths) or {
        task_spec.file_path for task_spec in spec.tasks
    }
    actual_media_paths = {item.file_path for item in dataset_items}
    if (
        len(dataset_items) != len(actual_media_paths)
        or actual_media_paths != expected_media_paths
    ):
        issues.append(
            f"{spec.dataset_display_id}: screenshot seed media set is not exact"
        )
    if dataset.file_count != len(dataset_items):
        issues.append(f"{spec.dataset_display_id}: file_count is stale")
    for item in dataset_items:
        if not item.file_path.startswith(spec.storage_prefix) or not item.content_hash:
            issues.append(
                f"{spec.dataset_display_id}: media {item.file_path} has invalid metadata"
            )
            continue
        try:
            object_digest = (
                await verified_video_seed_digest(db, item)
                if item.file_type == "video"
                else await asyncio.to_thread(_storage_object_digest, item.file_path)
            )
        except Exception as exc:  # noqa: BLE001 - report storage failure as an issue
            issues.append(
                f"{spec.dataset_display_id}: media {item.file_path} is unavailable in "
                f"storage ({type(exc).__name__})"
            )
            continue
        if object_digest != item.content_hash:
            issues.append(
                f"{spec.dataset_display_id}: media {item.file_path} digest is stale"
            )
        if spec.data_type == "lidar" and item.file_path.startswith(
            f"{spec.storage_prefix}camera/"
        ):
            try:
                SensorCalibration.model_validate(
                    (item.metadata_ or {}).get("calibration")
                )
            except ValidationError:
                issues.append(
                    f"{spec.dataset_display_id}: camera {item.file_path} has no valid calibration"
                )

    tasks: dict[str, Task] = {}
    payload: dict[str, dict[str, Any]] = {}
    resolved_task_ids: set = set()
    for task_spec in spec.tasks:
        item_rows = list(
            (
                await db.execute(
                    select(DatasetItem).where(
                        DatasetItem.dataset_id == dataset.id,
                        DatasetItem.file_path == task_spec.file_path,
                    )
                )
            ).scalars()
        )
        if len(item_rows) != 1 or not item_rows[0].content_hash:
            issues.append(
                f"{prefix}: task {task_spec.key} must resolve one hashed item at "
                f"{task_spec.file_path}"
            )
            continue
        task_rows = list(
            (
                await db.execute(
                    select(Task).where(
                        Task.project_id == project.id,
                        Task.dataset_item_id == item_rows[0].id,
                    )
                )
            ).scalars()
        )
        if len(task_rows) != 1:
            issues.append(f"{prefix}: task {task_spec.key} must resolve exactly once")
            continue
        task = task_rows[0]
        tasks[task_spec.key] = task
        payload[task_spec.key] = _task_payload(task, spec=task_spec)
        resolved_task_ids.add(task.id)
        if task.status != task_spec.status:
            issues.append(
                f"{prefix}: task {task_spec.key} must have status {task_spec.status}"
            )
        expected_assignee = (
            users.get(task_spec.assignee_key) if task_spec.assignee_key else None
        )
        expected_reviewer = (
            users.get(task_spec.reviewer_key) if task_spec.reviewer_key else None
        )
        if expected_assignee is not None and task.assignee_id != expected_assignee.id:
            issues.append(f"{prefix}: task {task_spec.key} has the wrong assignee")
        if task_spec.assignee_key is None and task.assignee_id is not None:
            issues.append(f"{prefix}: task {task_spec.key} must be unassigned")
        if expected_reviewer is not None and task.reviewer_id != expected_reviewer.id:
            issues.append(f"{prefix}: task {task_spec.key} has the wrong reviewer")
        if task_spec.reviewer_key is None and task.reviewer_id is not None:
            issues.append(f"{prefix}: task {task_spec.key} must have no reviewer")

        prediction_rows = list(
            (
                await db.execute(
                    select(Prediction).where(Prediction.task_id == task.id)
                )
            ).scalars()
        )
        expected_prediction = (
            logical_key == "image_demo" and task_spec.key == "predicted"
        )
        if len(prediction_rows) != int(expected_prediction):
            issues.append(
                f"{prefix}: task {task_spec.key} prediction count must be "
                f"{int(expected_prediction)}"
            )
        elif expected_prediction and prediction_rows[0].model_version != (
            f"screenshot-seed:{SEED_REVISION}"
        ):
            issues.append(f"{prefix}: task predicted has a stale seed prediction")

        annotation_count = (
            await db.scalar(
                select(func.count())
                .select_from(Annotation)
                .where(
                    Annotation.task_id == task.id,
                    Annotation.is_active.is_(True),
                    Annotation.was_cancelled.is_(False),
                )
            )
            or 0
        )
        if annotation_count != int(task_spec.annotation):
            issues.append(
                f"{prefix}: task {task_spec.key} annotation count must be "
                f"{int(task_spec.annotation)}"
            )
        if task.total_predictions != len(prediction_rows):
            issues.append(f"{prefix}: task {task_spec.key} prediction counter is stale")
        if task.total_annotations != annotation_count:
            issues.append(f"{prefix}: task {task_spec.key} annotation counter is stale")

    project_task_ids = set(
        (
            await db.execute(select(Task.id).where(Task.project_id == project.id))
        ).scalars()
    )
    if project_task_ids != resolved_task_ids:
        issues.append(f"{prefix}: project contains unexpected or missing tasks")
    return dataset, tasks, payload


async def _resolve_batches(
    db: AsyncSession,
    project: Project,
    spec: ProjectSpec,
    tasks: dict[str, Task],
    issues: list[str],
) -> dict[str, dict[str, Any]]:
    if not spec.batches:
        return {}
    rows = list(
        (
            await db.execute(
                select(TaskBatch).where(TaskBatch.project_id == project.id)
            )
        ).scalars()
    )
    expected_ids = {batch_spec.display_id for batch_spec in spec.batches}
    if {batch.display_id for batch in rows} != expected_ids or len(rows) != len(
        spec.batches
    ):
        issues.append(f"{project.display_id}: screenshot seed batch set is not exact")
    by_display_id = {batch.display_id: batch for batch in rows}
    payload: dict[str, dict[str, Any]] = {}
    for batch_spec in spec.batches:
        batch = by_display_id.get(batch_spec.display_id)
        if batch is None:
            issues.append(
                f"{project.display_id}: batch {batch_spec.display_id} is missing"
            )
            continue
        if batch.status != batch_spec.status:
            issues.append(
                f"{project.display_id}: batch {batch_spec.display_id} must have status "
                f"{batch_spec.status}"
            )
        expected_task_ids = {
            tasks[task_spec.key].id
            for task_spec in spec.tasks
            if task_spec.batch_key == batch_spec.key and task_spec.key in tasks
        }
        actual_task_ids = set(
            (
                await db.execute(select(Task.id).where(Task.batch_id == batch.id))
            ).scalars()
        )
        if actual_task_ids != expected_task_ids:
            issues.append(
                f"{project.display_id}: batch {batch_spec.display_id} task membership is stale"
            )
        payload[batch_spec.key] = {
            "id": str(batch.id),
            "display_id": batch.display_id,
            "status": batch.status,
        }
    return payload


async def _resolve_project(
    db: AsyncSession,
    logical_key: str,
    spec: ProjectSpec,
    users: dict[str, User],
    issues: list[str],
    backend_requirements: frozenset[str] | None = None,
) -> dict[str, Any] | None:
    projects = list(
        (
            await db.execute(
                select(Project).where(Project.display_id == spec.display_id)
            )
        ).scalars()
    )
    if len(projects) != 1:
        issues.append(f"{spec.display_id}: project is missing or duplicated")
        return None
    project = projects[0]
    if project.data_type != spec.data_type:
        issues.append(
            f"{spec.display_id}: data_type must be {spec.data_type}, got {project.data_type}"
        )

    dataset, tasks, task_payload = await _resolve_dataset_and_tasks(
        db, project, logical_key, spec, users, issues
    )
    batches = await _resolve_batches(db, project, spec, tasks, issues)

    if spec.require_members:
        members = list(
            (
                await db.execute(
                    select(ProjectMember).where(ProjectMember.project_id == project.id)
                )
            ).scalars()
        )
        for role_key, member_role in (
            ("annotator", "annotator"),
            ("reviewer", "reviewer"),
        ):
            user = users.get(role_key)
            if user is not None and not any(
                member.user_id == user.id and member.role == member_role
                for member in members
            ):
                issues.append(
                    f"{spec.display_id}: {role_key} project membership is missing"
                )

    backend = (
        await _resolve_backend(db, project, spec, issues)
        if backend_requirements is None or spec.required_backend in backend_requirements
        else None
    )
    dataset_payload = {}
    if dataset is not None:
        dataset_payload[dataset.display_id] = {
            "id": str(dataset.id),
            "name": dataset.name,
            "file_count": dataset.file_count,
        }
    return {
        "id": str(project.id),
        "display_id": project.display_id,
        "name": project.name,
        "data_type": project.data_type,
        "datasets": dataset_payload,
        "tasks": task_payload,
        "batches": batches,
        "ml_backend": backend,
    }


async def _large_pyramid_ready(db, task, item, generation) -> bool:
    """Validate persisted ownership without treating metadata as a live I/O test."""
    from fastapi import HTTPException

    from app.db.models.image_pyramid import ImagePyramidOutput
    from app.services import image_pyramid as pyramids
    from app.services import image_pyramid_objects as objects
    from app.services import image_pyramid_storage as physical, storage_access
    from app.services.content_snapshot import ContentError

    if (
        generation is None
        or generation.status != "ready"
        or generation.width != item.width
        or generation.height != item.height
        or generation.output_id is None
        or generation.auth_ref is None
        or generation.normalization_version
        != settings.image_pyramid_normalization_version
    ):
        return False
    try:
        owner = await pyramids.owner_for_task(db, task)
        if not pyramids.source_matches_generation(owner, generation):
            return False
        output = await db.get(
            ImagePyramidOutput, generation.output_id, populate_existing=True
        )
        if (
            output is None
            or output.state != "ready"
            or output.asset_id != generation.asset_id
            or output.generation != generation.generation
        ):
            return False
        saved = physical.resolve(output)
        objects.validate_inventory(output, generation, output.inventory)
        await storage_access.check(db, uuid.UUID(saved.ref["location_id"]))
    except (
        ContentError,
        HTTPException,
        pyramids.ImagePyramidError,
        physical.ImagePyramidStorageError,
        KeyError,
        TypeError,
        ValueError,
    ):
        return False
    return True


async def _resolve_optional_large_image_project(
    db: AsyncSession, issues: list[str]
) -> dict[str, Any] | None:
    """Expose the separately managed large-image seed when it is fully ready."""
    projects = list(
        (
            await db.execute(
                select(Project).where(
                    Project.display_id == LARGE_IMAGE_PROJECT_DISPLAY_ID
                )
            )
        ).scalars()
    )
    datasets = list(
        (
            await db.execute(
                select(Dataset).where(
                    Dataset.display_id == LARGE_IMAGE_DATASET_DISPLAY_ID
                )
            )
        ).scalars()
    )
    if not projects and not datasets:
        return None
    if len(projects) != 1 or len(datasets) != 1:
        issues.append(
            "large_image_demo: expected exactly one P-LARGE-IMG / DS-LARGE-IMG pair"
        )
        return None

    project, dataset = projects[0], datasets[0]
    prefix = LARGE_IMAGE_PROJECT_DISPLAY_ID
    links = list(
        (
            await db.execute(
                select(ProjectDataset).where(
                    (ProjectDataset.project_id == project.id)
                    | (ProjectDataset.dataset_id == dataset.id)
                )
            )
        ).scalars()
    )
    if (
        len(links) != 1
        or links[0].project_id != project.id
        or links[0].dataset_id != dataset.id
    ):
        issues.append(f"{prefix}: large-image project/dataset link is not exclusive")
    marker = (dataset.metadata_ or {}).get("seed")
    if not isinstance(marker, dict) or marker.get("managed_by") != (
        LARGE_IMAGE_SEED_MANAGED_BY
    ):
        issues.append(f"{LARGE_IMAGE_DATASET_DISPLAY_ID}: ownership marker is stale")
    if project.data_type != "image" or dataset.data_type != "image":
        issues.append(f"{prefix}: project and dataset must use image data")

    items = list(
        (
            await db.execute(
                select(DatasetItem).where(DatasetItem.dataset_id == dataset.id)
            )
        ).scalars()
    )
    if dataset.file_count != len(items):
        issues.append(f"{LARGE_IMAGE_DATASET_DISPLAY_ID}: file_count is stale")

    task_payload: dict[str, dict[str, Any]] = {}
    resolved_task_ids: set[uuid.UUID] = set()
    for item in items:
        item_marker = (item.metadata_ or {}).get("seed")
        fixture_id = (
            item_marker.get("fixture_id") if isinstance(item_marker, dict) else None
        )
        logical_key = LARGE_IMAGE_TASK_KEYS.get(str(fixture_id))
        if (
            not isinstance(item_marker, dict)
            or item_marker.get("managed_by") != LARGE_IMAGE_SEED_MANAGED_BY
            or logical_key is None
            or not item.content_hash
            or not item.width
            or not item.height
        ):
            issues.append(f"{prefix}: item {item.file_name} has invalid seed metadata")
            continue
        task_rows = list(
            (
                await db.execute(
                    select(Task).where(
                        Task.project_id == project.id,
                        Task.dataset_item_id == item.id,
                    )
                )
            ).scalars()
        )
        if len(task_rows) != 1:
            issues.append(
                f"{prefix}: fixture {fixture_id} must resolve exactly one task"
            )
            continue
        task = task_rows[0]
        resolved_task_ids.add(task.id)

        generation = await db.scalar(
            select(ImagePyramidGeneration)
            .join(
                ImagePyramidAsset,
                ImagePyramidAsset.id == ImagePyramidGeneration.asset_id,
            )
            .where(
                ImagePyramidAsset.dataset_item_id == item.id,
                ImagePyramidAsset.profile_version
                == settings.image_pyramid_profile_version,
                ImagePyramidAsset.active_generation
                == ImagePyramidGeneration.generation,
            )
        )
        if not await _large_pyramid_ready(db, task, item, generation):
            issues.append(f"{prefix}: fixture {fixture_id} pyramid is not ready")
            continue
        task_payload[logical_key] = _task_payload(task)

    if "cosmic_cliffs" not in task_payload:
        issues.append(f"{prefix}: cosmic_cliffs ready fixture is required")
    actual_task_ids = set(
        (
            await db.execute(select(Task.id).where(Task.project_id == project.id))
        ).scalars()
    )
    if actual_task_ids != resolved_task_ids:
        issues.append(f"{prefix}: contains unexpected or missing tasks")

    return {
        "id": str(project.id),
        "display_id": project.display_id,
        "name": project.name,
        "data_type": project.data_type,
        "datasets": {
            dataset.display_id: {
                "id": str(dataset.id),
                "name": dataset.name,
                "file_count": dataset.file_count,
            }
        },
        "tasks": task_payload,
        "batches": {},
        "ml_backend": None,
    }


async def build_screenshot_seed_catalog(
    db: AsyncSession, *, backend_requirements: str | None = None
) -> dict[str, Any]:
    required = parse_backend_requirements(backend_requirements)
    issues: list[str] = []
    users: dict[str, User] = {}
    user_payload: dict[str, dict[str, str]] = {}
    for logical_key, (email, role) in USER_SPECS.items():
        rows = list(
            (await db.execute(select(User).where(User.email == email))).scalars()
        )
        if len(rows) != 1:
            issues.append(f"user {logical_key} ({email}) is missing or duplicated")
            continue
        user = rows[0]
        if user.role != role:
            issues.append(f"user {email} must have role {role}, got {user.role}")
        if not user.is_active:
            issues.append(f"user {email} must be active")
        users[logical_key] = user
        user_payload[logical_key] = {
            "id": str(user.id),
            "email": user.email,
            "role": user.role,
        }

    project_payload: dict[str, Any] = {}
    for logical_key, spec in PROJECT_SPECS.items():
        project = await _resolve_project(db, logical_key, spec, users, issues, required)
        if project is not None:
            project_payload[logical_key] = project

    large_image_project = await _resolve_optional_large_image_project(db, issues)
    if large_image_project is not None:
        project_payload["large_image_demo"] = large_image_project

    if issues:
        raise ScreenshotSeedCatalogError(issues)
    return {
        "schema_version": SCHEMA_VERSION,
        "seed_revision": SEED_REVISION,
        "backend_requirements": sorted(required),
        "users": user_payload,
        "projects": project_payload,
    }
