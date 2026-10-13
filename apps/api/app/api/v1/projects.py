import logging
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Literal
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, text, or_, and_, update
from app.deps import (
    get_db,
    get_current_user,
    require_roles,
    require_project_visible,
    require_project_owner,
    require_project_capability,
    assert_project_visible,
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
    change_role as change_project_member_role,
    preview_role_change as preview_project_member_role_change,
    remove_member as remove_project_member,
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
from app.schemas.export import (
    ExportRequestBody,
    LidarExportPreflightRequest,
    LidarExportPreflightResponse,
)
from app.config import settings
from app.services.display_id import next_display_id
from app.services.pipeline_validation import (
    check_capability_violations,
    check_parent_geometry_roi,
    resolve_preannotate_queue,
)
from app.services.capability_registry import INPUT_BBOX_PROMPT, INPUT_CROP
from app.services.batch_permissions import allows_bulk_preannotation
from app.services.pipeline_template import (
    assert_pipeline_visible,
    copy_pipeline_stages,
    switch_project_default_pipeline,
    unenabled_backend_ids,
)
from app.services.project_kind import (
    ProjectKind,
    canonical_media_kind,
    dataset_has_scenes,
    dataset_kind,
    kind_mismatch_detail,
    scene_mode_allowed,
)
from app.services.project_clone import (
    CLONEABLE_PROJECT_FIELDS as _CLONEABLE_PROJECT_FIELDS,  # noqa: F401 (re-export)
    merge_from_source as _merge_from_source_project_impl,
)
from app.services.project_delete import delete_project_records
from app.schemas.project import VideoCollaborationConfig
from app.services.video_collaboration import validate_project_collaboration_update

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


async def _assert_project_kind_update_allowed(
    db: AsyncSession, project: Project, payload: dict
) -> None:
    if "scene_mode" in payload and payload["scene_mode"] != project.scene_mode:
        if (project.total_tasks or 0) > 0:
            raise HTTPException(
                status_code=422,
                detail="已建 task 的项目不可切换 scene 模式,请先解绑数据集",
            )

    target_data_type = payload.get("data_type", project.data_type)
    target_scene_mode = bool(payload.get("scene_mode", project.scene_mode))
    if target_scene_mode and not scene_mode_allowed(target_data_type):
        raise HTTPException(
            status_code=422,
            detail="scene_mode 仅支持 image/lidar 项目",
        )

    if "data_type" not in payload and "scene_mode" not in payload:
        return

    from app.db.models.dataset import Dataset, ProjectDataset

    rows = await db.execute(
        select(Dataset)
        .join(ProjectDataset, ProjectDataset.dataset_id == Dataset.id)
        .where(ProjectDataset.project_id == project.id)
    )
    target = ProjectKind(
        data_type=canonical_media_kind(target_data_type),
        scene_mode=target_scene_mode,
    )
    for dataset in rows.scalars().all():
        has_scenes = await dataset_has_scenes(db, dataset.id)
        mismatch = kind_mismatch_detail(
            target, dataset_kind(dataset, has_scenes=has_scenes)
        )
        if mismatch:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"已有数据集 {dataset.display_id} 与目标项目 kind 不匹配: "
                    f"{mismatch}; 请先解绑数据集"
                ),
            )


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
    # v0.10.11 · exclude_unset 让"未显式给出"与"显式给出默认值"可区分; 兜底字段
    # 优先用 source_project_id 项目的值, 其次走 Project 模型列默认值.
    payload = data.model_dump(exclude_unset=True)

    # v0.10.28 · B 路线: 新建以 data_type 为主. type_key 缺省时由 data_type 派生兼容值;
    # data_type 缺省时由 type_key 反推, 二者互填后均落库 (Project.type_key NOT NULL).
    from app.services.project import (
        assert_project_kind_consistent,
        data_type_from_type_key,
        legacy_type_key_from_data_type,
    )

    _dt = payload.get("data_type")
    _tk = payload.get("type_key")
    # 两侧都给时先断言一致 (cross-fill 只补缺, 不审已给值).
    assert_project_kind_consistent(_tk, _dt)
    if not _tk:
        payload["type_key"] = legacy_type_key_from_data_type(_dt)
    if not _dt:
        payload["data_type"] = data_type_from_type_key(payload.get("type_key"))
    if payload.get("scene_mode"):
        if not scene_mode_allowed(payload.get("data_type")):
            raise HTTPException(
                status_code=422,
                detail="scene_mode 仅支持 image/lidar 项目",
            )
        payload.setdefault("prefer_same_scene_continuation", True)
    if "video_collaboration" in payload:
        await validate_project_collaboration_update(
            db,
            None,
            VideoCollaborationConfig.model_validate(payload["video_collaboration"]),
            data_type=payload["data_type"],
            requested_video_sampling=payload.get("video_sampling"),
            requested_tool_bindings=payload.get("tool_bindings"),
        )
        if (
            payload["video_collaboration"]["enabled"]
            and payload["type_key"] != "video-track"
        ):
            raise HTTPException(
                status_code=422,
                detail="video_collaboration 只能用于 video-track 项目",
            )

    # v0.10.22 · 先把客户端显式传入的旧扁平 classes / classes_config / attribute_schema
    # 反向派生进 tool_bindings (按 type_key 推 unit), 再剔除扁平 key. 必须早于下面的
    # source / template 兜底合并 —— 它们只填"缺失"的 tool_bindings, 故显式输入优先;
    # 同时扁平字段对应 DB 列已删, 不能作为 ORM kwarg.
    from app.services.project import coalesce_legacy_into_tool_bindings

    coalesce_legacy_into_tool_bindings(
        payload, existing_tool_bindings=None, type_key=payload.get("type_key")
    )
    for _legacy_key in ("classes", "classes_config", "attribute_schema"):
        payload.pop(_legacy_key, None)

    # v0.9.7 · 取出 source_id (若给定), 校验存在性后再创建项目;
    # 项目 INSERT 完成后再为新项目启用 backend (项目 → project_ml_backend FK).
    source_id = payload.pop("ml_backend_source_id", None)
    # v0.23.3 ADR-0050 · 公共 schema 仍用 ml_backend_id (registry id, 兼容前端);
    # 内部转成 ml_backend_pool_id (singleton pool id) 存进项目主绑定。
    requested_main_backend_id = payload.pop("ml_backend_id", None)
    source_backend = None
    if source_id is not None:
        from app.db.models.ml_backend_registry import MLBackendRegistry as _MLB

        source_backend = await db.get(_MLB, source_id)
        if source_backend is None:
            raise HTTPException(
                status_code=400, detail="ml_backend_source_id not found"
            )

    # v0.10.14 · E2 · "从模板创建" — schema 已校验 template_id / source_project_id
    # 互斥. 应用模板时把模板载荷 deepcopy 进 payload, 并 usage_count += 1.
    template_id = payload.pop("template_id", None)

    # v0.10.11 · "从已有项目复制配置" — 用源项目兜底未显式给出的字段;
    # 若源项目带 ml_backend_id 且 caller 未单独指定 ml_backend_source_id,
    # 自动把源项目的 backend 作为 backend 克隆源.
    source_project_id = payload.pop("source_project_id", None)
    # v0.10.13 · E1 · annotation_guide / guide_assets 不在默认克隆白名单 (避免信息错位);
    # 通过独立 flag 触发, 仅当 source_project_id 给定时有效. 图片资源 storage key
    # 复用 (不重新上传) — 源项目删除资源会影响新项目, UI 已在 wizard 标注.
    copy_guide = payload.pop("copy_annotation_guide", False)
    if source_project_id is not None:
        source_project = await assert_project_visible(
            source_project_id, db, current_user
        )
        payload = _merge_from_source_project(payload, source_project)
        if copy_guide:
            import copy as _copy

            if source_project.annotation_guide is not None:
                payload.setdefault("annotation_guide", source_project.annotation_guide)
            if source_project.guide_assets:
                payload.setdefault(
                    "guide_assets", _copy.deepcopy(source_project.guide_assets)
                )
        if source_backend is None and source_project.ml_backend_pool_id is not None:
            from app.db.models.ml_backend_pool import MLBackendServicePool

            # v0.23.3 ADR-0050 · 项目主绑定存 pool id; 经 pool.legacy_instance_id 解析
            # 回 registry 实例 (off mode singleton, 与 v0.23.2 clone 行为一致)。
            src_pool = await db.get(
                MLBackendServicePool, source_project.ml_backend_pool_id
            )
            if src_pool is not None and src_pool.legacy_instance_id is not None:
                from app.db.models.ml_backend_registry import MLBackendRegistry as _MLB

                source_backend = await db.get(_MLB, src_pool.legacy_instance_id)
    elif copy_guide:
        # 没给 source_project_id 却给了 copy_annotation_guide 是请求体错误, 提示前端
        raise HTTPException(
            status_code=400,
            detail="copy_annotation_guide requires source_project_id",
        )

    template = None
    if template_id is not None:
        from app.db.models.project_template import ProjectTemplate
        from app.services.project_template import (
            assert_template_visible,
            merge_template_into_payload,
        )

        template = await db.get(ProjectTemplate, template_id)
        if template is None:
            raise HTTPException(status_code=404, detail="模板不存在")
        await assert_template_visible(db, template, current_user)
        payload = merge_template_into_payload(template, payload)

    if requested_main_backend_id:
        # v0.10.37 · 创建即绑定 backend 时同样按 data_type 校验模态 (与 update_project 对称)
        await _validate_backend_modality(
            db, requested_main_backend_id, payload["data_type"]
        )

    new_project_id = uuid.uuid4()
    # v0.19.0 ADR-0044 · 直接指定主 backend (非克隆路径) 时, 同步在新项目建启用关联;
    # 不然 project_ml_backend_pool 缺行, trigger_preannotation 的 is_enabled 校验 404 +
    # ai_enabled 又派生为 true → 工作台显示「已启用 AI 却跑不起来」。
    main_backend_to_enable = (
        requested_main_backend_id if source_backend is None else None
    )
    project = Project(
        id=new_project_id,
        display_id=await next_display_id(db, "projects"),
        owner_id=current_user.id,
        **payload,
    )
    db.add(project)
    await db.flush()  # 让 project row 入 DB, 满足 project_ml_backend FK

    if source_backend is not None:
        new_pool_id = await _clone_backend_to_new_project(
            db, source=source_backend, new_project_id=new_project_id
        )
        project.ml_backend_pool_id = new_pool_id
        # v0.10.37 · 克隆源项目 backend 落定后, 同样按新项目 data_type 校验模态
        # (clone 复制了 url/auth, 实时探 /setup 与校验 source 等价).
        await _validate_backend_modality(db, source_backend.id, project.data_type)
    elif main_backend_to_enable is not None:
        from app.services.ml_backend import MLBackendService

        svc = MLBackendService(db)
        await svc.set_enabled(new_project_id, main_backend_to_enable, enabled=True)
        # v0.23.3 ADR-0050 · 项目主绑定存 pool id; set_enabled 内部已解析 registry→pool。
        pool = await svc._pool_for_registry(main_backend_to_enable)
        if pool is not None:
            project.ml_backend_pool_id = pool.id

    if template is not None:
        template.usage_count = (template.usage_count or 0) + 1

    await db.commit()
    await db.refresh(project)
    if template is not None:
        # 测试环境共享 dependency-override session, 后续 GET /project-templates/{id}
        # 会从 identity map 取出本 template; commit 后属性可能进入"待重新加载"
        # 状态触发 sync I/O. 显式 refresh 保证下次访问不触发 lazy load.
        await db.refresh(template)
    return await _serialize_project(db, project)


async def _clone_backend_to_new_project(
    db: AsyncSession, *, source, new_project_id: uuid.UUID
) -> uuid.UUID:
    """v0.19.0 ADR-0044 · backend 已是全局注册项, 「克隆给新项目」退化为「为新项目启用同一
    全局 backend」: 建一条 project_ml_backend_pool 关联。返回项目主绑定的 pool id。
    v0.23.3 ADR-0050 · 返回 singleton pool id (项目主绑定存 pool, 非 registry)。
    """
    from app.services.ml_backend import MLBackendService

    svc = MLBackendService(db)
    await svc.set_enabled(new_project_id, source.id, enabled=True)
    pool = await svc._pool_for_registry(source.id)
    # source 必经 create_registry / singleton backfill, 故 pool 必然存在。
    return pool.id if pool is not None else source.id


# v0.10.14 · E2 · 白名单 + merge 实现迁出至 app.services.project_clone, 供
# projects + project_templates 共享. 这里保留同名 wrapper, 不改调用点.
def _merge_from_source_project(payload: dict, source: Project) -> dict:
    return _merge_from_source_project_impl(payload, source)


async def _validate_backend_modality(
    db: AsyncSession, backend_id, data_type: str
) -> None:
    """v0.10.37 · 绑定 backend 时按项目 data_type 校验模态匹配 (epic 阶段 1).

    实时探一次 `/setup` 派生 backend 模态; fail-open: 探测失败 (backend 暂不可达) → 放行,
    不因瞬时宕机卡住绑定, mismatch 留到 predict 时暴露. lidar 暂无 backend 支持, 跳过校验.
    """
    if data_type not in ("image", "video"):
        return
    from app.db.models.ml_backend_registry import MLBackendRegistry as _MLB
    from app.services.ml_capabilities import derive_modalities, extract_capabilities
    from app.services.ml_client import MLBackendClient

    backend = await db.get(_MLB, backend_id)
    if backend is None:
        return
    try:
        caps = extract_capabilities(await MLBackendClient(backend).setup())
    except Exception:
        return  # fail-open
    if caps is None:
        return
    modalities = derive_modalities(caps)
    if not modalities:
        return  # 能力快照不含模态信号 (无 prompt/tracker) → fail-open, 不误拦纯批量检测后端
    if data_type not in modalities:
        raise HTTPException(
            status_code=422,
            detail=(
                f"该 ML Backend 不支持「{data_type}」模态 (检测到: "
                f"{modalities or '无'}); 视频项目需绑定自报 supported_trackers 的 backend."
            ),
        )


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
):
    payload = data.model_dump(exclude_unset=True)
    if (
        "video_collaboration" in payload
        or "video_sampling" in payload
        or "tool_bindings" in payload
    ):
        requested_collaboration = VideoCollaborationConfig.model_validate(
            payload.get("video_collaboration", project.video_collaboration or {})
        )
        await validate_project_collaboration_update(
            db,
            project,
            requested_collaboration,
            data_type=payload.get("data_type", project.data_type),
            video_sampling_changed=(
                "video_sampling" in payload
                and payload["video_sampling"] != (project.video_sampling or {})
            ),
            requested_video_sampling=payload.get(
                "video_sampling", project.video_sampling or {}
            ),
            requested_tool_bindings=payload.get(
                "tool_bindings", project.tool_bindings or {}
            ),
        )
        if (
            requested_collaboration.enabled
            and payload.get("type_key", project.type_key) != "video-track"
        ):
            raise HTTPException(
                status_code=422,
                detail="video_collaboration 只能用于 video-track 项目",
            )
    if "mask_qc_config" in payload:
        from app.services.mask_qc.config import load_mask_qc_config

        project = (
            await db.execute(
                select(Project).where(Project.id == project.id).with_for_update()
            )
        ).scalar_one()
        current_config = load_mask_qc_config(project.mask_qc_config)
        requested_config = load_mask_qc_config(payload["mask_qc_config"])
        if requested_config.config_revision != current_config.config_revision:
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "mask_qc_config_revision_conflict",
                    "expected": requested_config.config_revision,
                    "actual": current_config.config_revision,
                },
            )
        requested_config.config_revision = current_config.config_revision + 1
        payload["mask_qc_config"] = requested_config.model_dump(mode="json")
    if "point_cloud_quality_config" in payload:
        from app.services.point_cloud_quality.config import (
            load_point_cloud_quality_config,
        )

        project = (
            await db.execute(
                select(Project).where(Project.id == project.id).with_for_update()
            )
        ).scalar_one()
        current_config = load_point_cloud_quality_config(
            project.point_cloud_quality_config
        )
        requested_config = load_point_cloud_quality_config(
            payload["point_cloud_quality_config"]
        )
        if requested_config.config_revision != current_config.config_revision:
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "point_cloud_quality_config_revision_conflict",
                    "expected": requested_config.config_revision,
                    "actual": current_config.config_revision,
                },
            )
        requested_config.config_revision = current_config.config_revision + 1
        payload["point_cloud_quality_config"] = requested_config.model_dump(mode="json")
    # v0.13.x 收口 PR#30 review #5: type_key 与 data_type 媒体维度必须一致.
    # 单独 PATCH 任一字段也要校验 (用 payload 给值 + 项目现值组合后的有效值).
    if "type_key" in payload or "data_type" in payload:
        from app.services.project import assert_project_kind_consistent

        assert_project_kind_consistent(
            payload.get("type_key", project.type_key),
            payload.get("data_type", project.data_type),
        )
    await _assert_project_kind_update_allowed(db, project, payload)
    # v0.18.27 · 项目级编排结构校验 (显式 null = 清除, 跳过校验直接置 None)。
    if payload.get("preannotate_pipeline") is not None:
        _validate_saved_pipeline(payload["preannotate_pipeline"])
    # v0.23.3 ADR-0050 · 公共 schema 仍用 ml_backend_id (registry id);
    # 内部转成 ml_backend_pool_id (singleton pool) 存项目主绑定。
    # 注意区分「字段未提供」(不改动) 与「显式 null」(清空): 用 in 判断而非 pop 默认值。
    if "ml_backend_id" in payload:
        requested_main_backend_id = payload.pop("ml_backend_id")
        if requested_main_backend_id:
            # v0.10.37 · 绑定按 data_type 校验模态 (用应用 payload 后的有效 data_type)
            await _validate_backend_modality(
                db,
                requested_main_backend_id,
                payload.get("data_type") or project.data_type,
            )
            # v0.19.0 ADR-0044 · 设主 backend 即视为「本项目启用该 backend」;
            # 否则 trigger_preannotation 的 is_enabled 校验 404, 而 ai_enabled 又自动派生 true
            # → 工作台显示「已启用 AI 却跑不起来」。与 PUT /ml-backends/{rid}/enablement 对称。
            from app.services.ml_backend import MLBackendService

            svc = MLBackendService(db)
            await svc.set_enabled(project.id, requested_main_backend_id, enabled=True)
            pool = await svc._pool_for_registry(requested_main_backend_id)
            payload["ml_backend_pool_id"] = pool.id if pool is not None else None
        else:
            # 显式 null = 清空主绑定
            payload["ml_backend_pool_id"] = None

    # v0.10.22 · 同 create_project: 旧扁平输入反向派生进 tool_bindings 后剔除.
    from app.services.project import coalesce_legacy_into_tool_bindings

    coalesce_legacy_into_tool_bindings(
        payload,
        existing_tool_bindings=project.tool_bindings,
        type_key=payload.get("type_key") or project.type_key,
    )
    for _legacy_key in ("classes", "classes_config", "attribute_schema"):
        payload.pop(_legacy_key, None)

    for k, v in payload.items():
        setattr(project, k, v)
    await db.commit()
    await db.refresh(project)
    result = await _serialize_project(db, project)
    # v0.19.3 WS1 · 保存编排能力软提示 (不挡, dispatch-time 422 仍是最终闸)。
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
    _validate_saved_pipeline(pipeline.stages)

    missing = await unenabled_backend_ids(db, project.id, pipeline.stages)
    if missing:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "编排引用的 backend 未在当前项目启用",
                "unenabled_backends": missing,
            },
        )

    new_pipeline = ProjectPipeline(
        id=uuid.uuid4(),
        scope="private",
        project_id=project.id,
        organization_id=None,
        name=pipeline.name,
        stages=copy_pipeline_stages(pipeline.stages),
        is_default=False,
        created_by=current_user.id,
    )
    # 原子自增, 避免并发套用同一源编排时的 read-modify-write 丢更新。
    await db.execute(
        update(ProjectPipeline)
        .where(ProjectPipeline.id == pipeline.id)
        .values(usage_count=func.coalesce(ProjectPipeline.usage_count, 0) + 1)
    )
    if body.set_default:
        await switch_project_default_pipeline(db, project.id)
        new_pipeline.is_default = True
    db.add(new_pipeline)
    await db.flush()
    await db.commit()
    await db.refresh(new_pipeline)
    return new_pipeline


@router.delete("/{project_id}", status_code=204)
async def delete_project(
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
):
    await delete_project_records(db, project)
    await db.commit()
    return Response(status_code=204)


class ClassRenameRequest(BaseModel):
    old_name: str
    new_name: str
    # v0.10.17 · 工具单位限定; 不传时为兼容 (旧客户端) 在所有 enabled unit 内同名类一起改.
    tool_unit_id: str | None = None


@router.post("/{project_id}/classes/rename", response_model=ProjectOut)
async def rename_class(
    body: ClassRenameRequest,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
):
    """B-13 · 原子地把类别 old_name 重命名为 new_name:
    - 更新 tool_bindings 中对应 unit (或所有 unit) 的 classes[].name (强隔离 · 仅本 unit)
    - **始终跨 unit** 改全项目内同名 annotations.class_name (避免历史 magic-box /
      region / 旧 schema 残留留下"孤儿"标注: binding 已无该类, 但 annotation 仍引用)
    - 不动 predictions.result (alias 不变)
    """
    old = body.old_name.strip()
    new = body.new_name.strip()
    if not old or not new:
        raise HTTPException(status_code=400, detail="名称不能为空")
    if old == new:
        return await _serialize_project(db, project)

    tool_bindings = dict(project.tool_bindings or {})
    target_units: list[str]
    if body.tool_unit_id is not None:
        if body.tool_unit_id not in tool_bindings:
            raise HTTPException(
                status_code=404, detail=f"tool_unit_id '{body.tool_unit_id}' 不存在"
            )
        target_units = [body.tool_unit_id]
    else:
        target_units = list(tool_bindings.keys())

    found_any = False
    for unit_id in target_units:
        binding = tool_bindings.get(unit_id)
        if not isinstance(binding, dict):
            continue
        classes_list = list(binding.get("classes") or [])
        names = [c.get("name") for c in classes_list if isinstance(c, dict)]
        if old not in names:
            continue
        if new in names:
            raise HTTPException(
                status_code=409,
                detail=f"类别 '{new}' 在工具单位 '{unit_id}' 已存在",
            )
        renamed = []
        for c in classes_list:
            if isinstance(c, dict) and c.get("name") == old:
                renamed.append({**c, "name": new})
            else:
                renamed.append(c)
        tool_bindings[unit_id] = {**binding, "classes": renamed}
        found_any = True

    if not found_any:
        raise HTTPException(status_code=404, detail=f"类别 '{old}' 不存在")

    project.tool_bindings = tool_bindings

    # 同步 annotations.class_name: 始终跨 unit 全项目改 (即使本次只动了一个 unit 的
    # binding). 强隔离仅适用于 binding 元数据; annotations 是面向最终用户的可见框,
    # 同一名字落到 magic-box / region / 旧 schema 等其他 unit 的标注若不一起改, 会
    # 在工作台显示成"老框没改名"的孤儿数据.
    await db.execute(
        text(
            "UPDATE annotations SET class_name = :new "
            "WHERE project_id = :pid AND class_name = :old"
        ),
        {"pid": str(project.id), "old": old, "new": new},
    )
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
    from app.db.models.annotation import Annotation
    from app.db.models.task import Task
    from app.services.project import (
        derive_attribute_keys,
        derive_classes_list,
        orphan_user_attribute_keys,
        prune_orphan_user_attributes,
    )

    dry_run = True if body is None else body.dry_run
    class_names = set(derive_classes_list(project.tool_bindings))
    attribute_keys = derive_attribute_keys(project.tool_bindings)

    annotations = list(
        (
            await db.execute(
                select(Annotation).where(
                    Annotation.project_id == project.id,
                    Annotation.is_active.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )

    orphan_annotations = 0
    orphan_attribute_keys: Counter[str] = Counter()
    mutation_annotation_ids: set[uuid.UUID] = set()
    mutation_task_ids: set[uuid.UUID] = set()

    for ann in annotations:
        if ann.class_name not in class_names:
            orphan_annotations += 1
            if not dry_run:
                mutation_annotation_ids.add(ann.id)
                mutation_task_ids.add(ann.task_id)
            continue

        orphan_keys = orphan_user_attribute_keys(ann.attributes, attribute_keys)
        if not orphan_keys:
            continue
        orphan_attribute_keys.update(orphan_keys)
        if not dry_run:
            mutation_annotation_ids.add(ann.id)
            mutation_task_ids.add(ann.task_id)

    if not dry_run:
        # Keep the global writer order used by Mask mutations and PATCH:
        # stable Task locks first, then stable Annotation locks.
        if mutation_task_ids:
            await db.execute(
                select(Task.id)
                .where(Task.id.in_(mutation_task_ids))
                .order_by(Task.id)
                .with_for_update()
            )
        locked_annotations = []
        if mutation_annotation_ids:
            locked_annotations = list(
                (
                    await db.execute(
                        select(Annotation)
                        .where(Annotation.id.in_(mutation_annotation_ids))
                        .order_by(Annotation.id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                )
                .scalars()
                .all()
            )
        deactivated_task_ids: set[uuid.UUID] = set()
        for ann in locked_annotations:
            if ann.class_name not in class_names:
                ann.is_active = False
                deactivated_task_ids.add(ann.task_id)
                continue
            ann.attributes = prune_orphan_user_attributes(
                ann.attributes,
                attribute_keys,
            )
        await db.flush()
        if mutation_task_ids:
            from app.services.annotation_evidence import (
                record_annotation_actors_for_tasks,
            )

            # A2 · this cleanup writes annotation attributes / deactivations under
            # task locks already held above, so accumulate the actor here too.
            await record_annotation_actors_for_tasks(
                db, mutation_task_ids, current_user.id
            )
        if deactivated_task_ids:
            from app.services.annotation import AnnotationService

            annotation_svc = AnnotationService(db)
            for task_id in sorted(deactivated_task_ids):
                await annotation_svc._update_task_stats(task_id)

        from app.services.audit import AuditService

        await AuditService.log(
            db,
            actor=current_user,
            action="project.cleanup_annotation_orphans",
            target_type="project",
            target_id=str(project.id),
            request=request,
            status_code=200,
            detail={
                "orphan_annotations": orphan_annotations,
                "orphan_attribute_keys": dict(orphan_attribute_keys),
            },
        )
        await db.commit()

    return ProjectCleanupOrphansOut(
        orphan_annotations=orphan_annotations,
        orphan_attribute_keys=dict(orphan_attribute_keys),
    )


@router.post("/{project_id}/transfer", response_model=ProjectOut)
async def transfer_owner(
    body: ProjectTransferRequest,
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    # Account-first, then the project row — the same order as the platform-role
    # mutation, so a concurrent demotion of the target cannot interleave with
    # the role check and the ownership write.
    from app.services.user_lifecycle import UserLifecycleService

    accounts = await UserLifecycleService.lock_accounts(db, [body.new_owner_id])
    target = accounts.get(body.new_owner_id)
    if target is None or not target.is_active:
        raise HTTPException(status_code=404, detail="目标用户不存在")

    project = (
        (
            await db.execute(
                select(Project)
                .where(Project.id == project_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .first()
    )
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    if target.role != UserRole.PROJECT_ADMIN:
        raise HTTPException(status_code=400, detail="仅可转移给 project_admin")

    project.owner_id = target.id
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


class PipelineStage(BaseModel):
    """v0.18.1 · 多阶段预标注的单个阶段声明 (路径 B).

    stage 0 (parent_stage=None) 是源检测阶段, 吃整图产框; 下游阶段 (parent_stage 指向源阶段)
    平台按父框 bbox 裁 ROI crop 喂入, 把返回的 attributes 合并进父框 (write.target=attributes)。
    v0.18.2 (M2): 支持单层并行扇出 (多个下游共享同一 parent_stage)、按类别路由
    (parent_class_filter)、阶段级失败策略 (on_failure)。深度≥3 / new_shape 留 M3。
    """

    # 脏键在校验期即 422, 不再原样落进 preannotate_pipeline JSONB (见 issue 0008.1);
    # 前端 PipelineStagePayload 键集与此严格一致, 不会误伤。
    model_config = ConfigDict(extra="forbid")

    stage: int
    ml_backend_id: uuid.UUID
    # v0.21.5 · 初始输入节点 (stage 0) 的数据源描述: {"kind":"dataset","data_type":...,"execution_unit":...}。
    # 声明「源类型 + 执行单位」维度 (ROADMAP 方向 B/C)。本版仅接受并透传/持久化, 不改派发语义
    # (video tracker 逐帧/整段编排由 v0.21.6 接线)。下游 stage 无此字段。
    source: dict | None = None
    model_id: str | None = None
    task_type: str | None = None
    model_variants: dict[str, str] | None = None
    params: dict | None = None
    class_filter: list[int] | None = None
    # 依赖的父阶段 index; None=源阶段 (吃整图)。
    parent_stage: int | None = None
    # v0.18.2 · 按类别路由: 本阶段只对这些 class_name 的父框启动 (空/缺=全部父框)。
    # 不相交类别集=不同类走不同下游模型; 重叠=同类喂多模型 (并行扇出)。声明式过滤, 非分支节点。
    parent_class_filter: list[str] | None = None
    # ROI 构造; 认 {"mode":"crop","pad":0.05}, pad∈[0,0.5]。
    roi: dict | None = None
    # 结果写回; 认 {"target":"attributes","keys":[...]}。keys 用于并行兄弟键冲突检测。
    write: dict | None = None
    # v0.18.2 · 阶段级失败策略: keep_parent (默认, 下游失败保留上游框、属性留空) | drop_box (丢父框)。
    on_failure: Literal["keep_parent", "drop_box"] = "keep_parent"
    # v0.18.14 · 卡片显示名 + 写回属性键前缀。设了 label, 写回键加 f"{label}_" 前缀 (子物体
    # 命名空间, 如 hat_color / shoe_color); 缺省写原始键 (双阶段零退化)。
    label: str | None = None
    # v0.18.14 · 显式输入模式覆盖 {"mode": "full_image"|"crop"|"geometry"}; 缺省由 worker 按
    # write.target 推断 (attributes→crop, geometry/intermediate→geometry-prompt)。
    input: dict | None = None


class PreannotateRequest(BaseModel):
    ml_backend_id: uuid.UUID | None = None
    task_ids: list[uuid.UUID] | None = Field(default=None, min_length=1, max_length=200)
    # v0.9.5 · 文本批量预标可选参数
    prompt: str | None = None
    output_mode: Literal["box", "mask", "both"] = "mask"
    batch_id: uuid.UUID | None = None
    # 执行场景 (issue #121): bulk=数据管理批量预标 (缺省, 保留 pending + active 批次 +
    # 无有效编辑锁的批量前置条件); workbench=工作台「当前题 AI」单题交互执行, 按当前用户
    # 对该题的编辑语义校验 (允许 in_progress 任务与 draft 批次, 保留管理员锁 / 他人锁 / 终态保护)。
    execution_scope: Literal["bulk", "workbench"] = "bulk"
    # v0.10.38 · 按后端参数面板 (epic 阶段 2): 选中 backend 的 /setup.params 值,
    # 由前端按 backend 分桶解析后显式带上, worker 合并进 /predict context (覆盖项目级阈值兜底).
    params: dict | None = None
    # v0.11.24 · 幂等模式: skip_predicted=跳过已预标 task (默认), overwrite=先清旧预测再预标,
    # append=保留旧行为 (无脑追加, 仅特殊场景). 避免重复预标叠加重复标注.
    predict_mode: Literal["skip_predicted", "overwrite", "append"] = "skip_predicted"
    # v0.14.9 · 能力声明协议 v2 多模型目录: 选中 backend 暴露的某个 model 条目 id,
    # worker 透传进 /predict context["model_id"], backend 据此路由到对应模型。
    model_id: str | None = None
    # v0.14.9 · 任务类型便捷别名 ("ocr"/"doc_layout"/"text"): worker 写 context["type"],
    # 让纯文本以外的 task (OCR / 版面分析) 也能走批量预标。缺省走老的纯 prompt / image 行为。
    task_type: str | None = None
    # v0.14.17 · 协议 v2 结构化路径 (YOLO 等多 task 几何 backend): 选中 variant 组合 (dict[axis,value])。
    # 非空时 worker 构造 v2 context (model_variants dict + nested params + type=几何 task),
    # 而非 gsam2 文本路径的扁平形态。修通 YOLO 批量预标 (此前 worker 发 type="text" 被 YOLO 422)。
    model_variants: dict[str, str] | None = None
    # v0.14.17 · 类别白名单 (模型原生类别 index 子集): 非空时 backend 只检出这些类。
    # 平台不做类→项目标签映射 (NG6), 仅透传给 yolo /predict context.classes 做推理层过滤。
    class_filter: list[int] | None = None
    # v0.18.1 · 多阶段预标注 (路径 B): 有序阶段列表。非空时走阶段化编排 (detect→ROI→classify),
    # 缺省时由上面的平铺字段合成单阶段, 与现状逐字等价 (向后兼容)。
    # v0.18.2 · 支持单层并行扇出 (源 + N 个共享 parent_stage 的下游)。
    pipeline_stages: list[PipelineStage] | None = None
    # v0.18.2 · 并行兄弟写同一属性键时的策略: reject (默认, 校验期 422) | last_wins (末位覆盖)。
    on_key_conflict: Literal["reject", "last_wins"] = "reject"

    @model_validator(mode="after")
    def _validate_pipeline_stages(self) -> "PreannotateRequest":
        stages = self.pipeline_stages
        if not stages:
            if self.ml_backend_id is None:
                raise ValueError("ml_backend_id 必填")
            return self
        indices = {s.stage for s in stages}
        if len(indices) != len(stages):
            raise ValueError("pipeline_stages 的 stage 序号不可重复")
        # 源阶段: 恰一个 parent_stage=None。顶层 ml_backend_id 是兼容字段, 多阶段时
        # 自动从源阶段派生, 避免项目主 backend / payload 顶层成为第二真值。
        roots = [s for s in stages if s.parent_stage is None]
        if len(roots) != 1:
            raise ValueError("pipeline_stages 须恰有一个源阶段 (parent_stage=None)")
        root = roots[0]
        self.ml_backend_id = root.ml_backend_id
        self.model_id = root.model_id
        self.task_type = root.task_type
        self.model_variants = root.model_variants
        self.params = root.params
        self.class_filter = root.class_filter
        # v0.18.14 · 受限树形校验 (max depth 3): 替换原单层扇出约束。
        # parent_stage 须指向已定义且更早的阶段 (序号严格小于, 自然无环); 父须产可消费几何
        # (write.target ∈ {geometry, intermediate}); 任一链路深度 ≤ 3。
        # parent_stage=0 的旧双阶段 payload 在此等价通过 (root depth=1, 子 depth=2)。
        known_depth: dict[int, int] = {root.stage: 1}
        known_target: dict[int, str] = {
            root.stage: (root.write or {}).get("target", "geometry")
        }
        for s in sorted(stages, key=lambda x: x.stage):
            if s.parent_stage is None:
                continue
            if s.parent_stage >= s.stage:
                raise ValueError(
                    f"stage {s.stage} 的 parent_stage={s.parent_stage} 必须小于子阶段序号"
                )
            if s.parent_stage not in known_depth:
                raise ValueError(
                    f"stage {s.stage} 的 parent_stage={s.parent_stage} 未在前面定义; "
                    "受限树形要求父阶段序号严格小于子阶段"
                )
            parent_depth = known_depth[s.parent_stage]
            if parent_depth >= 3:
                raise ValueError(
                    f"stage {s.stage} 超过最大深度 3 (父深度={parent_depth})"
                )
            parent_target = known_target[s.parent_stage]
            if parent_target not in {"geometry", "intermediate"}:
                raise ValueError(
                    f"stage {s.stage} 的父阶段 {s.parent_stage} write.target={parent_target!r}, "
                    "不产几何, 无法作为父阶段"
                )
            # drop_box 的「丢父框」语义仅在父阶段为源阶段 (root) 时下标才与 root_boxes 对齐;
            # 深层 (非-root-父) 阶段的父几何是中间产物, worker 误用其下标会删掉无关的 root 框
            # (见 issue 0001)。深层 drop_box 语义未定义, 此处直接拒绝, 防止静默数据丢失。
            if s.on_failure == "drop_box" and s.parent_stage != root.stage:
                raise ValueError(
                    f"stage {s.stage} 的 on_failure='drop_box' 仅支持父阶段为源阶段 "
                    f"(stage {root.stage}); 深层阶段请用 on_failure='keep_parent'"
                )
            target = (s.write or {}).get("target", "attributes")
            # geometry: 下游产独立 polygon 追加为 new shape。intermediate: 只产几何给下游消费,
            # 不落库为候选。attributes: 纯分类下游写回父框 attributes。
            if target not in {"attributes", "geometry", "intermediate"}:
                raise ValueError(
                    "write.target 须为 'attributes' / 'geometry' / 'intermediate', "
                    f"收到 {target!r}"
                )
            # 未来祖先选择扩展点; 本版仅接受 'root' (缺省也按 root 处理)。
            ts = (s.write or {}).get("target_stage")
            if ts not in (None, "root"):
                raise ValueError(
                    f"stage {s.stage} 的 write.target_stage={ts!r} 暂不支持, 本版仅接受 'root'"
                )
            if s.roi is not None:
                mode = s.roi.get("mode", "crop")
                # crop: 平台裁父框 ROI 喂下游分类。geometry: 全图 + 父框列表喂 box-seg。
                if mode not in {"crop", "geometry"}:
                    raise ValueError(
                        f"roi.mode 须为 'crop' 或 'geometry', 收到 {mode!r}"
                    )
                pad = s.roi.get("pad")
                if pad is not None and not (0.0 <= float(pad) <= 0.5):
                    raise ValueError("roi.pad 须在 [0, 0.5] 区间")
            if s.input is not None:
                imode = s.input.get("mode")
                # 下游阶段投递只有 crop (裁父框 ROI) / geometry (整图+父框列表) 两态; worker
                # _resolve_input_mode 也只认这两个。full_image 不是真实投递模式 (会被 worker
                # 静默忽略并回落 write.target 启发式), 校验期直接拒绝以保契约一致 (见 issue 0006)。
                if imode is not None and imode not in {"crop", "geometry"}:
                    raise ValueError(
                        f"input.mode 须为 'crop' / 'geometry', 收到 {imode!r}"
                    )
            known_depth[s.stage] = parent_depth + 1
            known_target[s.stage] = target
        # 属性键冲突: 按"加完 label 前缀的最终键"维度 (写回 root)。无 label 时用原始键 (= 旧双阶段
        # 行为, 零退化); 设了 label 才加 f"{label}_" 前缀 (子物体命名空间, 与 worker 写回一致)。
        # geometry/intermediate 不写 attributes, 不参与冲突检测。
        if self.on_key_conflict == "reject":
            final_keys: dict[str, int] = {}
            for s in stages:
                if s.parent_stage is None or not s.write:
                    continue
                if s.write.get("target", "attributes") != "attributes":
                    continue
                prefix = f"{s.label}_" if s.label else ""
                for k in s.write.get("keys") or []:
                    final = f"{prefix}{k}"
                    if final in final_keys:
                        raise ValueError(
                            f"attribute key 冲突: stage {s.stage} 与 stage {final_keys[final]} "
                            f"都写 {final!r} 到 root; 设 on_key_conflict=last_wins 以允许末位覆盖"
                        )
                    final_keys[final] = s.stage
        return self


def _validate_saved_pipeline(stages) -> None:
    """v0.18.27 · 校验「保存到项目」的编排结构, 复用预标注端点同款 PipelineStage + 树形校验。

    存储态 ml_backend_id 是 str (PipelineStage 自动 coerce 成 UUID)。顶层 ml_backend_id
    取源阶段 (parent_stage=None) 的 backend, 以满足 PreannotateRequest「源阶段 backend 须
    等于顶层」约束。结构非法 → 422 (避免脏编排存进库, 到 v0.18.28 执行时才炸)。

    注意: 仅做结构 / 树形校验, 不校验 backend 存在性与归属 (那是执行期 trigger_preannotation
    的职责); 本版只存不跑。
    """
    if not stages:
        return
    if not isinstance(stages, list) or not all(isinstance(s, dict) for s in stages):
        raise HTTPException(
            status_code=422, detail="preannotate_pipeline 须为阶段对象数组"
        )
    roots = [s for s in stages if s.get("parent_stage") is None]
    if len(roots) != 1:
        raise HTTPException(
            status_code=422,
            detail="preannotate_pipeline 须恰有一个源阶段 (parent_stage=None)",
        )
    try:
        PreannotateRequest(
            ml_backend_id=roots[0].get("ml_backend_id"), pipeline_stages=stages
        )
    except ValidationError as e:
        raise HTTPException(
            status_code=422, detail=f"preannotate_pipeline 校验失败: {e}"
        ) from e


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
            return m
    return {}


def _stage_device(backend, model_id: str | None) -> str | None:
    """v0.19.5 · 取某 model 的 resource_profile.device (gpu/cpu); 无快照/未自报 → None。"""
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
    body: PreannotateRequest,
    request: Request,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        description="Optional durable identity for retrying a scoped preannotation request.",
    ),
):
    from app.services.ml_backend import MLBackendService
    from app.services.audit import AuditService
    from app.workers.tasks import _build_predict_context, _retryable_request_context

    retry_context = _build_predict_context(
        prompt=body.prompt,
        output_mode=body.output_mode,
        params=body.params,
        model_id=body.model_id,
        task_type=body.task_type,
        model_variants=body.model_variants,
        class_filter=body.class_filter,
        box_threshold=float(project.box_threshold),
        text_threshold=float(project.text_threshold),
    )
    if retry_context is not None and _retryable_request_context(retry_context) is None:
        raise HTTPException(status_code=422, detail="预标注模型上下文不能超过 8 KiB")

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
        # v0.20.21 · stage号→(caps, model_id) map, 源阶段固定 stage=0 (worker tasks.py:753)。
        # 循环内填各下游 caps, 循环后校验「上游产出几何可否作下游 ROI」。
        stage_caps: dict[int, tuple[dict, object]] = {
            0: (_stage_model(backend, body.model_id), body.model_id)
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
                    "task_type": st.task_type,
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
        from sqlalchemy import select, func
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

    from app.workers.tasks import batch_predict

    # v0.19.5 · 设备感知路由: 全 CPU pipeline → cpu 队列; 任一 GPU/未自报阶段 → gpu(ml) 队列 (零退化)。
    queue = resolve_preannotate_queue(
        stage_devices,
        gpu_queue=settings.preannotate_gpu_queue,
        cpu_queue=settings.preannotate_cpu_queue,
    )
    # v0.21.6 · detect-then-track: 含 tracker 阶段的 job 施加 soft 超时 (帧上限之外的双保险)。
    #   单阶段源 tracker 走 body.task_type; 编排里的 tracker 阶段走 pipeline_stages_payload。
    has_tracker_stage = body.task_type == "tracker" or any(
        (s or {}).get("task_type") == "tracker" for s in (pipeline_stages_payload or [])
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
        else:
            scoped_ids = (
                [str(task_id) for task_id in explicit_task_ids]
                if explicit_task_ids is not None
                else None
            )
            precreated_payload: dict = {
                "batch_id": str(body.batch_id) if body.batch_id else None,
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
        precreated_job.celery_task_id = str(precreated_job.id)
        await db.commit()
    elif body.execution_scope == "workbench" and explicit_task_ids is not None:
        # issue #121 review · 工作台单题请求 (无幂等键) 同样预建持久作业: worker 在派发
        # 前的范围复校验失败时, on_failure 能按 celery_task_id 落一条 failed 作业,
        # 工作台面板不会因找不到终态记录而无限轮询。
        scoped_ids = [str(task_id) for task_id in explicit_task_ids]
        precreated_job = await async_job_svc.create_job(
            db,
            kind="batch_predict",
            user_id=current_user.id,
            project_id=project.id,
            payload={
                "batch_id": None,
                "ml_backend_id": str(source_backend_id),
                "total_tasks": len(scoped_ids),
                "project_display_id": project.display_id,
                "project_name": project.name,
                "scope": {"task_ids": scoped_ids},
                "task_ids": scoped_ids,
            },
        )
        precreated_job.celery_task_id = str(precreated_job.id)
        await db.commit()

    apply_opts: dict = {"queue": queue}
    if has_tracker_stage:
        apply_opts["soft_time_limit"] = settings.tracker_soft_time_limit_seconds
    if precreated_job is not None:
        apply_opts["task_id"] = str(precreated_job.id)
    try:
        job = batch_predict.apply_async(
            args=[
                str(project.id),
                str(source_backend_id),
                [str(tid) for tid in explicit_task_ids] if explicit_task_ids else None,
            ],
            kwargs={
                "prompt": body.prompt,
                "output_mode": body.output_mode,
                "batch_id": str(body.batch_id) if body.batch_id else None,
                "user_id": str(current_user.id),
                "params": body.params or None,
                "predict_mode": body.predict_mode,
                # v0.14.9 · 协议 v2: 多模型路由 + task 别名透传到 /predict context
                "model_id": body.model_id,
                "task_type": body.task_type,
                # v0.14.17 · 协议 v2 结构化 variant 路径 (YOLO) + 类别白名单
                "model_variants": body.model_variants,
                "class_filter": body.class_filter,
                # v0.18.1 · 多阶段预标注: 非空时 worker 走阶段化编排 (detect→ROI→classify)
                "pipeline_stages": pipeline_stages_payload,
                # v0.21.7 · 执行单位 (video/frame/scene): frame → 逐帧 fan-out。缺省=整段/逐题。
                "execution_unit": execution_unit,
                # issue #121 · 执行场景透传 worker: workbench 单题允许 in_progress / draft,
                # bulk 保持 pending + active 批次前置条件。
                "execution_scope": body.execution_scope,
                **(
                    {"async_job_id": str(precreated_job.id)}
                    if precreated_job is not None
                    else {}
                ),
            },
            **apply_opts,
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
        precreated_job.celery_task_id = str(precreated_job.id)
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
            "job_id": str(precreated_job.id) if precreated_job else job.id,
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
            "task_type": body.task_type,
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
        "job_id": str(precreated_job.id) if precreated_job else job.id,
        "status": "queued",
        "total_tasks": total_tasks_hint,
        "channel": f"project:{project.id}:preannotate",
        **({"celery_task_id": job.id} if precreated_job else {}),
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
    pid = str(project.id)
    # 用项目 delete 同款 raw SQL 但只针对孤儿 task ids
    orphan_q = """
        SELECT id FROM tasks WHERE project_id = :pid AND id NOT IN (
            SELECT t.id FROM tasks t
            JOIN dataset_items di ON di.id = t.dataset_item_id
            JOIN project_datasets pd ON pd.dataset_id = di.dataset_id AND pd.project_id = t.project_id
            WHERE t.project_id = :pid
        )
    """
    rows = (await db.execute(text(orphan_q), {"pid": pid})).all()
    if not rows:
        return {"deleted_tasks": 0, "deleted_annotations": 0}
    orphan_count = len(rows)

    # v0.7.0：把 ANY(:ids) 序列化数组改为子查询联查，避免 10 万级孤儿场景下的 array overflow。
    # 所有 DELETE / UPDATE 共用同一 orphan-id 子查询。
    orphan_subq = (
        "SELECT id FROM tasks WHERE project_id = :pid AND id NOT IN ("
        "  SELECT t.id FROM tasks t"
        "  JOIN dataset_items di ON di.id = t.dataset_item_id"
        "  JOIN project_datasets pd ON pd.dataset_id = di.dataset_id AND pd.project_id = t.project_id"
        "  WHERE t.project_id = :pid"
        ")"
    )
    ann_count = (
        await db.execute(
            text(f"SELECT COUNT(*) FROM annotations WHERE task_id IN ({orphan_subq})"),
            {"pid": pid},
        )
    ).scalar() or 0

    await db.execute(
        text(
            f"DELETE FROM annotation_comments WHERE annotation_id IN ("
            f"  SELECT id FROM annotations WHERE task_id IN ({orphan_subq}))"
        ),
        {"pid": pid},
    )
    await db.execute(
        text(f"DELETE FROM annotation_drafts WHERE task_id IN ({orphan_subq})"),
        {"pid": pid},
    )
    await db.execute(
        text(
            f"UPDATE annotations SET parent_prediction_id = NULL, parent_annotation_id = NULL "
            f"WHERE task_id IN ({orphan_subq})"
        ),
        {"pid": pid},
    )
    await db.execute(
        text(f"DELETE FROM annotations WHERE task_id IN ({orphan_subq})"), {"pid": pid}
    )
    await db.execute(
        text(f"DELETE FROM task_locks WHERE task_id IN ({orphan_subq})"), {"pid": pid}
    )
    await db.execute(
        text(f"UPDATE bug_reports SET task_id = NULL WHERE task_id IN ({orphan_subq})"),
        {"pid": pid},
    )
    await db.execute(
        text(f"DELETE FROM tasks WHERE id IN ({orphan_subq})"), {"pid": pid}
    )

    # 重算 project + batch counters（v0.7.0：含 in_progress_tasks）
    from sqlalchemy import select, func
    from app.db.models.task import Task
    from app.db.models.task_batch import TaskBatch

    row = (
        await db.execute(
            select(
                func.count().label("total"),
                func.count().filter(Task.status == "completed").label("completed"),
                func.count().filter(Task.status == "review").label("review"),
                func.count().filter(Task.status == "in_progress").label("in_progress"),
            ).where(Task.project_id == project.id)
        )
    ).one()
    project.total_tasks = row.total
    project.completed_tasks = row.completed
    project.review_tasks = row.review
    project.in_progress_tasks = row.in_progress

    batches = (
        (await db.execute(select(TaskBatch).where(TaskBatch.project_id == project.id)))
        .scalars()
        .all()
    )
    for b in batches:
        r = (
            await db.execute(
                select(
                    func.count().label("total"),
                    func.count().filter(Task.status == "completed").label("completed"),
                    func.count().filter(Task.status == "review").label("review"),
                ).where(Task.batch_id == b.id)
            )
        ).one()
        b.total_tasks = r.total
        b.completed_tasks = r.completed
        b.review_tasks = r.review

    from app.services.audit import AuditService

    await AuditService.log(
        db,
        actor=current_user,
        action="project.cleanup_orphans",
        target_type="project",
        target_id=str(project.id),
        request=request,
        status_code=200,
        detail={"deleted_tasks": orphan_count, "deleted_annotations": int(ann_count)},
    )
    await db.commit()
    return {"deleted_tasks": orphan_count, "deleted_annotations": int(ann_count)}


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
