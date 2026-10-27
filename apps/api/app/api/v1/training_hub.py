"""Global training discovery without widening project authority."""

import uuid
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, defer

from app.db.enums import MANAGER_PLATFORM_ROLES
from app.db.models.project import Project
from app.db.models.training_dataset import TrainingDatasetVersion
from app.db.models.training_run import TrainingResourceLease, TrainingRun
from app.deps import get_current_user, get_db, require_roles, require_scopes
from app.schemas.training_hub import (
    TrainingHubCatalog,
    TrainingProjectPage,
    TrainingQueuePage,
)
from app.schemas.training_run import TrainingRunOut
from app.services import training_runs as service
from app.services import training_adapters as adapters
from app.services.project_aggregates import managed_project_scope_clause
from app.services.identity_contracts import IdentityActorFacts
from app.services.workspace_read_relations import project_facts, project_rows

router = APIRouter(
    prefix="/training",
    dependencies=[
        Depends(require_roles(*sorted(MANAGER_PLATFORM_ROLES))),
        Depends(require_scopes("annotations:read", "datasets:read")),
    ],
)


def project_scope(user: IdentityActorFacts, project_id_column):
    predicate = managed_project_scope_clause(user, project_id_column)
    return [] if predicate is None else [predicate]


@router.get("/catalog", response_model=TrainingHubCatalog)
async def catalog(db: AsyncSession = Depends(get_db)):
    result = adapters.catalog()
    lease = await db.get(TrainingResourceLease, adapters.resource_id())
    online = bool(
        lease
        and lease.last_seen_at
        and lease.last_seen_at > service.now() - timedelta(seconds=30)
    )
    service.annotate_readiness(result, lease.runtime if lease else {}, online=online)
    return {
        **result,
        "online": online,
        "busy": bool(lease and lease.attempt_id),
        "executor_id": lease.executor_id if lease else None,
        "last_seen_at": lease.last_seen_at if lease else None,
    }


def preparation_reason(project):
    if (project.data_type or "image") != "image":
        return "当前支持图片矩形框检测和单多边形实例分割训练"
    for tool in ("bbox", "region"):
        binding = (project.tool_bindings or {}).get(tool, {})
        classes = binding.get("classes", []) if binding.get("enabled") else []
        names = [
            item.get("name") if isinstance(item, dict) else item for item in classes
        ]
        if (
            names
            and all(isinstance(name, str) and name.strip() for name in names)
            and len(set(names)) == len(names)
        ):
            return None
    return "请先启用矩形框或 region 多边形，并配置不重复的类别"


@router.get("/projects", response_model=TrainingProjectPage)
async def projects(
    search: str = Query("", max_length=120),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: IdentityActorFacts = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    count_projects = aliased(Project, project_facts(), adapt_on_names=True)
    count_predicates = project_scope(user, count_projects.id)
    search_value = None
    if search.strip():
        escaped = (
            search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        search_value = f"%{escaped}%"
        count_predicates.append(
            or_(
                count_projects.name.ilike(search_value, escape="\\"),
                count_projects.display_id.ilike(search_value, escape="\\"),
            )
        )
    total = await db.scalar(
        select(func.count()).select_from(count_projects).where(*count_predicates)
    )

    project = aliased(Project, project_rows(), adapt_on_names=True)
    project_predicates = project_scope(user, project.id)
    if search_value is not None:
        project_predicates.append(
            or_(
                project.name.ilike(search_value, escape="\\"),
                project.display_id.ilike(search_value, escape="\\"),
            )
        )
    ready_count = (
        select(func.count())
        .where(
            TrainingDatasetVersion.project_id == project.id,
            TrainingDatasetVersion.status == "ready",
            TrainingDatasetVersion.package_sha256.is_not(None),
        )
        .correlate(project)
        .scalar_subquery()
    )
    active_count = (
        select(func.count())
        .where(
            TrainingRun.project_id == project.id,
            TrainingRun.status.in_(service.ACTIVE),
        )
        .correlate(project)
        .scalar_subquery()
    )
    rows = (
        await db.execute(
            select(project, ready_count, active_count)
            .options(defer(project.annotation_guide), defer(project.guide_assets))
            .where(*project_predicates)
            .order_by(project.updated_at.desc(), project.id)
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
    user: IdentityActorFacts = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if adapters.catalog()["enabled"]:
        lease = await service.lock_resource(db)
        await service.observe_lost(db, lease)
        await db.commit()
    counts_projects = project_facts()
    count_predicates = project_scope(user, counts_projects.c.id)
    if project_id:
        count_predicates.append(counts_projects.c.id == project_id)
    if framework:
        count_predicates.append(TrainingRun.config["framework"].astext == framework)
    counts = dict(
        (
            await db.execute(
                select(TrainingRun.status, func.count())
                .join(counts_projects, counts_projects.c.id == TrainingRun.project_id)
                .where(*count_predicates)
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
    run_projects = project_facts()
    run_predicates = project_scope(user, run_projects.c.id)
    if project_id:
        run_predicates.append(run_projects.c.id == project_id)
    if framework:
        run_predicates.append(TrainingRun.config["framework"].astext == framework)
    if state == "active":
        run_predicates.append(TrainingRun.status.in_(service.ACTIVE))
    elif state == "finished":
        run_predicates.append(TrainingRun.status.in_(service.TERMINAL))
    elif state == "failed":
        run_predicates.append(TrainingRun.status.in_({"failed", "lost"}))
    statement = (
        select(TrainingRun, run_projects.c.name, run_projects.c.display_id)
        .join(run_projects, run_projects.c.id == TrainingRun.project_id)
        .where(*run_predicates)
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
