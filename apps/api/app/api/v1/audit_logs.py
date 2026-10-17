import base64
import csv
import io
import json
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import String, and_, cast, func, or_, outerjoin, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_db, require_roles
from app.db.enums import UserRole
from app.db.models.audit_log import AuditLog
from app.db.models.user import User
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.annotation import Annotation
from app.db.models.annotation_feedback import AnnotationFeedback
from app.db.models.user_invitation import UserInvitation
from app.config import settings
from app.schemas.audit import (
    AuditArchiveOut,
    AuditArchiveRowsOut,
    AuditLogList,
    AuditLogOut,
    AuditMonthlySummary,
    AuditSummaryBucket,
    AuditSummaryDailyPoint,
    AuditSummaryTotals,
)
from app.services.audit_partition_service import AuditPartitionService

router = APIRouter()

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _month_bounds(month: str) -> tuple[date, date]:
    if not _MONTH_RE.fullmatch(month):
        raise HTTPException(status_code=422, detail="month 必须使用 YYYY-MM 格式")
    start = date(int(month[:4]), int(month[5:]), 1)
    end = (
        date(start.year + 1, 1, 1)
        if start.month == 12
        else date(start.year, start.month + 1, 1)
    )
    return start, end


def _shift_month(start: date, delta: int) -> date:
    month_index = start.year * 12 + start.month - 1 + delta
    year, zero_month = divmod(month_index, 12)
    return date(year, zero_month + 1, 1)


def _rank_buckets(values: dict[str, int], *, limit: int | None = None):
    ranked = [
        AuditSummaryBucket(key=key, event_count=count)
        for key, count in sorted(values.items(), key=lambda item: (-item[1], item[0]))
    ]
    return ranked[:limit] if limit is not None else ranked


async def _load_monthly_summary_rows(
    db: AsyncSession, *, month_start: date, month_end: date, business_only: bool
) -> tuple[date | None, list[tuple]]:
    materialized_through = (
        await db.execute(text("SELECT MAX(day) FROM mv_audit_bi_daily"))
    ).scalar_one_or_none()
    mv_end = month_start
    if materialized_through is not None:
        mv_end = min(
            month_end, max(month_start, materialized_through + timedelta(days=1))
        )

    rows: list[tuple] = []
    if mv_end > month_start:
        rows.extend(
            (
                await db.execute(
                    text(
                        """
                        SELECT day, action, target_type, actor_role, status_family, event_count
                        FROM mv_audit_bi_daily
                        WHERE day >= :month_start AND day < :mv_end
                          AND (:business_only = false OR action NOT LIKE 'http.%')
                        """
                    ),
                    {
                        "month_start": month_start,
                        "mv_end": mv_end,
                        "business_only": business_only,
                    },
                )
            ).all()
        )

    raw_start = max(month_start, mv_end)
    if raw_start < month_end:
        rows.extend(
            (
                await db.execute(
                    text(
                        """
                        SELECT
                            (created_at AT TIME ZONE 'UTC')::date AS day,
                            action,
                            COALESCE(target_type, '') AS target_type,
                            COALESCE(actor_role, '') AS actor_role,
                            CASE
                                WHEN status_code BETWEEN 200 AND 599
                                    THEN (status_code / 100)::smallint
                                ELSE 0::smallint
                            END AS status_family,
                            COUNT(*)::bigint AS event_count
                        FROM audit_logs
                        WHERE created_at >= :raw_start AND created_at < :month_end
                          AND (:business_only = false OR action NOT LIKE 'http.%')
                        GROUP BY day, action, target_type, actor_role, status_family
                        """
                    ),
                    {
                        "raw_start": datetime.combine(
                            raw_start, time.min, timezone.utc
                        ),
                        "month_end": datetime.combine(
                            month_end, time.min, timezone.utc
                        ),
                        "business_only": business_only,
                    },
                )
            ).all()
        )
    return materialized_through, rows


def _build_monthly_summary(
    *,
    month: str,
    month_start: date,
    month_end: date,
    materialized_through: date | None,
    rows: list[tuple],
) -> AuditMonthlySummary:
    daily = {
        day: {"event_count": 0, "error_count": 0}
        for day in (
            month_start + timedelta(days=offset)
            for offset in range((month_end - month_start).days)
        )
    }
    actions: dict[str, int] = {}
    target_types: dict[str, int] = {}
    actor_roles: dict[str, int] = {}
    event_count = 0
    error_count = 0

    for row in rows:
        row_count = int(row.event_count)
        is_error = int(row.status_family) in (4, 5)
        event_count += row_count
        error_count += row_count if is_error else 0
        daily[row.day]["event_count"] += row_count
        daily[row.day]["error_count"] += row_count if is_error else 0
        actions[row.action] = actions.get(row.action, 0) + row_count
        target_types[row.target_type] = target_types.get(row.target_type, 0) + row_count
        actor_roles[row.actor_role] = actor_roles.get(row.actor_role, 0) + row_count

    return AuditMonthlySummary(
        month=month,
        materialized_through=materialized_through,
        totals=AuditSummaryTotals(
            event_count=event_count,
            error_count=error_count,
            action_kind_count=len(actions),
        ),
        daily=[
            AuditSummaryDailyPoint(day=day, **counts) for day, counts in daily.items()
        ],
        top_actions=_rank_buckets(actions, limit=10),
        target_types=_rank_buckets(target_types),
        actor_roles=_rank_buckets(actor_roles),
    )


def _build_base_query(
    action: str | None,
    target_type: str | None,
    target_id: str | None,
    actor_id: str | None,
    from_: datetime | None,
    to: datetime | None,
    business_only: bool = False,
    detail_key: str | None = None,
    detail_value: str | None = None,
):
    """返回带 actor_email JOIN 的基础 select，避免 N+1。

    detail_key + detail_value（A.3）：走 PG GIN 索引 + JSONB `@>` 子集匹配，
    例如 `?detail_key=role&detail_value=super_admin` 等价 `WHERE detail_json @> '{"role":"super_admin"}'`。
    """
    j = outerjoin(AuditLog, User, AuditLog.actor_id == User.id)
    base = select(AuditLog, User.email.label("_u_email")).select_from(j)
    count_base = select(func.count()).select_from(AuditLog)

    if action:
        base = base.where(AuditLog.action == action)
        count_base = count_base.where(AuditLog.action == action)
    if target_type:
        base = base.where(AuditLog.target_type == target_type)
        count_base = count_base.where(AuditLog.target_type == target_type)
    if target_id:
        base = base.where(AuditLog.target_id == target_id)
        count_base = count_base.where(AuditLog.target_id == target_id)
    if actor_id:
        base = base.where(AuditLog.actor_id == actor_id)
        count_base = count_base.where(AuditLog.actor_id == actor_id)
    if from_:
        base = base.where(AuditLog.created_at >= from_)
        count_base = count_base.where(AuditLog.created_at >= from_)
    if to:
        base = base.where(AuditLog.created_at <= to)
        count_base = count_base.where(AuditLog.created_at <= to)
    if business_only:
        base = base.where(~AuditLog.action.like("http.%"))
        count_base = count_base.where(~AuditLog.action.like("http.%"))
    if detail_key and detail_value is not None:
        # JSONB 子集匹配 —— 走 GIN 索引（仅 PG）；测试态 SQLite 走通用 contains
        match = {detail_key: detail_value}
        base = base.where(AuditLog.detail_json.contains(match))
        count_base = count_base.where(AuditLog.detail_json.contains(match))

    return base, count_base


def _row_to_out(row) -> AuditLogOut:
    log, u_email = row
    result = AuditLogOut.model_validate(log)
    if result.actor_email is None and u_email is not None:
        result.actor_email = u_email
    return result


def _managed_audit_scope(actor: User, *, project_id: UUID | None = None):
    conditions = (
        [] if actor.role == UserRole.SUPER_ADMIN else [Project.owner_id == actor.id]
    )
    if project_id is not None:
        conditions.append(Project.id == project_id)
    project_ids = select(Project.id).where(*conditions)
    projects = select(cast(Project.id, String)).where(*conditions)
    tasks = select(cast(Task.id, String)).where(Task.project_id.in_(project_ids))
    batches = select(cast(TaskBatch.id, String)).where(
        TaskBatch.project_id.in_(project_ids)
    )
    annotations = (
        select(cast(Annotation.id, String))
        .join(Task, Task.id == Annotation.task_id)
        .where(Task.project_id.in_(project_ids))
    )
    feedbacks = select(cast(AnnotationFeedback.id, String)).where(
        AnnotationFeedback.project_id.in_(project_ids)
    )
    invitations = select(cast(UserInvitation.id, String)).where(
        UserInvitation.project_id.in_(project_ids)
    )
    video_items = select(cast(Task.dataset_item_id, String)).where(
        Task.project_id.in_(project_ids), Task.dataset_item_id.is_not(None)
    )
    primary = ("project", "task", "batch", "annotation", "feedback", "invitation")
    contexts = (
        "project_member",
        "project_task_view",
        "dataset",
        "annotation_comment",
        "annotation_operation",
        "camera_annotation_member",
        "async_job",
        "job",
        "export",
        "failed_prediction",
        "mask_format_import",
        "mask_repair_batch",
        "point_cloud_quality_evaluation",
        "point_cloud_quality_issue",
        "scene_track_operation",
        "video_bbox",
        "video_chapter",
        "video_segment",
        "video_track_mask",
        "video_tracker_job",
    )
    path_scope = or_(
        and_(
            AuditLog.path.like("/api/v1/projects/%"),
            func.split_part(AuditLog.path, "/", 5).in_(projects),
        ),
        and_(
            AuditLog.path.like("/api/v1/tasks/%"),
            func.split_part(AuditLog.path, "/", 5).in_(tasks),
        ),
        and_(
            AuditLog.path.like("/api/v1/annotations/%"),
            func.split_part(AuditLog.path, "/", 5).in_(annotations),
        ),
        and_(
            AuditLog.path.like("/api/v1/admin/projects/%"),
            func.split_part(AuditLog.path, "/", 6).in_(projects),
        ),
    )
    return or_(
        and_(AuditLog.target_type == "project", AuditLog.target_id.in_(projects)),
        and_(AuditLog.target_type == "task", AuditLog.target_id.in_(tasks)),
        and_(AuditLog.target_type == "batch", AuditLog.target_id.in_(batches)),
        and_(AuditLog.target_type == "annotation", AuditLog.target_id.in_(annotations)),
        and_(AuditLog.target_type == "feedback", AuditLog.target_id.in_(feedbacks)),
        and_(AuditLog.target_type == "invitation", AuditLog.target_id.in_(invitations)),
        and_(
            AuditLog.target_type == "invitation",
            AuditLog.target_id.is_(None),
            AuditLog.action == "invitation.export",
            AuditLog.detail_json["project_id"].astext.in_(projects),
        ),
        and_(
            AuditLog.target_type == "video_chapter",
            AuditLog.detail_json["dataset_item_id"].astext.in_(video_items),
        ),
        and_(
            AuditLog.target_type == "batch",
            AuditLog.action == "batch.deleted",
            ~AuditLog.target_id.in_(select(cast(TaskBatch.id, String))),
            or_(AuditLog.detail_json["project_id"].astext.in_(projects), path_scope),
        ),
        and_(
            AuditLog.target_type.in_(contexts),
            or_(
                AuditLog.detail_json["project_id"].astext.in_(projects),
                AuditLog.detail_json["task_id"].astext.in_(tasks),
            ),
        ),
        and_(
            or_(AuditLog.target_type.is_(None), ~AuditLog.target_type.in_(primary)),
            path_scope,
        ),
    )


def _encode_cursor(created_at: datetime, id_: int) -> str:
    payload = f"{created_at.isoformat()}|{id_}"
    return base64.urlsafe_b64encode(payload.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, int]:
    payload = base64.urlsafe_b64decode(cursor.encode()).decode()
    ts_str, id_str = payload.rsplit("|", 1)
    return datetime.fromisoformat(ts_str), int(id_str)


@router.get("", response_model=AuditLogList)
async def list_audit_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    cursor: str | None = None,
    action: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    actor_id: str | None = None,
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = None,
    business_only: bool = Query(
        False, description="仅返回业务事件，排除 http.* 中间件元数据行"
    ),
    detail_key: str | None = Query(
        None, description="A.3：detail_json 字段级过滤——键名"
    ),
    detail_value: str | None = Query(
        None, description="A.3：detail_json 字段级过滤——键值"
    ),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.PROJECT_ADMIN)),
):
    base, count_q = _build_base_query(
        action,
        target_type,
        target_id,
        actor_id,
        from_,
        to,
        business_only,
        detail_key,
        detail_value,
    )

    if actor.role != UserRole.SUPER_ADMIN:
        scope = _managed_audit_scope(actor)
        base = base.where(scope)
        count_q = count_q.where(scope)

    total = (await db.execute(count_q)).scalar_one()

    if cursor:
        cur_ts, cur_id = _decode_cursor(cursor)
        paged = base.where(
            (AuditLog.created_at < cur_ts)
            | ((AuditLog.created_at == cur_ts) & (AuditLog.id < cur_id))
        )
    else:
        paged = base.offset((page - 1) * page_size)

    rows = (
        await db.execute(
            paged.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(
                page_size
            )
        )
    ).all()

    items = [_row_to_out(r) for r in rows]

    next_cursor = None
    if len(items) == page_size and items:
        last = items[-1]
        next_cursor = _encode_cursor(last.created_at, last.id)

    return AuditLogList(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        next_cursor=next_cursor,
    )


@router.get("/monthly-summary", response_model=AuditMonthlySummary)
async def get_monthly_audit_summary(
    month: str = Query(..., description="UTC 自然月，格式 YYYY-MM"),
    business_only: bool = Query(True, description="排除 http.* 中间件元数据行"),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    month_start, month_end = _month_bounds(month)
    current_month = datetime.now(timezone.utc).date().replace(day=1)
    retention_start = _shift_month(current_month, -settings.audit_retention_months)
    if month_start < retention_start:
        raise HTTPException(
            status_code=422,
            detail="该月份已超出在线审计保留期，请使用归档查询",
        )

    materialized_through, rows = await _load_monthly_summary_rows(
        db,
        month_start=month_start,
        month_end=month_end,
        business_only=business_only,
    )
    return _build_monthly_summary(
        month=month,
        month_start=month_start,
        month_end=month_end,
        materialized_through=materialized_through,
        rows=rows,
    )


@router.get("/archives", response_model=list[AuditArchiveOut])
async def list_audit_archives(
    _: User = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    """列出已归档到 MinIO 冷存储的审计月份（合规回源前置查询）。

    无归档对象时返回空列表。
    """
    return AuditPartitionService.list_archives()


@router.get("/archives/{year}/{month}", response_model=AuditArchiveRowsOut)
async def read_audit_archive(
    year: int = Path(..., ge=2000, le=9999),
    month: int = Path(..., ge=1, le=12),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    _: User = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    """回源读取指定月份的归档 jsonl.gz，解压解析后分页返回归档行。

    对象不存在 → 404。
    """
    rows = AuditPartitionService.read_archive_rows(year, month)
    if rows is None:
        raise HTTPException(
            status_code=404, detail=f"未找到归档对象: {year}/{month:02d}.jsonl.gz"
        )
    return AuditArchiveRowsOut(
        items=rows[offset : offset + limit],
        total=len(rows),
        limit=limit,
        offset=offset,
        year=year,
        month=month,
    )


@router.get("/export")
async def export_audit_logs(
    request: Request,
    format: Literal["csv", "json"] = "csv",
    action: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    actor_id: str | None = None,
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = None,
    business_only: bool = Query(
        False, description="仅导出业务事件，排除 http.* 中间件元数据行"
    ),
    detail_key: str | None = Query(None),
    detail_value: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    _MAX_ROWS = 50_000
    base, count_q = _build_base_query(
        action,
        target_type,
        target_id,
        actor_id,
        from_,
        to,
        business_only,
        detail_key,
        detail_value,
    )

    total = (await db.execute(count_q)).scalar_one()
    if total > _MAX_ROWS:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=413, detail=f"导出条数超过 {_MAX_ROWS}，请缩小筛选范围"
        )

    rows = (
        await db.execute(
            base.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(
                _MAX_ROWS
            )
        )
    ).all()
    items = [_row_to_out(r) for r in rows]

    # 记录自身的导出操作
    from app.services.audit import AuditService, export_detail, export_metadata_header
    from app.middleware.request_id import request_id_var

    filter_criteria = {
        k: v
        for k, v in {
            "action": action,
            "target_type": target_type,
            "target_id": target_id,
            "actor_id": actor_id,
            "from": from_.isoformat() if from_ else None,
            "to": to.isoformat() if to else None,
            "business_only": business_only if business_only else None,
            "detail_key": detail_key,
            "detail_value": detail_value,
        }.items()
        if v is not None
    }
    await AuditService.log(
        db,
        actor=current_user,
        action="audit.export",
        target_type="audit_logs",
        status_code=200,
        request=request,
        detail=export_detail(
            actor=current_user,
            request=request,
            base={"format": format, "rows": len(items)},
            filter_criteria=filter_criteria,
        ),
    )
    await db.commit()

    if format == "json":

        def _gen_json():
            wrapped = {
                "_export_meta": {
                    "exported_by": current_user.email,
                    "exported_at": datetime.now().isoformat(),
                    "request_id": request_id_var.get() or None,
                    "rows": len(items),
                    "filter_criteria": filter_criteria,
                },
                "items": [i.model_dump(mode="json") for i in items],
            }
            yield json.dumps(wrapped, ensure_ascii=False, indent=2)

        return StreamingResponse(
            _gen_json(),
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=audit_logs.json"},
        )

    # CSV
    _COLS = [
        "id",
        "created_at",
        "actor_email",
        "actor_role",
        "action",
        "target_type",
        "target_id",
        "method",
        "path",
        "status_code",
        "ip",
        "detail_json",
    ]

    def _gen_csv():
        buf = io.StringIO()
        # v0.8.1 · 首部审计 metadata 注释行
        buf.write(
            export_metadata_header(actor=current_user, fmt="csv", request=request)
        )
        writer = csv.DictWriter(buf, fieldnames=_COLS, extrasaction="ignore")
        writer.writeheader()
        for item in items:
            row = item.model_dump(mode="json")
            if row.get("detail_json") is not None:
                row["detail_json"] = json.dumps(row["detail_json"], ensure_ascii=False)
            writer.writerow(row)
        yield buf.getvalue()

    return StreamingResponse(
        _gen_csv(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=audit_logs.csv"},
    )
