from aap_task_contract import normalize_task
import logging
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from app.application.preannotation_contract import (
    BatchPredictionDispatcher,
    BatchPredictionRequest,
)
from app.application.project_workspace import (
    transfer_project_owner as transfer_project_owner_workflow,
)
from app.application.project_membership import (
    change_role as change_project_member_role,
    remove_member as remove_project_member,
)
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_, and_
from app.deps import (
    get_db,
    get_current_user,
    require_roles,
    require_project_visible,
    require_project_owner,
    require_project_capability,
    get_batch_prediction_dispatcher,
)
from app.db.enums import (
    PROJECT_ROLES,
    TaskStatus,
    UserRole,
    WORKBENCH_AI_EDITABLE_TASK_STATUSES,
)
from app.services.project_access import (
    ProjectCapability,
    resolve_project_access,
)
from app.services.project_membership import (
    add_member as add_project_member,
    preview_role_change as preview_project_member_role_change,
)
from app.db.models.user import User
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.project_pipeline import ProjectPipeline
from app.db.models.dataset import DatasetItem, ProjectDataset
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.async_job import AsyncJob
from app.schemas.project import (
    ProjectOut,
    ProjectPage,
    ProjectCreate,
    ProjectUpdate,
    ProjectStats,
    ProjectClassUsageOut,
    ProjectCleanupOrphansRequest,
    ProjectCleanupOrphansOut,
    ProjectMemberOut,
    ProjectMemberCreate,
    ProjectMemberRolePreviewRequest,
    ProjectMemberRolePreviewOut,
    ProjectMemberRoleChangeRequest,
    ProjectAccessOut,
    ProjectTransferRequest,
    ProjectReadinessSummary,
    MentionCandidateOut,
)
from app.schemas.project_pipeline import ProjectPipelineApplyRequest, ProjectPipelineOut
import app.schemas.preannotate as preannotate_schemas
from app.schemas.export import (
    ExportRequestBody,
    LidarExportPreflightRequest,
    LidarExportPreflightResponse,
)
from app.config import settings
from app.services.pipeline_validation import (
    check_capability_violations,
    check_parent_geometry_roi,
    resolve_preannotate_queue,
)
from app.services.capability_registry import INPUT_BBOX_PROMPT, INPUT_CROP
from app.services.batch_permissions import allows_bulk_preannotation
from app.services.pipeline_template import (
    assert_pipeline_visible,
)
from app.application.project_deletion import (
    delete_project as delete_project_transaction,
)
from app.application.project_task_cleanup import (
    cleanup_annotation_orphans as cleanup_project_annotation_orphans,
    delete_source_less_tasks as cleanup_source_less_tasks,
)
from app.application.project_class_rename import rename_project_class
from app.application.project_pipeline import apply_saved_pipeline
from app.application.project_workspace_configuration import (
    WorkspaceConfigurationWorkflow,
)

router = APIRouter()
logger = logging.getLogger("app.api.projects")

_MANAGERS = (UserRole.SUPER_ADMIN, UserRole.PROJECT_ADMIN)
_STATS_SERIES_POINTS = 12
_STATS_SERIES_STEP = timedelta(days=7)


def _visible_project_filter(user: User):
    """构造按当前用户可见性过滤项目的子查询条件 (Project 主查询)。

    统一口径：super_admin 全见；项目负责人见自有项目；其余账号见其成员项目。
    平台 project_admin 若只是他人项目成员，按成员可见性处理（不获得管理权）。
    """
    if user.role == UserRole.SUPER_ADMIN:
        return None  # 不过滤
    # A legitimate owner must also hold an administrative platform role; an
    # anomalous non-administrative owner is not implicitly visible.
    if user.role == UserRole.PROJECT_ADMIN:
        return or_(
            Project.owner_id == user.id,
            Project.id.in_(
                select(ProjectMember.project_id).where(ProjectMember.user_id == user.id)
            ),
        )
    return Project.id.in_(
        select(ProjectMember.project_id).where(ProjectMember.user_id == user.id)
    )


async def _my_project_roles(
    db: AsyncSession, user: User, projects: list[Project]
) -> dict[uuid.UUID, str]:
    """Membership role of the requesting account per project (managers: absent)."""

    ids = [project.id for project in projects]
    if not ids or user.role == UserRole.SUPER_ADMIN:
        return {}
    rows = await db.execute(
        select(ProjectMember.project_id, ProjectMember.role).where(
            ProjectMember.user_id == user.id,
            ProjectMember.project_id.in_(ids),
        )
    )
    return {project_id: role for project_id, role in rows.all()}


def _attach_my_project_role(items: list[dict], roles: dict[uuid.UUID, str]) -> None:
    for item in items:
        item["my_project_role"] = roles.get(item["id"])


def _stats_bucket_ends(now: datetime) -> list[datetime]:
    return [
        now - _STATS_SERIES_STEP * (_STATS_SERIES_POINTS - 1 - i)
        for i in range(_STATS_SERIES_POINTS)
    ]


async def _build_project_stats_series(
    db: AsyncSession,
    visible_project_ids: list[uuid.UUID],
) -> dict[str, list[int] | list[float]]:
    from app.db.models.annotation import Annotation
    from app.db.models.task import Task

    bucket_ends = _stats_bucket_ends(datetime.now(timezone.utc))

    # v0.12.1 · 12 桶 × 标量原本逐桶 sequential（48 次 round-trip）；改用
    # count(*) FILTER (WHERE bucket) 把每个指标的 12 桶折叠进一条聚合：Task 3 指标
    # 合 1 条、Annotation 2 指标合 1 条 = 共 2 次往返。AsyncSession 不允许同一
    # session 并发查询，故走「减查询数」而非 asyncio.gather。
    task_cols = (
        [func.count().filter(Task.created_at <= be) for be in bucket_ends]
        + [
            func.count().filter(
                and_(
                    Task.status == "completed",
                    or_(
                        Task.reviewed_at <= be,
                        and_(Task.reviewed_at.is_(None), Task.updated_at <= be),
                    ),
                )
            )
            for be in bucket_ends
        ]
        + [
            func.count().filter(
                and_(
                    Task.submitted_at.isnot(None),
                    Task.submitted_at <= be,
                    or_(Task.reviewed_at.is_(None), Task.reviewed_at > be),
                )
            )
            for be in bucket_ends
        ]
    )
    task_row = (
        await db.execute(
            select(*task_cols)
            .select_from(Task)
            .where(Task.project_id.in_(visible_project_ids))
        )
    ).one()
    n = _STATS_SERIES_POINTS
    total_data_series = [int(v or 0) for v in task_row[0:n]]
    completed_series = [int(v or 0) for v in task_row[n : 2 * n]]
    pending_review_series = [int(v or 0) for v in task_row[2 * n : 3 * n]]

    ann_base = (
        Annotation.is_active.is_(True),
        Annotation.was_cancelled.is_(False),
        Annotation.project_id.in_(visible_project_ids),
    )
    ann_cols = [
        func.count().filter(Annotation.created_at <= be) for be in bucket_ends
    ] + [
        func.count().filter(
            and_(
                Annotation.created_at <= be,
                Annotation.parent_prediction_id.isnot(None),
            )
        )
        for be in bucket_ends
    ]
    ann_row = (
        await db.execute(select(*ann_cols).select_from(Annotation).where(*ann_base))
    ).one()
    total_ann_series = [int(v or 0) for v in ann_row[0:n]]
    ai_ann_series = [int(v or 0) for v in ann_row[n : 2 * n]]
    ai_rate_series = [
        round(ai / total * 100, 1) if total else 0.0
        for ai, total in zip(ai_ann_series, total_ann_series)
    ]

    return {
        "total_data_series": total_data_series,
        "completed_series": completed_series,
        "pending_review_series": pending_review_series,
        "ai_rate_series": ai_rate_series,
    }


async def _serialize_project(
    db: AsyncSession,
    project: Project,
    *,
    ai_completed_lookup: dict[uuid.UUID, int] | None = None,
    batch_summary_lookup: dict[uuid.UUID, dict] | None = None,
) -> dict:
    """补齐 owner_name + member_count + ai_completed_tasks，转 dict 以喂给 ProjectOut。

    v0.7.0：in_progress_tasks 已是持久化列（alembic 0028）；ai_completed_tasks 由调用方
    通过 ai_completed_lookup 批量提供（list_projects 路径）或 fallback 单独查询。
    """
    owner_name = None
    owner_avatar_ref = None
    if project.owner_id:
        owner_row = await db.execute(
            select(User.name, User.avatar_ref).where(User.id == project.owner_id)
        )
        owner_row = owner_row.first()
        if owner_row is not None:
            owner_name, owner_avatar_ref = owner_row
    count_row = await db.execute(
        select(func.count())
        .select_from(ProjectMember)
        .where(ProjectMember.project_id == project.id)
    )
    member_count = count_row.scalar() or 0

    if ai_completed_lookup is not None:
        ai_completed = int(ai_completed_lookup.get(project.id, 0))
    else:
        from app.db.models.annotation import Annotation

        ai_row = await db.execute(
            select(func.count(func.distinct(Annotation.task_id))).where(
                Annotation.project_id == project.id,
                Annotation.parent_prediction_id.is_not(None),
                Annotation.is_active.is_(True),
            )
        )
        ai_completed = int(ai_row.scalar() or 0)

    # v0.10.25 · 直接读物化列 batch_summary (迁移 0079 + _sync_project_counters 维护).
    if batch_summary_lookup is not None:
        batch_summary = batch_summary_lookup.get(
            project.id, {"total": 0, "assigned": 0, "in_review": 0}
        )
    else:
        batch_summary = project.batch_summary or {
            "total": 0,
            "assigned": 0,
            "in_review": 0,
        }

    data = {c.name: getattr(project, c.name) for c in project.__table__.columns}
    # v0.23.3 ADR-0050 · 公共 schema ProjectOut 仍暴露 ml_backend_id (registry id, 兼容前端);
    # ORM 存的是 ml_backend_pool_id (pool id)。这里解析 pool 的 legacy instance 回 registry id。
    # 前端完整池管理留给 v0.23.4; 本版本前端看到的仍是 singleton pool 的 legacy 实例。
    ml_backend_id_for_response: uuid.UUID | None = None
    if project.ml_backend_pool_id is not None:
        from app.db.models.ml_backend_pool import MLBackendServicePool

        pool = await db.get(MLBackendServicePool, project.ml_backend_pool_id)
        ml_backend_id_for_response = (
            pool.legacy_instance_id if pool is not None else None
        )
    data["ml_backend_id"] = ml_backend_id_for_response
    data["owner_name"] = owner_name
    data["owner_avatar_ref"] = owner_avatar_ref
    data["member_count"] = member_count
    data["ai_completed_tasks"] = ai_completed
    data["batch_summary"] = batch_summary
    return data


def _project_filter_datetime(value: str | None, *, end_of_day: bool = False):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail="创建日期须为 ISO 日期或时间"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if end_of_day and len(value) == 10:
        parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    return parsed


def _project_list_query(
    user: User,
    *,
    status: str | None = None,
    search: str | None = None,
    type_key: list[str] | None = None,
    data_type: list[str] | None = None,
    member_id: uuid.UUID | None = None,
    project_role: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
):
    q = select(Project)
    cond = _visible_project_filter(user)
    if cond is not None:
        q = q.where(cond)
    if status:
        q = q.where(Project.status == status)
    if search:
        q = q.where(Project.name.ilike(f"%{search}%"))
    if type_key:
        q = q.where(Project.type_key.in_(type_key))
    if data_type:
        q = q.where(Project.data_type.in_(data_type))
    if member_id is not None:
        q = q.where(
            Project.id.in_(
                select(ProjectMember.project_id).where(
                    ProjectMember.user_id == member_id
                )
            )
        )
    if project_role:
        if project_role not in PROJECT_ROLES:
            raise HTTPException(status_code=422, detail="非法项目职责")
        q = q.where(
            Project.id.in_(
                select(ProjectMember.project_id).where(
                    ProjectMember.user_id == user.id,
                    ProjectMember.role == project_role,
                )
            )
        )
    start = _project_filter_datetime(created_from)
    end = _project_filter_datetime(created_to, end_of_day=True)
    if start is not None and end is not None and start > end:
        raise HTTPException(status_code=422, detail="创建开始日期不能晚于结束日期")
    if start is not None:
        q = q.where(Project.created_at >= start)
    if end is not None:
        q = q.where(Project.created_at <= end)
    return q.order_by(Project.created_at.desc(), Project.id.desc())


async def _serialize_project_list(
    db: AsyncSession, projects: list[Project]
) -> list[dict]:
    # v0.7.0：批量预查 ai_completed_tasks 避免 N+1 — 单 GROUP BY 查询
    from app.db.models.annotation import Annotation

    project_ids = [p.id for p in projects]
    ai_lookup: dict[uuid.UUID, int] = {}
    # v0.10.25 · batch_summary 改读物化列 (迁移 0079), 直接从已加载的 project 取.
    bs_lookup: dict[uuid.UUID, dict] = {
        p.id: (p.batch_summary or {"total": 0, "assigned": 0, "in_review": 0})
        for p in projects
    }
    if project_ids:
        ai_rows = (
            await db.execute(
                select(
                    Annotation.project_id,
                    func.count(func.distinct(Annotation.task_id)).label("cnt"),
                )
                .where(
                    Annotation.project_id.in_(project_ids),
                    Annotation.parent_prediction_id.is_not(None),
                    Annotation.is_active.is_(True),
                )
                .group_by(Annotation.project_id)
            )
        ).all()
        ai_lookup = {row[0]: int(row[1]) for row in ai_rows}

    return [
        await _serialize_project(
            db,
            p,
            ai_completed_lookup=ai_lookup,
            batch_summary_lookup=bs_lookup,
        )
        for p in projects
    ]


@router.get("", response_model=list[ProjectOut])
async def list_projects(
    status: str | None = None,
    search: str | None = None,
    # v0.7.2 · 高级筛选维度（FilterDrawer 对接）
    type_key: list[str] | None = Query(None),
    # v0.10.28 · 媒体维度筛选 (image / video / lidar)
    data_type: list[str] | None = Query(None),
    member_id: uuid.UUID | None = None,
    project_role: str | None = None,
    created_from: str | None = None,  # ISO date "2026-01-01"
    created_to: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = _project_list_query(
        user,
        status=status,
        search=search,
        type_key=type_key,
        data_type=data_type,
        member_id=member_id,
        project_role=project_role,
        created_from=created_from,
        created_to=created_to,
    )
    result = await db.execute(q)
    projects = list(result.scalars().all())
    items = await _serialize_project_list(db, projects)
    _attach_my_project_role(items, await _my_project_roles(db, user, projects))
    return items


@router.get("/query", response_model=ProjectPage)
async def query_projects(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: str | None = None,
    search: str | None = None,
    type_key: list[str] | None = Query(None),
    data_type: list[str] | None = Query(None),
    member_id: uuid.UUID | None = None,
    project_role: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = _project_list_query(
        user,
        status=status,
        search=search,
        type_key=type_key,
        data_type=data_type,
        member_id=member_id,
        project_role=project_role,
        created_from=created_from,
        created_to=created_to,
    )
    total = int(
        await db.scalar(select(func.count()).select_from(q.order_by(None).subquery()))
        or 0
    )
    result = await db.execute(q.offset((page - 1) * page_size).limit(page_size))
    projects = list(result.scalars().all())
    items = await _serialize_project_list(db, projects)
    _attach_my_project_role(items, await _my_project_roles(db, user, projects))
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size,
    }


@router.get("/stats", response_model=ProjectStats)
async def get_stats(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.db.models.annotation import Annotation

    q = select(Project)
    cond = _visible_project_filter(user)
    if cond is not None:
        q = q.where(cond)
    result = await db.execute(q)
    projects = result.scalars().all()
    total = sum(p.total_tasks for p in projects)
    completed = sum(p.completed_tasks for p in projects)
    review = sum(p.review_tasks for p in projects)

    visible_ids = [p.id for p in projects]

    if not visible_ids:
        return ProjectStats(
            total_data=0,
            completed=0,
            ai_rate=0.0,
            pending_review=0,
            total_annotations=0,
            ai_derived_annotations=0,
            total_data_series=[0] * _STATS_SERIES_POINTS,
            completed_series=[0] * _STATS_SERIES_POINTS,
            ai_rate_series=[0.0] * _STATS_SERIES_POINTS,
            pending_review_series=[0] * _STATS_SERIES_POINTS,
        )

    total_ann_result = await db.execute(
        select(func.count())
        .select_from(Annotation)
        .where(
            Annotation.is_active.is_(True),
            Annotation.was_cancelled.is_(False),
            Annotation.project_id.in_(visible_ids),
        )
    )
    total_annotations = total_ann_result.scalar() or 0

    ai_ann_result = await db.execute(
        select(func.count())
        .select_from(Annotation)
        .where(
            Annotation.is_active.is_(True),
            Annotation.was_cancelled.is_(False),
            Annotation.parent_prediction_id.isnot(None),
            Annotation.project_id.in_(visible_ids),
        )
    )
    ai_derived_annotations = ai_ann_result.scalar() or 0

    ai_rate = (
        round(ai_derived_annotations / total_annotations * 100, 1)
        if total_annotations
        else 0.0
    )
    series = await _build_project_stats_series(db, visible_ids)

    return ProjectStats(
        total_data=total,
        completed=completed,
        ai_rate=ai_rate,
        pending_review=review,
        total_annotations=total_annotations,
        ai_derived_annotations=ai_derived_annotations,
        **series,
    )


@router.post("", response_model=ProjectOut)
async def create_project(
    data: ProjectCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(*_MANAGERS)),
):
    project, template = await WorkspaceConfigurationWorkflow(db).create_project(
        data.model_dump(exclude_unset=True), actor_id=current_user.id
    )
    await db.commit()
    await db.refresh(project)
    if template is not None:
        await db.refresh(template)
    return await _serialize_project(db, project)


@router.get("/{project_id}", response_model=ProjectOut)
async def get_project(
    project: Project = Depends(require_project_visible),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    out = await _serialize_project(db, project)
    roles = await _my_project_roles(db, user, [project])
    out["my_project_role"] = roles.get(project.id)
    return out


@router.get("/{project_id}/readiness", response_model=ProjectReadinessSummary)
async def get_project_readiness(
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
):
    """Return the owner-facing, server-observed project start conditions.

    This endpoint intentionally reports task-creation jobs and active member
    accounts together with counters.  The settings page can therefore explain
    a pending or failed worker without treating a partially created project as
    ready.
    """
    from app.services.project import derive_classes_list

    dataset_ids = list(
        (
            await db.execute(
                select(ProjectDataset.dataset_id).where(
                    ProjectDataset.project_id == project.id
                )
            )
        ).scalars()
    )
    linked_dataset_count = len(dataset_ids)

    dataset_item_count = 0
    linked_task_count = 0
    if dataset_ids:
        dataset_item_count = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(DatasetItem)
                    .where(DatasetItem.dataset_id.in_(dataset_ids))
                )
            ).scalar()
            or 0
        )
        linked_task_count = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(Task)
                    .where(
                        Task.project_id == project.id,
                        Task.dataset_item_id.is_not(None),
                        Task.status != "uploading",
                        Task.dataset_item_id.in_(
                            select(DatasetItem.id).where(
                                DatasetItem.dataset_id.in_(dataset_ids)
                            )
                        ),
                    )
                )
            ).scalar()
            or 0
        )

    task_count = int(
        (
            await db.execute(
                select(func.count())
                .select_from(Task)
                .where(Task.project_id == project.id, Task.status != "uploading")
            )
        ).scalar()
        or 0
    )

    job_query = select(AsyncJob).where(
        AsyncJob.project_id == project.id, AsyncJob.kind == "create_tasks"
    )
    job_counts = dict(
        (
            await db.execute(
                select(AsyncJob.status, func.count())
                .where(
                    AsyncJob.project_id == project.id, AsyncJob.kind == "create_tasks"
                )
                .group_by(AsyncJob.status)
            )
        ).all()
    )
    latest_job = await db.scalar(
        job_query.order_by(AsyncJob.created_at.desc(), AsyncJob.id.desc()).limit(1)
    )
    active_jobs = job_counts.get("pending", 0) + job_counts.get("running", 0)
    failed_jobs = job_counts.get("failed", 0)

    batch_rows = (
        (
            await db.execute(
                select(TaskBatch).where(
                    TaskBatch.project_id == project.id, TaskBatch.status != "archived"
                )
            )
        )
        .scalars()
        .all()
    )
    task_counts = dict(
        (
            await db.execute(
                select(Task.batch_id, func.count())
                .where(Task.project_id == project.id, Task.status != "uploading")
                .group_by(Task.batch_id)
            )
        ).all()
    )
    # Assignment validity includes the actual receiving account and membership,
    # not merely the existence of some other active member with that role.
    # Responsibility is project-scoped: the project role alone decides, never
    # the account's platform role.
    valid_members = (
        await db.execute(
            select(ProjectMember.user_id, ProjectMember.role)
            .join(User, User.id == ProjectMember.user_id)
            .where(
                ProjectMember.project_id == project.id,
                User.is_active.is_(True),
            )
        )
    ).all()
    annotators = {user_id for user_id, role in valid_members if role == "annotator"}
    reviewers = {user_id for user_id, role in valid_members if role == "reviewer"}
    executable_statuses = {
        "active",
        "pre_annotated",
        "annotating",
        "reviewing",
        "rejected",
    }
    nonempty_batch_count = sum(task_counts.get(batch.id, 0) > 0 for batch in batch_rows)
    executable_batch_count = sum(
        batch.status in executable_statuses and task_counts.get(batch.id, 0) > 0
        for batch in batch_rows
    )
    assigned_batch_count = sum(
        batch.status in executable_statuses
        and task_counts.get(batch.id, 0) > 0
        and batch.annotator_id in annotators
        and batch.reviewer_id in reviewers
        for batch in batch_rows
    )
    return ProjectReadinessSummary(
        project_id=project.id,
        guide_ready=bool((project.annotation_guide or "").strip()),
        classes_ready=bool(derive_classes_list(project.tool_bindings)),
        linked_dataset_count=linked_dataset_count,
        dataset_item_count=dataset_item_count,
        task_count=task_count,
        linked_task_count=linked_task_count,
        task_creation_active_jobs=active_jobs,
        task_creation_failed_jobs=failed_jobs,
        latest_task_creation_status=latest_job.status if latest_job else None,
        batch_count=len(batch_rows),
        nonempty_batch_count=nonempty_batch_count,
        executable_batch_count=executable_batch_count,
        assigned_batch_count=assigned_batch_count,
        active_annotator_count=len(annotators),
        active_reviewer_count=len(reviewers),
        activated_batch_count=executable_batch_count,
    )


@router.patch("/{project_id}", response_model=ProjectOut)
async def update_project(
    data: ProjectUpdate,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    project = await WorkspaceConfigurationWorkflow(db).update_project(
        project.id,
        data.model_dump(exclude_unset=True),
        actor_id=current_user.id,
    )
    await db.commit()
    await db.refresh(project)
    result = await _serialize_project(db, project)
    result["capability_warnings"] = await _compute_pipeline_capability_warnings(
        db, project.preannotate_pipeline
    )
    return result


@router.post(
    "/{project_id}/pipelines/apply",
    response_model=ProjectPipelineOut,
    status_code=201,
)
async def apply_project_pipeline(
    body: ProjectPipelineApplyRequest,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    pipeline = await db.get(ProjectPipeline, body.pipeline_id)
    if pipeline is None:
        raise HTTPException(status_code=404, detail="编排不存在")
    await assert_pipeline_visible(db, pipeline, current_user)
    copied = await apply_saved_pipeline(
        db,
        actor_id=current_user.id,
        project_id=project.id,
        source=pipeline,
        set_default=body.set_default,
    )
    await db.commit()
    await db.refresh(copied)
    return copied


@router.delete("/{project_id}", status_code=204)
async def delete_project(
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
):
    await delete_project_transaction(db, project)
    await db.commit()
    return Response(status_code=204)


class ClassRenameRequest(BaseModel):
    old_name: str
    new_name: str
    # Omission targets all stored bindings, including disabled tool units.
    tool_unit_id: str | None = None


@router.post("/{project_id}/classes/rename", response_model=ProjectOut)
async def rename_class(
    body: ClassRenameRequest,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """B-13 · 原子地把类别 old_name 重命名为 new_name:
    - 更新 tool_bindings 中对应 unit (或所有 unit) 的 classes[].name (强隔离 · 仅本 unit)
    - **始终跨 unit** 改全项目内同名 annotations.class_name (避免历史 magic-box /
      region / 旧 schema 残留留下"孤儿"标注: binding 已无该类, 但 annotation 仍引用)
    - 不动 predictions.result (alias 不变)
    """
    changed = await rename_project_class(
        db,
        actor_id=current_user.id,
        project_id=project.id,
        old_name=body.old_name,
        new_name=body.new_name,
        tool_unit_id=body.tool_unit_id,
    )
    if not changed:
        return await _serialize_project(db, project)

    await db.commit()
    await db.refresh(project)
    return await _serialize_project(db, project)


@router.get("/{project_id}/class-usage", response_model=ProjectClassUsageOut)
async def get_class_usage(
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
):
    from app.db.models.annotation import Annotation

    class_rows = (
        await db.execute(
            select(Annotation.class_name, func.count(Annotation.id))
            .where(
                Annotation.project_id == project.id,
                Annotation.is_active.is_(True),
            )
            .group_by(Annotation.class_name)
        )
    ).all()
    classes = {name: int(count or 0) for name, count in class_rows if name}

    attribute_rows = (
        await db.execute(
            select(Annotation.attributes).where(
                Annotation.project_id == project.id,
                Annotation.is_active.is_(True),
            )
        )
    ).scalars()
    attributes: Counter[str] = Counter()
    for attrs in attribute_rows:
        if isinstance(attrs, dict):
            attributes.update(str(key) for key in attrs.keys())

    return ProjectClassUsageOut(classes=classes, attributes=dict(attributes))


@router.post("/{project_id}/cleanup-orphans", response_model=ProjectCleanupOrphansOut)
async def cleanup_annotation_orphans(
    request: Request,
    body: ProjectCleanupOrphansRequest | None = None,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    dry_run = True if body is None else body.dry_run
    result = await cleanup_project_annotation_orphans(
        db,
        project=project,
        actor_id=current_user.id,
        dry_run=dry_run,
    )

    if not dry_run:
        from app.services.audit import AuditService

        await AuditService.log(
            db,
            actor=current_user,
            action="project.cleanup_annotation_orphans",
            target_type="project",
            target_id=str(project.id),
            request=request,
            status_code=200,
            detail=result,
        )
        await db.commit()

    return ProjectCleanupOrphansOut(**result)


@router.post("/{project_id}/transfer", response_model=ProjectOut)
async def transfer_owner(
    body: ProjectTransferRequest,
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    project = await transfer_project_owner_workflow(
        db,
        actor_id=current_user.id,
        project_id=project_id,
        new_owner_id=body.new_owner_id,
    )
    await db.commit()
    await db.refresh(project)
    return await _serialize_project(db, project)


@router.get("/{project_id}/access", response_model=ProjectAccessOut)
async def get_project_access(
    project: Project = Depends(require_project_visible),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Resolved project access for the current account.

    Managers may have null membership fields; ordinary members expose their
    membership identity/version and fixed capability set.
    """

    access = await resolve_project_access(db, user=current_user, project=project)
    return ProjectAccessOut(
        project_id=access.project_id,
        user_id=access.user_id,
        platform_role=access.platform_role,
        project_role=access.project_role,
        membership_id=access.membership_id,
        membership_version=access.membership_version,
        access_kind=access.access_kind,
        is_manager=access.is_manager,
        capabilities=sorted(access.capabilities),
    )


@router.get("/{project_id}/members", response_model=list[ProjectMemberOut])
async def list_members(
    project: Project = Depends(require_project_visible),
    db: AsyncSession = Depends(get_db),
):
    rows = await db.execute(
        select(ProjectMember, User.name, User.email, User.avatar_ref, User.role)
        .join(User, User.id == ProjectMember.user_id)
        .where(ProjectMember.project_id == project.id)
        .order_by(ProjectMember.assigned_at.desc())
    )
    out = []
    for member, user_name, user_email, avatar_ref, platform_role in rows.all():
        out.append(
            ProjectMemberOut(
                id=member.id,
                user_id=member.user_id,
                user_name=user_name,
                user_email=user_email,
                role=member.role,
                platform_role=platform_role,
                version=member.version,
                assigned_at=member.assigned_at,
                updated_at=member.updated_at,
                avatar_ref=avatar_ref,
            )
        )
    return out


@router.get(
    "/{project_id}/mention-candidates", response_model=list[MentionCandidateOut]
)
async def list_mention_candidates(
    project: Project = Depends(require_project_visible),
    db: AsyncSession = Depends(get_db),
):
    """讨论区 @ 提及候选：项目负责人、启用的平台超管、项目成员，按 user_id 去重。

    顺序固定为 负责人 → 超管 → 成员，前端据此展示「项目负责人 / 超级管理员」标签。
    """
    seen: set[uuid.UUID] = set()
    out: list[MentionCandidateOut] = []

    def add(user_id: uuid.UUID, name: str, email: str | None, kind: str) -> None:
        if user_id in seen:
            return
        seen.add(user_id)
        out.append(
            MentionCandidateOut(
                user_id=user_id, user_name=name, user_email=email, kind=kind
            )
        )

    if project.owner_id is not None:
        owner = (
            await db.execute(
                select(User.name, User.email, User.is_active).where(
                    User.id == project.owner_id
                )
            )
        ).first()
        if owner is not None and owner.is_active:
            add(project.owner_id, owner.name, owner.email, "owner")

    super_admins = (
        await db.execute(
            select(User.id, User.name, User.email)
            .where(
                User.role == UserRole.SUPER_ADMIN.value,
                User.is_active.is_(True),
            )
            .order_by(User.name)
        )
    ).all()
    for user_id, name, email in super_admins:
        add(user_id, name, email, "super_admin")

    members = (
        await db.execute(
            select(ProjectMember, User.name, User.email)
            .join(User, User.id == ProjectMember.user_id)
            .where(ProjectMember.project_id == project.id)
            .order_by(ProjectMember.assigned_at.desc())
        )
    ).all()
    for member, user_name, user_email in members:
        add(member.user_id, user_name, user_email, "member")

    return out


async def _member_out(db: AsyncSession, member: ProjectMember) -> ProjectMemberOut:
    target = await db.get(User, member.user_id)
    return ProjectMemberOut(
        id=member.id,
        user_id=member.user_id,
        user_name=target.name if target else "",
        user_email=target.email if target else "",
        role=member.role,
        platform_role=target.role if target else None,
        version=member.version,
        assigned_at=member.assigned_at,
        updated_at=member.updated_at,
        avatar_ref=target.avatar_ref if target else None,
    )


@router.post("/{project_id}/members", response_model=ProjectMemberOut, status_code=201)
async def add_member(
    body: ProjectMemberCreate,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _access=Depends(require_project_capability(ProjectCapability.MEMBER_MANAGE.value)),
):
    member = await add_project_member(
        db,
        project=project,
        actor=current_user,
        target_user_id=body.user_id,
        project_role=body.role,
    )
    return await _member_out(db, member)


@router.post(
    "/{project_id}/members/{member_id}/role/preview",
    response_model=ProjectMemberRolePreviewOut,
)
async def preview_member_role_change(
    member_id: uuid.UUID,
    body: ProjectMemberRolePreviewRequest,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _access=Depends(require_project_capability(ProjectCapability.MEMBER_MANAGE.value)),
):
    """Read-only role-change preview: blockers, resource snapshot and token."""

    result = await preview_project_member_role_change(
        db,
        project=project,
        actor=current_user,
        member_id=member_id,
        target_role=body.project_role,
        replacement_annotator_id=body.replacement_annotator_id,
        replacement_reviewer_id=body.replacement_reviewer_id,
    )
    return ProjectMemberRolePreviewOut(**result)


@router.patch(
    "/{project_id}/members/{member_id}/role",
    response_model=ProjectMemberOut,
)
async def change_member_role(
    member_id: uuid.UUID,
    body: ProjectMemberRoleChangeRequest,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _access=Depends(require_project_capability(ProjectCapability.MEMBER_MANAGE.value)),
):
    """Role change with expected version, preview token and explicit handoff."""

    member = await change_project_member_role(
        db,
        project=project,
        actor=current_user,
        member_id=member_id,
        target_role=body.project_role,
        expected_version=body.expected_version,
        preview_token=body.preview_token,
        reason=body.reason,
        replacement_annotator_id=body.replacement_annotator_id,
        replacement_reviewer_id=body.replacement_reviewer_id,
    )
    return await _member_out(db, member)


@router.delete("/{project_id}/members/{member_id}", status_code=204)
async def remove_member(
    member_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _access=Depends(require_project_capability(ProjectCapability.MEMBER_MANAGE.value)),
):
    await remove_project_member(
        db, project=project, actor=current_user, member_id=member_id
    )
    return Response(status_code=204)


def _validate_export_targets(
    targets: list[str], data_type: str | None = None
) -> list[str]:
    """v0.10.43 · 校验并去重导出目标，非法转 400。v0.10.47 · 按 data_type 过滤模态。"""
    from app.services.exporting.packaging import clean_export_targets

    try:
        return clean_export_targets(targets, data_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/{project_id}/exports/lidar:preflight",
    response_model=LidarExportPreflightResponse,
)
async def preflight_project_lidar_export(
    body: LidarExportPreflightRequest,
    project: Project = Depends(require_project_visible),
    db: AsyncSession = Depends(get_db),
    _access=Depends(
        require_project_capability(ProjectCapability.EXPORT_ANNOTATIONS.value)
    ),
) -> LidarExportPreflightResponse:
    if project.data_type != "lidar":
        raise HTTPException(status_code=422, detail="project is not a lidar project")
    targets = _validate_export_targets(body.targets, project.data_type)
    from app.services.exporting.lidar_preflight import preflight_lidar_export

    return await preflight_lidar_export(
        db,
        project_id=project.id,
        batch_id=None,
        targets=targets,
        options=body.lidar,
    )


@router.post("/{project_id}/export", status_code=202)
async def export_project(
    request: Request,
    body: ExportRequestBody | None = None,
    targets: list[str] = Query(
        default=["coco"],
        description="导出目标，可多选：coco / yolo-det / yolo-obb / yolo-seg"
        " / label-studio-brush / binary-png / indexed-png / aap_json"
        " / video_json / yolo-frames-det / yolo-frames-seg / coco-frames-seg / davis"
        " / youtube-vos / mots / mot / kitti"
        " / coco-multicamera / nuscenes / pointmask"
        "（voc 仅可单选，走同步下载；lidar.kitti 为 3D label，video.kitti 为 tracking label）",
    ),
    include_attributes: bool = Query(
        True,
        description="是否在导出包中携带 annotation.attributes 与 project.attribute_schema",
    ),
    video_frame_mode: str = Query(
        "keyframes",
        pattern="^(keyframes|all_frames)$",
        description="video-track 导出帧模式：keyframes 仅关键帧，all_frames 展开逐帧插值；图片项目忽略",
    ),
    axis_frame: str = Query(
        "iso",
        pattern="^(iso|source)$",
        description="3D box export axis frame: iso keeps platform-normalized PSR; source maps back to dataset axis convention",
    ),
    indexed_overlap_policy: str = Query(
        "error",
        pattern="^(error|z_order|larger_area|smaller_area)$",
        description="Indexed PNG 的实例重叠策略；默认拒绝重叠",
    ),
    video_overlap_policy: str = Query(
        "error",
        pattern="^(error|z_order|larger_area|smaller_area)$",
        description="DAVIS / YouTube-VOS 的实例重叠策略；默认拒绝重叠",
    ),
    mots_frame_base: int = Query(
        0,
        ge=0,
        le=1,
        description="MOTS 输出帧号基准：0 或 1",
    ),
    project: Project = Depends(require_project_visible),
    actor: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _access=Depends(
        require_project_capability(ProjectCapability.EXPORT_ANNOTATIONS.value)
    ),
):
    # v0.10.27 · 导出异步化：创建 async_job(kind=export) + 派发 run_export，返回 {job_id}。
    # v0.10.43 · 多目标（方案 B）：一个 job 产一个 zip（>1 目标分子目录）。
    # 状态/下载走 GET /async-jobs/{id}（result.download_url 为 7d 预签名）。
    # VOC 后端保留同步 blob（前端已隐藏），仅可单选；不删避免破坏 API 契约。
    from app.services.audit import AuditService, AuditAction, export_detail

    targets = _validate_export_targets(targets, project.data_type)

    lidar_options = body.lidar if body else None
    lidar_options_payload = (
        lidar_options.model_dump(mode="json", exclude_none=True)
        if lidar_options
        else None
    )
    if project.data_type == "lidar":
        from app.services.exporting.lidar_preflight import (
            LidarExportPreflightFailed,
            assert_lidar_export_ready,
        )

        try:
            await assert_lidar_export_ready(
                db,
                project_id=project.id,
                batch_id=None,
                targets=targets,
                options=lidar_options,
            )
        except LidarExportPreflightFailed as exc:
            raise HTTPException(
                status_code=409,
                detail=exc.report.model_dump(mode="json"),
            ) from exc

    from app.services.exporting.video_scope import normalize_video_export_scope

    try:
        video_scope = await normalize_video_export_scope(
            db,
            project=project,
            request=body.scope if body else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    video_scope_payload = video_scope.as_dict() if video_scope else None
    if project.data_type == "video":
        from app.services.video_canonical import (
            VideoBoundaryUnreconciledError,
            assert_export_boundaries_reconciled,
        )

        try:
            await assert_export_boundaries_reconciled(
                db,
                project=project,
                batch_id=None,
                scope=video_scope,
            )
        except VideoBoundaryUnreconciledError as exc:
            raise HTTPException(status_code=409, detail=exc.detail) from exc

    if "voc" in targets:
        if targets != ["voc"]:
            raise HTTPException(
                status_code=400, detail="voc 格式只能单独导出，不能与其它目标混选"
            )
        from app.services.exporting.service import ExportService, UnsupportedExportError

        svc = ExportService(db)
        try:
            data = await svc.export_voc(
                project.id, include_attributes=include_attributes
            )
        except UnsupportedExportError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await AuditService.log(
            db,
            actor=actor,
            action=AuditAction.PROJECT_EXPORT,
            target_type="project",
            target_id=str(project.id),
            request=request,
            status_code=200,
            detail={"format": "voc", "project_display_id": project.display_id},
        )
        await db.commit()
        return Response(
            content=data,
            media_type="application/zip",
            headers={
                "Content-Disposition": f"attachment; filename={project.display_id}_voc.zip"
            },
        )

    from app.services import async_job as async_job_svc
    from app.db.models.async_job import AsyncJobKind
    from app.workers.export import run_export

    job = await async_job_svc.create_job(
        db,
        kind=AsyncJobKind.EXPORT.value,
        user_id=actor.id,
        project_id=project.id,
        payload={
            "targets": targets,
            "format": ",".join(targets),
            "include_attributes": include_attributes,
            "video_frame_mode": video_frame_mode,
            "axis_frame": axis_frame,
            "indexed_overlap_policy": indexed_overlap_policy,
            "video_overlap_policy": video_overlap_policy,
            "mots_frame_base": mots_frame_base,
            "project_display_id": project.display_id,
            **(
                {"video_export_scope": video_scope_payload}
                if video_scope_payload
                else {}
            ),
            **({"lidar": lidar_options_payload} if lidar_options_payload else {}),
        },
    )
    await AuditService.log(
        db,
        actor=actor,
        action=AuditAction.PROJECT_EXPORT,
        target_type="project",
        target_id=str(project.id),
        request=request,
        status_code=202,
        detail=export_detail(
            actor=actor,
            request=request,
            base={"targets": targets, "project_display_id": project.display_id},
            filter_criteria={
                "include_attributes": include_attributes,
                "video_frame_mode": video_frame_mode,
                "axis_frame": axis_frame,
                "indexed_overlap_policy": indexed_overlap_policy,
                "video_overlap_policy": video_overlap_policy,
                "mots_frame_base": mots_frame_base,
                **(
                    {"video_export_scope": video_scope_payload}
                    if video_scope_payload
                    else {}
                ),
                **({"lidar": lidar_options_payload} if lidar_options_payload else {}),
            },
        ),
    )
    await db.commit()

    run_export.delay(
        project_id=str(project.id),
        batch_id=None,
        targets=targets,
        opts={
            "include_attributes": include_attributes,
            "video_frame_mode": video_frame_mode,
            "axis_frame": axis_frame,
            "indexed_overlap_policy": indexed_overlap_policy,
            "video_overlap_policy": video_overlap_policy,
            "mots_frame_base": mots_frame_base,
            **(
                {"video_export_scope": video_scope_payload}
                if video_scope_payload
                else {}
            ),
            **({"lidar": lidar_options_payload} if lidar_options_payload else {}),
        },
        async_job_id=str(job.id),
    )
    return {"job_id": str(job.id)}


def _stage_supported_inputs(backend, model_id: str | None) -> list[str]:
    """v0.18.15 · 从 backend 能力快照取某 model 的 supported_inputs (一等输入契约)。

    health_meta.capabilities.models[] 已由 extract_capabilities 规范化 (含合成默认)。
    无快照 / 无 model_id / 匹配不到 → 返回 [] (调用方据此放过门控, 保持零退化)。
    """
    meta = getattr(backend, "health_meta", None)
    caps = meta.get("capabilities") if isinstance(meta, dict) else None
    models = caps.get("models") if isinstance(caps, dict) else None
    if not isinstance(models, list) or model_id is None:
        return []
    for m in models:
        if isinstance(m, dict) and str(m.get("id")) == str(model_id):
            return list(m.get("supported_inputs") or [])
    return []


def _stage_model(backend, model_id: str | None) -> dict:
    """v0.19.2 WS2 · 从 backend 能力快照取某 model 的规范化条目 (无快照/匹配不到 → {})。

    供 dispatch-time 校验读 resource_profile / output_attribute_types。读
    health_meta.capabilities.models[] (extract_capabilities 已规范化), 不走 /instances。
    """
    meta = getattr(backend, "health_meta", None)
    caps = meta.get("capabilities") if isinstance(meta, dict) else None
    models = caps.get("models") if isinstance(caps, dict) else None
    if not isinstance(models, list) or model_id is None:
        return {}
    for m in models:
        if isinstance(m, dict) and str(m.get("id")) == str(model_id):
            return {
                **m,
                "task": normalize_task(m.get("task") or "unknown", allow_unknown=True),
            }
    return {}


def _stage_device(backend, model_id: str | None) -> str | None:
    """v0.19.5 · 取某 model 的 resource_profile.device (gpu/cpu); 无快照/未自报 → None。"""
    from app.services.trained_models import controlled_device, is_controlled

    if is_controlled(model_id):
        return controlled_device(backend)
    rp = _stage_model(backend, model_id).get("resource_profile") or {}
    d = rp.get("device")
    return d if isinstance(d, str) else None


def _assert_capabilities(
    backend, model_id: str | None, where: str, *, writes_attributes: bool
) -> None:
    """v0.19.3 WS1 · 派发期能力闸门: 任一能力违例 → 422 硬挡。

    判据本体抽到 services/pipeline_validation.check_capability_violations (纯函数), 与保存路径
    软提示 + 前端 stageWarning 共用同一 SSOT。batchable=false (交互/有状态) 不可批量; 写属性的
    下游模型不产 class → 提前拦。均「显式自报才拦, 缺省放过」, 对老 backend 零退化。
    """
    violations = check_capability_violations(
        _stage_model(backend, model_id),
        where=where,
        model_id=model_id,
        writes_attributes=writes_attributes,
    )
    if violations:
        raise HTTPException(status_code=422, detail=violations[0].detail)


async def _compute_pipeline_capability_warnings(db, stages) -> list[str]:
    """v0.19.3 WS1 · 保存编排时算「能力软提示」(不挡), 与 dispatch 422 同判据共享纯函数。

    保存是配置中途态: backend 可能未启用 / 能力快照滞后 / 先存草稿之后再换 backend, 故只软提示。
    解析不到 backend 的阶段静默跳过 (留 dispatch-time 422 作最终把关)。返回 detail 字符串列表。
    """
    if not stages or not isinstance(stages, list):
        return []
    from app.services.ml_backend import MLBackendService

    svc = MLBackendService(db)
    warnings: list[str] = []
    # v0.20.21 · 建 stage号→(caps, model_id) map, 供下面校验下游阶段的「上游产出几何可否作 ROI」。
    stage_caps: dict[int, tuple[dict, object]] = {}
    for s in stages:
        if not isinstance(s, dict) or not s.get("ml_backend_id"):
            continue
        backend = await svc.get(s["ml_backend_id"])
        if not backend:
            continue
        caps = _stage_model(backend, s.get("model_id"))
        if s.get("stage") is not None:
            stage_caps[s["stage"]] = (caps, s.get("model_id"))
        is_source = s.get("parent_stage") is None
        where = "源阶段" if is_source else f"stage {s.get('stage')} "
        # 源阶段不写属性, class 判据不适用 (与 dispatch 对称)。
        writes_attributes = (
            not is_source
            and (s.get("write") or {}).get("target", "attributes") == "attributes"
        )
        violations = check_capability_violations(
            caps,
            where=where,
            model_id=s.get("model_id"),
            writes_attributes=writes_attributes,
        )
        warnings.extend(v.detail for v in violations)
    # v0.20.21 · 上下游几何兼容: 下游按上游框作 ROI, 上游若不产 bbox/polygon → 软提示。
    for s in stages:
        if not isinstance(s, dict):
            continue
        parent = s.get("parent_stage")
        if parent is None or parent not in stage_caps:
            continue
        parent_caps, parent_model_id = stage_caps[parent]
        geo_violations = check_parent_geometry_roi(
            parent_caps,
            where=f"stage {s.get('stage')} ",
            parent_model_id=parent_model_id,
        )
        warnings.extend(v.detail for v in geo_violations)
    return warnings


async def _validate_preannotate_task_scope(
    db: AsyncSession,
    *,
    project: Project,
    task_ids: list[uuid.UUID],
    batch_id: uuid.UUID | None,
) -> list[uuid.UUID]:
    """Validate and canonicalize an explicit preannotation task scope."""

    requested = sorted(set(task_ids), key=str)
    rows = (
        await db.execute(
            select(Task, TaskBatch)
            .outerjoin(TaskBatch, TaskBatch.id == Task.batch_id)
            .where(Task.project_id == project.id, Task.id.in_(requested))
        )
    ).all()
    if len(rows) != len(requested):
        raise HTTPException(
            status_code=422,
            detail="task_ids must belong to the selected project",
        )
    if batch_id is not None and any(task.batch_id != batch_id for task, _ in rows):
        raise HTTPException(
            status_code=422,
            detail="task_ids must belong to batch_id",
        )
    if any(batch is not None and batch.admin_locked for _, batch in rows):
        raise HTTPException(
            status_code=409,
            detail="one or more selected batches are admin-locked",
        )
    if any(task.status != "pending" for task, _ in rows):
        raise HTTPException(
            status_code=409,
            detail="explicit preannotation tasks must be pending",
        )
    # issue #124 · draft 批次在未分派标注员与质检员时同样可批量预标;
    # 已分派人员的 draft 与已进入人工流程的批次仍拒绝。
    if any(
        batch is not None and not allows_bulk_preannotation(batch) for _, batch in rows
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "explicit preannotation tasks must belong to active or "
                "unassigned draft batches"
            ),
        )
    from app.services.task_lock import TaskLockService

    lock_service = TaskLockService(db)
    for task, _ in rows:
        if await lock_service.active_lock(task.id) is not None:
            raise HTTPException(
                status_code=409,
                detail="one or more selected tasks are locked for editing",
            )
    return requested


# 终态任务: 先撤回 / 重开后再运行; 其它不可编辑状态 (如 uploading) 单独提示。
_WORKBENCH_AI_TERMINAL_STATUSES = {
    TaskStatus.REVIEW.value,
    TaskStatus.COMPLETED.value,
}


async def _validate_workbench_task_scope(
    db: AsyncSession,
    *,
    project: Project,
    task_ids: list[uuid.UUID],
    actor: User,
) -> list[uuid.UUID]:
    """校验工作台「当前题 AI」的单题执行范围 (issue #121)。

    与数据管理批量路径不同, 交互式执行只针对调用者正在编辑的那道题, 因此允许
    in_progress 任务与 draft 批次; 任务归属、批次管理员锁、他人编辑锁与终态任务
    仍然受保护。拒绝原因用中文, 因为工作台面板会原样展示 API detail。
    """

    requested = sorted(set(task_ids), key=str)
    if len(requested) != 1:
        raise HTTPException(
            status_code=422,
            detail="工作台当前题 AI 一次仅支持一个任务",
        )
    row = (
        await db.execute(
            select(Task, TaskBatch)
            .outerjoin(TaskBatch, TaskBatch.id == Task.batch_id)
            .where(Task.project_id == project.id, Task.id == requested[0])
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="任务不存在或不属于当前项目")
    task, batch = row
    if batch is not None and batch.admin_locked:
        raise HTTPException(
            status_code=409,
            detail="任务所属批次已被管理员锁定，无法运行 AI",
        )
    # 白名单而非黑名单: 只有明确可编辑的 pending / in_progress / rejected 放行,
    # uploading (对象未校验) 与未来新增/非法状态一律拒绝 (issue #121 review)。
    if task.status not in WORKBENCH_AI_EDITABLE_TASK_STATUSES:
        if task.status in _WORKBENCH_AI_TERMINAL_STATUSES:
            detail = "当前任务已提交审核或已完成，请先撤回或重开后再运行 AI"
        else:
            detail = "当前任务尚未就绪，请等待上传完成后再运行 AI"
        raise HTTPException(status_code=409, detail=detail)
    from app.services.task_lock import TaskLockService

    # 同一 task 可能存在多行残留锁 (unique 仅在 (task_id, user_id) 上), 必须逐行判断,
    # 不能只看 active_lock() 的第一行, 否则他人持锁时可能被本人锁掩盖 (issue #121 review)。
    locks = await TaskLockService(db).active_locks(task.id)
    if any(lock.user_id != actor.id for lock in locks):
        raise HTTPException(
            status_code=409,
            detail="当前任务正由其他成员编辑，无法运行 AI",
        )
    return requested


@router.post("/{project_id}/preannotate")
async def trigger_preannotation(
    body: preannotate_schemas.PreannotateRequest,
    request: Request,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        description="Optional durable identity for retrying a scoped preannotation request.",
    ),
    batch_dispatcher: BatchPredictionDispatcher = Depends(
        get_batch_prediction_dispatcher
    ),
):
    from app.services.ml_backend import MLBackendService
    from app.services.audit import AuditService
    from app.services.prediction_context import build_predict_context
    from app.services.prediction_retry import retryable_request_context
    from app.services import trained_models

    uses_controlled_model = trained_models.is_controlled(body.model_id) or any(
        trained_models.is_controlled(stage.model_id)
        for stage in (body.pipeline_stages or [])
    )
    frozen_model_ref = None
    if uses_controlled_model:
        root_source = (
            next(
                (
                    s.source
                    for s in (body.pipeline_stages or [])
                    if s.parent_stage is None
                ),
                None,
            )
            or {}
        )
        trained_models.assert_image_scope(
            project,
            prompt=body.prompt,
            task=body.task,
            prompt_type=body.prompt_type,
            model_variants=body.model_variants,
            pipeline_stages=body.pipeline_stages,
            execution_unit=root_source.get("execution_unit"),
        )
        if root_source.get("data_type") not in {None, "image"}:
            raise HTTPException(status_code=422, detail="受控模型不支持该数据源")

    svc = MLBackendService(db)
    source_backend_id = body.ml_backend_id
    if source_backend_id is None:
        raise HTTPException(status_code=422, detail="ml_backend_id 必填")
    backend = await svc.get(source_backend_id)
    # v0.19.0 ADR-0044 · 校验 backend 存在且在本项目「已启用」
    # (project_ml_backend_pool.enabled, 经 pool member 解析回 registry):
    # 与下游阶段校验对称, 防止 owner 手动 POST 未启用/别项目 backend id 触发预标。
    if not backend or not await svc.is_enabled(project.id, source_backend_id):
        raise HTTPException(status_code=404, detail="ML Backend not found")

    from app.services.ml_capabilities import resolve_model_task

    try:
        body.task = resolve_model_task(backend, body.model_id, body.task)
        for stage in body.pipeline_stages or []:
            if stage.parent_stage is None:
                stage.task = body.task
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    retry_context = build_predict_context(
        prompt=body.prompt,
        output_mode=body.output_mode,
        params=body.params,
        model_id=body.model_id,
        task=body.task,
        prompt_type=body.prompt_type,
        model_variants=body.model_variants,
        class_filter=body.class_filter,
        box_threshold=float(project.box_threshold),
        text_threshold=float(project.text_threshold),
    )
    if retry_context is not None and retryable_request_context(retry_context) is None:
        raise HTTPException(status_code=422, detail="预标注模型上下文不能超过 8 KiB")

    # v0.19.2 WS2 · 源阶段 (单模型预标 / 流水线源) batchable 闸门: 交互/有状态模型不可批量。
    # 源阶段不写属性, class 判据不适用 (writes_attributes=False)。
    _assert_capabilities(backend, body.model_id, "源阶段", writes_attributes=False)

    # v0.19.5 · 设备感知队列路由: 收集源 + 各下游阶段 model 的 device, 据此选 Celery 队列。
    stage_devices: list[str | None] = [_stage_device(backend, body.model_id)]

    # v0.18.1 · 多阶段编排: 校验每个下游阶段的 backend 存在且归属本项目, 归一化成 worker
    # 可消费的 stage dict 列表 (uuid → str)。源阶段 backend 归属已在上面校验。
    pipeline_stages_payload: list[dict] | None = None
    if body.pipeline_stages:
        norm: list[dict] = []
        # v0.20.21 · stage号→(caps, model_id) map, source stage is identified by parent_stage=None.
        # 循环内填各下游 caps, 循环后校验「上游产出几何可否作下游 ROI」。
        stage_caps: dict[int, tuple[dict, object]] = {
            next(
                stage.stage
                for stage in body.pipeline_stages
                if stage.parent_stage is None
            ): (_stage_model(backend, body.model_id), body.model_id)
        }
        for st in body.pipeline_stages:
            resolved_input = st.input
            if st.parent_stage is not None:
                st_backend = await svc.get(st.ml_backend_id)
                if not st_backend or not await svc.is_enabled(
                    project.id, st.ml_backend_id
                ):
                    raise HTTPException(
                        status_code=404,
                        detail=f"stage {st.stage} 的 ML Backend 不存在或未在本项目启用",
                    )
                try:
                    st.task = resolve_model_task(st_backend, st.model_id, st.task)
                except ValueError as exc:
                    raise HTTPException(
                        status_code=422, detail=f"stage {st.stage}: {exc}"
                    ) from exc
                # v0.19.2 WS2 / v0.19.3 WS1 · 下游阶段能力闸门: batchable + 写属性产 class。
                # 写属性的子但模型不产 class → 跑完属性恒空, 提前 422。
                _assert_capabilities(
                    st_backend,
                    st.model_id,
                    f"stage {st.stage} ",
                    writes_attributes=(st.write or {}).get("target", "attributes")
                    == "attributes",
                )
                # v0.19.5 · 收集下游阶段 device 供队列路由。
                stage_devices.append(_stage_device(st_backend, st.model_id))
                # v0.20.21 · 记本阶段 caps, 供循环后校验其作为下游父阶段时几何可否作 ROI。
                stage_caps[st.stage] = (
                    _stage_model(st_backend, st.model_id),
                    st.model_id,
                )
                # v0.18.15 · 按子模型 supported_inputs 解析投递方式 + 几何可达性门控。
                # 产几何的子: supported_inputs 须含 bbox_prompt (geometry-prompt 路径) 或
                # crop (普通检测器在 crop 上跑 + 坐标回映)。据此把投递方式烘焙进 input.mode,
                # worker 直接消费 (无快照时 inputs=[] → 放过门控、不烘焙, 保持零退化)。
                target = (st.write or {}).get("target", "attributes")
                inputs = _stage_supported_inputs(st_backend, st.model_id)
                if target in {"geometry", "intermediate"} and inputs:
                    if INPUT_BBOX_PROMPT not in inputs and INPUT_CROP not in inputs:
                        raise HTTPException(
                            status_code=422,
                            detail=(
                                f"stage {st.stage} 产几何 (write.target={target!r}), 但其模型 "
                                f"supported_inputs={inputs} 不含 "
                                f"{INPUT_BBOX_PROMPT!r}/{INPUT_CROP!r}, 无法作几何下游"
                            ),
                        )
                    if not (st.input or {}).get("mode"):
                        mode = "geometry" if INPUT_BBOX_PROMPT in inputs else "crop"
                        resolved_input = {**(st.input or {}), "mode": mode}
            norm.append(
                {
                    "stage": st.stage,
                    "ml_backend_id": str(st.ml_backend_id),
                    "model_id": st.model_id,
                    "task": st.task,
                    "prompt_type": st.prompt_type,
                    "model_variants": st.model_variants,
                    "params": st.params,
                    "class_filter": st.class_filter,
                    "parent_stage": st.parent_stage,
                    "parent_class_filter": st.parent_class_filter,
                    "roi": st.roi,
                    "write": st.write,
                    "on_failure": st.on_failure,
                    # v0.18.14 · 卡片显示名 + 写回属性键前缀 (子物体命名空间, 如 hat_color)。
                    "label": st.label,
                    # v0.18.15 · 投递模式: 用户显式 input 或按 supported_inputs 烘焙的结果。
                    "input": resolved_input,
                    # v0.18.2 · 键冲突策略下放到每个下游阶段, worker 末位覆盖时据此合并。
                    "on_key_conflict": body.on_key_conflict,
                }
            )
        # v0.20.21 · 上下游几何兼容闸门 (与「下游能否吃框」门对称): 下游按上游框作 ROI,
        # 上游若完全不产 bbox/polygon → 该阶段所有父框运行期被跳过、零富集, 提前 422 硬挡。
        for st in body.pipeline_stages:
            if st.parent_stage is None or st.parent_stage not in stage_caps:
                continue
            parent_caps, parent_model_id = stage_caps[st.parent_stage]
            geo_violations = check_parent_geometry_roi(
                parent_caps,
                where=f"stage {st.stage} ",
                parent_model_id=parent_model_id,
            )
            if geo_violations:
                raise HTTPException(status_code=422, detail=geo_violations[0].detail)
        pipeline_stages_payload = norm

    if body.execution_scope == "workbench":
        if not body.task_ids:
            raise HTTPException(
                status_code=422,
                detail="工作台当前题 AI 必须指定当前任务",
            )
        # 工作台单题语义由任务推导批次, 不接受 batch_id: 否则会落入下方「批次须 active」
        # 的批量校验 (draft 批次误报 400) 或放行无关 active 批次 (issue #121 review)。
        if body.batch_id is not None:
            raise HTTPException(
                status_code=422,
                detail="工作台当前题 AI 不支持指定批次",
            )
    explicit_task_ids: list[uuid.UUID] | None = None
    if body.task_ids:
        if body.execution_scope == "workbench":
            explicit_task_ids = await _validate_workbench_task_scope(
                db,
                project=project,
                task_ids=body.task_ids,
                actor=current_user,
            )
        else:
            explicit_task_ids = await _validate_preannotate_task_scope(
                db,
                project=project,
                task_ids=body.task_ids,
                batch_id=body.batch_id,
            )

    # v0.9.5 · 指定 batch 时校验归属本项目 + 状态在 active
    total_tasks_hint: int | None = None
    if body.batch_id:
        from app.db.models.task_batch import TaskBatch
        from app.db.enums import BatchStatus
        from app.db.models.task import Task as TaskModel

        batch = await db.get(TaskBatch, body.batch_id)
        if not batch or batch.project_id != project.id:
            raise HTTPException(status_code=404, detail="Batch not found")
        # issue #124 · 未分派人员（标注员与质检员）的 draft 批次同样可批量预标,
        # 让管理员先跑 AI 再分派人工; 已分派 draft / 其它状态仍拒绝。
        if not allows_bulk_preannotation(batch):
            if batch.status == BatchStatus.DRAFT:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "draft batch already has an annotator or reviewer "
                        "assigned; bulk preannotation is only available "
                        "before assignment"
                    ),
                )
            raise HTTPException(
                status_code=400,
                detail=(
                    "batch.status must be 'active' or an unassigned 'draft' "
                    f"to preannotate, got {batch.status!r}"
                ),
            )
        if batch.admin_locked:
            raise HTTPException(status_code=409, detail="batch is admin-locked")
        hint_conds = [
            TaskModel.batch_id == body.batch_id,
            TaskModel.status == "pending",
        ]
        # v0.11.24 · skip_predicted 下进度条分母应排除已预标 task，否则虚高
        if body.predict_mode == "skip_predicted":
            hint_conds.append(TaskModel.total_predictions == 0)
        count_q = await db.execute(select(func.count(TaskModel.id)).where(*hint_conds))
        total_tasks_hint = int(count_q.scalar_one() or 0)

    # v0.19.5 · 设备感知路由: 全 CPU pipeline → cpu 队列; 任一 GPU/未自报阶段 → gpu(ml) 队列 (零退化)。
    queue = resolve_preannotate_queue(
        stage_devices,
        gpu_queue=settings.preannotate_gpu_queue,
        cpu_queue=settings.preannotate_cpu_queue,
    )
    # v0.21.6 · detect-then-track: 含 tracker 阶段的 job 施加 soft 超时 (帧上限之外的双保险)。
    #   单阶段源 tracker 走 body.task; 编排里的 tracker 阶段走 pipeline_stages_payload。
    has_tracker_stage = body.task == "tracker" or any(
        (s or {}).get("task") == "tracker" for s in (pipeline_stages_payload or [])
    )
    # v0.21.7 · 逐帧执行单位: 从源阶段(parent_stage=None)的 source.execution_unit 提取 (norm 丢弃了
    #   source, 故从原始 body.pipeline_stages 取)。frame → worker 走二级 fan-out 逐帧跑图像 backend。
    #   execution_unit 是整任务迭代粒度、非 per-stage, 故作 batch_predict 顶层参数。
    #   源阶段判据须与 _validate_pipeline_stages 一致 (恰一个 parent_stage=None); stage 号是自由整数,
    #   不保证为 0, 故不能按 s.stage == 0 找源。
    execution_unit: str | None = None
    if body.pipeline_stages:
        src_stage = next(
            (s for s in body.pipeline_stages if s.parent_stage is None), None
        )
        if src_stage is not None and src_stage.source:
            execution_unit = (src_stage.source or {}).get("execution_unit")

    if uses_controlled_model:
        media_scope = [Task.project_id == project.id, Task.file_type != "image"]
        if explicit_task_ids is not None:
            media_scope.append(Task.id.in_(explicit_task_ids))
        else:
            media_scope.append(Task.status == "pending")
        if body.batch_id:
            media_scope.append(Task.batch_id == body.batch_id)
        if await db.scalar(select(Task.id).where(*media_scope).limit(1)):
            raise HTTPException(status_code=422, detail="所选任务包含非图片素材")

    async def freeze_controlled_model():
        ref = await trained_models.resolve_reference(
            db, project, body.model_id, source_backend_id
        )
        checked = build_predict_context(
            prompt=body.prompt,
            output_mode=body.output_mode,
            params=body.params,
            model_id=body.model_id,
            task=body.task,
            prompt_type=body.prompt_type,
            model_variants=body.model_variants,
            class_filter=body.class_filter,
            frozen_model_ref=ref,
        )
        if checked is not None and retryable_request_context(checked) is None:
            raise HTTPException(
                status_code=422, detail="模型类别与来源上下文超过 8 KiB 限制"
            )
        return ref

    # 派发前预建持久作业, 让客户端可轮询同一 job、并发重试不会重复入队。
    # - 数据管理重试可带幂等键 (复用/去重)。
    # - 工作台单题请求 (无幂等键) 也预建, 使 worker 在派发前的范围复校验失败时
    #   on_failure 能按 celery_task_id 落一条 failed 作业 (issue #121 review)。
    from app.services import async_job as async_job_svc
    from app.services.data_management.actions import (
        assert_idempotent_request_matches,
        find_idempotent_job,
        lock_idempotency_key,
        normalize_idempotency_key,
        request_digest as data_manager_request_digest,
    )

    idempotency_key = normalize_idempotency_key(idempotency_key)
    precreated_job: AsyncJob | None = None
    if idempotency_key is not None:
        canonical_body = body.model_dump(mode="json")
        canonical_body["task_ids"] = (
            [str(task_id) for task_id in explicit_task_ids]
            if explicit_task_ids is not None
            else None
        )
        request_digest_value = data_manager_request_digest(canonical_body)
        await lock_idempotency_key(
            db,
            action="preannotate",
            project_id=project.id,
            actor_id=current_user.id,
            key=idempotency_key,
        )
        existing = await find_idempotent_job(
            db,
            kind="batch_predict",
            project_id=project.id,
            actor_id=current_user.id,
            key=idempotency_key,
        )
        if existing is not None:
            assert_idempotent_request_matches(existing, digest=request_digest_value)
            if existing.status != "pending":
                return {
                    "job_id": str(existing.id),
                    "status": existing.status,
                    "total_tasks": (existing.payload or {}).get("total_tasks"),
                    "channel": f"project:{project.id}:preannotate",
                    "celery_task_id": existing.celery_task_id,
                }
            # A request may have crashed after committing this pending row but
            # before publishing to Celery. Reuse the row and safely republish;
            # the worker's row lock makes a duplicate delivery a no-op.
            precreated_job = existing
            if uses_controlled_model:
                frozen_model_ref = (existing.payload or {}).get("frozen_model_ref")
                if not frozen_model_ref:
                    raise HTTPException(
                        status_code=409, detail="原任务缺少冻结模型身份"
                    )
        else:
            scoped_ids = (
                [str(task_id) for task_id in explicit_task_ids]
                if explicit_task_ids is not None
                else None
            )
            precreated_payload: dict = {
                "batch_id": str(body.batch_id) if body.batch_id else None,
                "execution_scope": body.execution_scope,
                "ml_backend_id": str(source_backend_id),
                "total_tasks": (
                    len(scoped_ids) if scoped_ids is not None else total_tasks_hint
                ),
                "project_display_id": project.display_id,
                "project_name": project.name,
                "data_manager_idempotency_key": idempotency_key,
                "data_manager_request_digest": request_digest_value,
            }
            if scoped_ids is not None:
                precreated_payload["scope"] = {"task_ids": scoped_ids}
                precreated_payload["task_ids"] = scoped_ids
            if uses_controlled_model:
                frozen_model_ref = await freeze_controlled_model()
                precreated_payload["frozen_model_ref"] = frozen_model_ref
            precreated_job = await async_job_svc.create_job(
                db,
                kind="batch_predict",
                user_id=current_user.id,
                project_id=project.id,
                payload=precreated_payload,
            )
        # Use the durable async job id as the Celery task id. The failure hook
        # can therefore resolve a job even if the worker fails before the API
        # request reaches its post-dispatch commit.
        async_job_svc.bind_execution_id(precreated_job, str(precreated_job.id))
        await db.commit()
    elif (
        body.execution_scope == "workbench" and explicit_task_ids is not None
    ) or uses_controlled_model:
        # issue #121 review · 工作台单题请求 (无幂等键) 同样预建持久作业: worker 在派发
        # 前的范围复校验失败时, on_failure 能按 celery_task_id 落一条 failed 作业,
        # 工作台面板不会因找不到终态记录而无限轮询。
        scoped_ids = (
            [str(task_id) for task_id in explicit_task_ids]
            if explicit_task_ids is not None
            else None
        )
        if uses_controlled_model:
            frozen_model_ref = await freeze_controlled_model()
        precreated_job = await async_job_svc.create_job(
            db,
            kind="batch_predict",
            user_id=current_user.id,
            project_id=project.id,
            payload={
                "batch_id": str(body.batch_id) if body.batch_id else None,
                "execution_scope": body.execution_scope,
                "ml_backend_id": str(source_backend_id),
                "total_tasks": len(scoped_ids)
                if scoped_ids is not None
                else total_tasks_hint,
                "project_display_id": project.display_id,
                "project_name": project.name,
                **(
                    {"scope": {"task_ids": scoped_ids}, "task_ids": scoped_ids}
                    if scoped_ids is not None
                    else {}
                ),
                **({"frozen_model_ref": frozen_model_ref} if frozen_model_ref else {}),
            },
        )
        async_job_svc.bind_execution_id(precreated_job, str(precreated_job.id))
        await db.commit()

    soft_time_limit = (
        settings.tracker_soft_time_limit_seconds if has_tracker_stage else None
    )
    batch_request = BatchPredictionRequest(
        project_id=str(project.id),
        ml_backend_id=str(source_backend_id),
        task_ids=(
            [str(tid) for tid in explicit_task_ids] if explicit_task_ids else None
        ),
        prompt=body.prompt,
        output_mode=body.output_mode,
        batch_id=str(body.batch_id) if body.batch_id else None,
        user_id=str(current_user.id),
        params=body.params or None,
        predict_mode=body.predict_mode,
        # v0.14.9 · protocol v2 resolves the durable controlled model identity here.
        model_id=(
            f"trained:{frozen_model_ref['model_version_id']}"
            if frozen_model_ref
            else body.model_id
        ),
        task=body.task,
        prompt_type=body.prompt_type,
        model_variants=body.model_variants,
        class_filter=body.class_filter,
        pipeline_stages=pipeline_stages_payload,
        execution_unit=execution_unit,
        execution_scope=body.execution_scope,
        async_job_id=str(precreated_job.id) if precreated_job is not None else None,
        frozen_model_ref=frozen_model_ref,
    )
    try:
        dispatched_id = batch_dispatcher.dispatch(
            batch_request,
            queue=queue,
            soft_time_limit=soft_time_limit,
        )
    except Exception as exc:
        if precreated_job is not None:
            await async_job_svc.mark_failed(
                db,
                precreated_job.id,
                error=f"dispatch failed: {type(exc).__name__}: {exc}",
            )
            await db.commit()
        raise
    if precreated_job is not None:
        # ``task_id`` above is the durable async job id; keep that identity even
        # if a test broker returns a different result wrapper id.
        async_job_svc.bind_execution_id(precreated_job, str(precreated_job.id))
    # B-5 · AI 预标注触发审计 — 让超管在 /audit 看到 谁/何时/对哪个 batch 跑了 AI
    await AuditService.log(
        db,
        actor=current_user,
        action="ai.preannotate.triggered",
        target_type="project",
        target_id=str(project.id),
        request=request,
        status_code=200,
        detail={
            "job_id": str(precreated_job.id) if precreated_job else dispatched_id,
            "ml_backend_id": str(source_backend_id),
            "batch_id": str(body.batch_id) if body.batch_id else None,
            "task_count": (
                len(explicit_task_ids) if explicit_task_ids else total_tasks_hint
            ),
            "prompt": (body.prompt or "")[:200],
            # output_mode 仅文本 prompt 路径生效; 几何路径不读它, 留 None 免误导 (与 job payload 一致)
            "output_mode": body.output_mode if body.prompt else None,
            # v0.14.10 · 协议 v2 多模型路由溯源: 记录具体 model / task 类型
            # v0.14.18 · 增记 model_variants (实际 variant, 如 series/size=yolov8/l)
            "model_id": body.model_id,
            "task": body.task,
            "prompt_type": body.prompt_type,
            "model_variants": body.model_variants,
        },
    )
    await db.commit()
    # v0.20.21 · both 多阶段引导: output=both 仅文本 prompt 路径生效 (见上文 job payload 注释),
    # 且会对同一实例产出框+多边形两条几何; 若还配了下游阶段, 编排会把两条各当一个父框各处理
    # 一次 (重复裁剪/推理/富集) 并落两条 region。软提示、不拦截 (both 仍可跑)。
    warnings: list[str] = []
    if body.output_mode == "both" and body.prompt and body.pipeline_stages:
        msg = (
            "源阶段 output=both 会对同一实例产出「框 + 多边形」两条几何, 多阶段下游会各处理"
            "一次 (重复裁剪 / 推理 / 富集) 并落两条 region; 建议源阶段改用 box 或 mask。"
        )
        warnings.append(msg)
        logger.warning("[ai-pre] %s", msg)
    return {
        "job_id": str(precreated_job.id) if precreated_job else dispatched_id,
        "status": "queued",
        "total_tasks": total_tasks_hint,
        "channel": f"project:{project.id}:preannotate",
        **({"celery_task_id": dispatched_id} if precreated_job else {}),
        "warnings": warnings,
    }


@router.get("/{project_id}/orphan-tasks/preview")
async def preview_orphan_tasks(
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
):
    """v0.6.7 二修 B-10：预览本项目中「无源 task」（dataset_item_id 为空 / 指向已 unlink 的数据集 / 指向已删除 dataset_item）。"""
    from sqlalchemy import select, func
    from app.db.models.task import Task
    from app.db.models.dataset import DatasetItem
    from app.db.models.annotation import Annotation
    from app.db.models.dataset import ProjectDataset

    # 孤儿条件：tasks 不存在仍 link 着的 (dataset_item → dataset → project_datasets)
    orphan_task_ids = (
        (
            await db.execute(
                select(Task.id).where(
                    Task.project_id == project.id,
                    ~Task.id.in_(
                        select(Task.id)
                        .join(DatasetItem, DatasetItem.id == Task.dataset_item_id)
                        .join(
                            ProjectDataset,
                            (ProjectDataset.dataset_id == DatasetItem.dataset_id)
                            & (ProjectDataset.project_id == project.id),
                        )
                        .where(Task.project_id == project.id)
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    task_count = len(orphan_task_ids)
    ann_count = 0
    if orphan_task_ids:
        ann_count = (
            await db.execute(
                select(func.count(Annotation.id)).where(
                    Annotation.task_id.in_(list(orphan_task_ids))
                )
            )
        ).scalar() or 0
    return {"orphan_tasks": task_count, "orphan_annotations": int(ann_count)}


@router.post("/{project_id}/orphan-tasks/cleanup")
async def cleanup_orphan_tasks(
    request: Request,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """v0.6.7 二修 B-10：删除本项目所有「无源 task」（含 annotations / locks / comments），重算 counters。

    适用场景：v0.6.0~v0.6.6 期间 link_project 写 dataset_item_id 但未持久化 batch 关系，
    后续数据集被 unlink / 删除留下的孤儿 task。
    """
    result = await cleanup_source_less_tasks(db, project.id)
    if result["deleted_tasks"] == 0:
        return result

    from app.services.audit import AuditService

    await AuditService.log(
        db,
        actor=current_user,
        action="project.cleanup_orphans",
        target_type="project",
        target_id=str(project.id),
        request=request,
        status_code=200,
        detail=result,
    )
    await db.commit()
    return result


# ── v0.7.3 · 项目侧关联数据集 ────────────────────────────────────────────


@router.get("/{project_id}/datasets")
async def list_project_datasets(
    project_id: uuid.UUID,
    project: Project = Depends(require_project_visible),
    db: AsyncSession = Depends(get_db),
):
    """列出本项目已关联的所有数据集（含基础元数据 + task 数）。"""
    from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
    from app.db.models.task import Task

    rows = (
        await db.execute(
            select(Dataset, ProjectDataset.created_at.label("linked_at"))
            .join(ProjectDataset, ProjectDataset.dataset_id == Dataset.id)
            .where(ProjectDataset.project_id == project_id)
            .order_by(ProjectDataset.created_at.desc())
        )
    ).all()

    if not rows:
        return []

    ds_ids = [r[0].id for r in rows]
    item_counts = dict(
        (
            await db.execute(
                select(DatasetItem.dataset_id, func.count())
                .where(DatasetItem.dataset_id.in_(ds_ids))
                .group_by(DatasetItem.dataset_id)
            )
        ).all()
    )
    task_counts = dict(
        (
            await db.execute(
                select(DatasetItem.dataset_id, func.count())
                .join(Task, Task.dataset_item_id == DatasetItem.id)
                .where(
                    Task.project_id == project_id, DatasetItem.dataset_id.in_(ds_ids)
                )
                .group_by(DatasetItem.dataset_id)
            )
        ).all()
    )

    return [
        {
            "id": str(d.id),
            "display_id": d.display_id,
            "name": d.name,
            "data_type": d.data_type,
            "linked_at": linked_at.isoformat() if linked_at else None,
            "items_count": int(item_counts.get(d.id, 0)),
            "tasks_in_project": int(task_counts.get(d.id, 0)),
        }
        for d, linked_at in rows
    ]
