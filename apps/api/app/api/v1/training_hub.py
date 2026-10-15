"""Global training discovery without widening project authority."""

import uuid
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.config import settings
from app.db.enums import MANAGER_PLATFORM_ROLES
from app.db.models.project import Project
from app.db.models.training_dataset import TrainingDatasetVersion
from app.db.models.training_run import TrainingResourceLease, TrainingRun
from app.db.models.user import User
from app.deps import get_current_user, get_db, require_roles, require_scopes
from app.schemas.training_hub import (
    TrainingHubCatalog,
    TrainingProjectPage,
    TrainingQueuePage,
)
from app.schemas.training_run import TrainingRunOut
from app.services import training_runs as service
from app.services.project_aggregates import managed_project_scope_clause

router = APIRouter(
    prefix="/training",
    dependencies=[
        Depends(require_roles(*sorted(MANAGER_PLATFORM_ROLES))),
        Depends(require_scopes("annotations:read", "datasets:read")),
    ],
)


def project_scope(user):
    predicate = managed_project_scope_clause(user, Project.id)
    return [] if predicate is None else [predicate]


@router.get("/catalog", response_model=TrainingHubCatalog)
async def catalog(db: AsyncSession = Depends(get_db)):
    result = service.catalog()
    lease = await db.get(TrainingResourceLease, service.resource_id())
    online = bool(
        lease
        and lease.last_seen_at
        and lease.last_seen_at > service.now() - timedelta(seconds=30)
    )
    for item in result["items"]:
        legacy = {
            "framework": item["framework"],
            "framework_version": item["version"],
            "image_digest": item["image_digest"],
            "base_model": item["base_models"][0],
            "adapter_revision": 1,
        }
        item["compatible"] = (
            bool(
                lease.runtime.get("gpu_uuid") == settings.training_gpu_uuid
                and service.runtime_compatible(legacy, lease.runtime)
            )
            if online
            else None
        )
        for schema in item["parameter_schemas"]:
            identity = {
                key: value
                for key, value in schema.items()
                if key not in {"field_count", "editable_count"}
            }
            schema["compatible"] = (
                bool(
                    lease.runtime.get("gpu_uuid") == settings.training_gpu_uuid
                    and service.runtime_compatible(
                        {
                            **legacy,
                            "adapter_revision": 2,
                            "configuration": {"identity": identity},
                        },
                        lease.runtime,
                    )
                )
                if online
                else None
            )
    return {
        **result,
        "online": online,
        "busy": bool(lease and lease.attempt_id),
        "executor_id": lease.executor_id if lease else None,
        "last_seen_at": lease.last_seen_at if lease else None,
    }


def preparation_reason(project):
    if (project.data_type or "image") != "image":
        return "当前支持图片矩形框检测训练"
    binding = (project.tool_bindings or {}).get("bbox", {})
    classes = binding.get("classes", []) if binding.get("enabled") else []
    names = [item.get("name") if isinstance(item, dict) else item for item in classes]
    if (
        not names
        or any(not isinstance(name, str) or not name.strip() for name in names)
        or len(set(names)) != len(names)
    ):
        return "请先启用矩形框并配置不重复的类别"
    return None


@router.get("/projects", response_model=TrainingProjectPage)
async def projects(
    search: str = Query("", max_length=120),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    predicates = project_scope(user)
    if search.strip():
        escaped = (
            search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        predicates.append(
            or_(
                Project.name.ilike(f"%{escaped}%", escape="\\"),
                Project.display_id.ilike(f"%{escaped}%", escape="\\"),
            )
        )
    total = await db.scalar(
        select(func.count()).select_from(Project).where(*predicates)
    )
    ready_count = (
        select(func.count())
        .where(
            TrainingDatasetVersion.project_id == Project.id,
            TrainingDatasetVersion.status == "ready",
            TrainingDatasetVersion.package_sha256.is_not(None),
        )
        .correlate(Project)
        .scalar_subquery()
    )
    active_count = (
        select(func.count())
        .where(
            TrainingRun.project_id == Project.id, TrainingRun.status.in_(service.ACTIVE)
        )
        .correlate(Project)
        .scalar_subquery()
    )
    rows = (
        await db.execute(
            select(Project, ready_count, active_count)
            .options(defer(Project.annotation_guide), defer(Project.guide_assets))
            .where(*predicates)
            .order_by(Project.updated_at.desc(), Project.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return {
        "total": total or 0,
        "items": [
            {
                "id": project.id,
                "name": project.name,
                "display_id": project.display_id,
                "data_type": project.data_type or "image",
                "status": project.status,
                "can_prepare_dataset": preparation_reason(project) is None,
                "preparation_reason": preparation_reason(project),
                "ready_dataset_count": ready,
                "active_run_count": active,
            }
            for project, ready, active in rows
        ],
    }


@router.get("/runs", response_model=TrainingQueuePage)
async def runs(
    state: Literal["all", "active", "finished", "failed"] = "active",
    project_id: uuid.UUID | None = None,
    framework: Literal["ultralytics", "libreyolo"] | None = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if service.catalog()["enabled"]:
        lease = await service.lock_resource(db)
        await service.observe_lost(db, lease)
        await db.commit()
    predicates = project_scope(user)
    if project_id:
        predicates.append(Project.id == project_id)
    if framework:
        predicates.append(TrainingRun.config["framework"].astext == framework)
    counts = dict(
        (
            await db.execute(
                select(TrainingRun.status, func.count())
                .join(Project, Project.id == TrainingRun.project_id)
                .where(*predicates)
                .group_by(TrainingRun.status)
            )
        ).all()
    )
    summary = {
        "active": sum(counts.get(key, 0) for key in service.ACTIVE),
        "queued": counts.get("queued", 0),
        "running": sum(counts.get(key, 0) for key in service.ACTIVE - {"queued"}),
        **{key: counts.get(key, 0) for key in ("succeeded", "failed", "cancelled")},
    }
    if state == "active":
        predicates.append(TrainingRun.status.in_(service.ACTIVE))
    elif state == "finished":
        predicates.append(TrainingRun.status.in_(service.TERMINAL))
    elif state == "failed":
        predicates.append(TrainingRun.status.in_({"failed", "lost"}))
    statement = (
        select(TrainingRun, Project.name, Project.display_id)
        .join(Project, Project.id == TrainingRun.project_id)
        .where(*predicates)
    )
    total = await db.scalar(select(func.count()).select_from(statement.subquery()))
    rows = (
        await db.execute(
            statement.order_by(TrainingRun.created_at.desc(), TrainingRun.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return {
        "total": total or 0,
        "counts": summary,
        "items": [
            {
                "run": TrainingRunOut.model_validate(run),
                "project_name": name,
                "project_display_id": display_id,
            }
            for run, name, display_id in rows
        ],
    }
