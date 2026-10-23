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
from sqlalchemy.orm import aliased

from aap_audit import (
    EXPORT_MAX_ROWS,
    AuditQueryAfter,
    AuditQueryFilters,
    AuditQueryRequest,
)
from aap_audit.query_postgres import AuditQueryPostgresData

from app.deps import get_db, require_roles
from app.db.enums import UserRole
from app.services.audit_read_relations import audit_event
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.annotation import Annotation
from app.db.models.annotation_feedback import AnnotationFeedback
from app.config import settings
from app.schemas.audit import (
    AuditArchiveOut,
    AuditArchiveRowsOut,
    AuditLogList,
    AuditLogOut,
    AuditMonthlySummary,
)
from app.services.audit_partition_service import AuditPartitionService
from app.services.audit_summary import build_monthly_summary
from app.services.identity_contracts import IdentityActorFacts
from app.services.identity_read_relations import (
    account_profiles,
    invitation_audit_targets,
)
from app.services.workspace_read_relations import project_facts

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


async def _load_monthly_summary_rows(
    db: AsyncSession, *, month_start: date, month_end: date, business_only: bool
) -> tuple[date | None, list[tuple]]:
    materialized_through = (
        await db.execute(text("SELECT MAX(day) FROM aap_audit_v1.audit_daily_facts"))
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
                        FROM aap_audit_v1.audit_daily_facts
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
                        FROM aap_audit_v1.audit_events
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
    actor_profile = account_profiles.alias("audit_actor_profile")
    j = outerjoin(
        audit_event, actor_profile, audit_event.actor_id == actor_profile.c.id
    )
    base = select(audit_event, actor_profile.c.email.label("_u_email")).select_from(j)
    count_base = select(func.count()).select_from(audit_event)

    if action:
        base = base.where(audit_event.action == action)
        count_base = count_base.where(audit_event.action == action)
    if target_type:
        base = base.where(audit_event.target_type == target_type)
        count_base = count_base.where(audit_event.target_type == target_type)
    if target_id:
        base = base.where(audit_event.target_id == target_id)
        count_base = count_base.where(audit_event.target_id == target_id)
    if actor_id:
        base = base.where(audit_event.actor_id == actor_id)
        count_base = count_base.where(audit_event.actor_id == actor_id)
    if from_:
        base = base.where(audit_event.created_at >= from_)
        count_base = count_base.where(audit_event.created_at >= from_)
    if to:
        base = base.where(audit_event.created_at <= to)
        count_base = count_base.where(audit_event.created_at <= to)
    if business_only:
        base = base.where(~audit_event.action.like("http.%"))
        count_base = count_base.where(~audit_event.action.like("http.%"))
    if detail_key and detail_value is not None:
        # JSONB 子集匹配 —— 走 GIN 索引（仅 PG）；测试态 SQLite 走通用 contains
        match = {detail_key: detail_value}
        base = base.where(audit_event.detail_json.contains(match))
        count_base = count_base.where(audit_event.detail_json.contains(match))

    return base, count_base


def _row_to_out(row) -> AuditLogOut:
    log, u_email = row
    result = AuditLogOut.model_validate(log)
    if result.actor_email is None and u_email is not None:
        result.actor_email = u_email
    return result


def _query_event_to_out(event) -> AuditLogOut:
    values = event.model_dump()
    values["id"] = int(event.id)
    return AuditLogOut.model_validate(values)


def _legacy_query_datetime(value: datetime | None) -> datetime | None:
    if value is not None and value.utcoffset() is None:
        return value.astimezone()
    return value


def _legacy_query_actor_id(value: str | None) -> str | None:
    if value is None or value == "":
        return value
    if re.fullmatch(
        r"(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{32})",
        value,
        re.IGNORECASE,
    ):
        return str(UUID(value))
    return value


def _query_filters(
    *,
    action: str | None,
    target_type: str | None,
    target_id: str | None,
    actor_id: str | None,
    from_: datetime | None,
    to: datetime | None,
    business_only: bool,
    detail_key: str | None,
    detail_value: str | None,
) -> AuditQueryFilters:
    return AuditQueryFilters(
        action=action,
        target_type=target_type,
        target_id=target_id,
        actor_id=_legacy_query_actor_id(actor_id),
        from_=_legacy_query_datetime(from_),
        to=_legacy_query_datetime(to),
        business_only=business_only,
        detail_key=detail_key,
        detail_value=detail_value,
    )


def _managed_audit_scope(actor: IdentityActorFacts, *, project_id: UUID | None = None):
    project = aliased(Project, project_facts(), adapt_on_names=True)
    conditions = (
        [] if actor.role == UserRole.SUPER_ADMIN else [project.owner_id == actor.id]
    )
    if project_id is not None:
        conditions.append(project.id == project_id)
    project_ids = select(project.id).where(*conditions)
    projects = select(cast(project.id, String)).where(*conditions)
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
    invitations = select(cast(invitation_audit_targets.c.id, String)).where(
        invitation_audit_targets.c.project_id.in_(project_ids)
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
            audit_event.path.like("/api/v1/projects/%"),
            func.split_part(audit_event.path, "/", 5).in_(projects),
        ),
        and_(
            audit_event.path.like("/api/v1/tasks/%"),
            func.split_part(audit_event.path, "/", 5).in_(tasks),
        ),
        and_(
            audit_event.path.like("/api/v1/annotations/%"),
            func.split_part(audit_event.path, "/", 5).in_(annotations),
        ),
        and_(
            audit_event.path.like("/api/v1/admin/projects/%"),
            func.split_part(audit_event.path, "/", 6).in_(projects),
        ),
    )
    return or_(
        and_(audit_event.target_type == "project", audit_event.target_id.in_(projects)),
        and_(audit_event.target_type == "task", audit_event.target_id.in_(tasks)),
        and_(audit_event.target_type == "batch", audit_event.target_id.in_(batches)),
        and_(
            audit_event.target_type == "annotation",
            audit_event.target_id.in_(annotations),
        ),
        and_(
            audit_event.target_type == "feedback", audit_event.target_id.in_(feedbacks)
        ),
        and_(
            audit_event.target_type == "invitation",
            audit_event.target_id.in_(invitations),
        ),
        and_(
            audit_event.target_type == "invitation",
            audit_event.target_id.is_(None),
            audit_event.action == "invitation.export",
            audit_event.detail_json["project_id"].astext.in_(projects),
        ),
        and_(
            audit_event.target_type == "video_chapter",
            audit_event.detail_json["dataset_item_id"].astext.in_(video_items),
        ),
        and_(
            audit_event.target_type == "batch",
            audit_event.action == "batch.deleted",
            ~audit_event.target_id.in_(select(cast(TaskBatch.id, String))),
            or_(audit_event.detail_json["project_id"].astext.in_(projects), path_scope),
        ),
        and_(
            audit_event.target_type.in_(contexts),
            or_(
                audit_event.detail_json["project_id"].astext.in_(projects),
                audit_event.detail_json["task_id"].astext.in_(tasks),
            ),
        ),
        and_(
            or_(
                audit_event.target_type.is_(None), ~audit_event.target_type.in_(primary)
            ),
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
    actor: IdentityActorFacts = Depends(
        require_roles(UserRole.SUPER_ADMIN, UserRole.PROJECT_ADMIN)
    ),
):
    if actor.role == UserRole.SUPER_ADMIN:
        after = None
        if cursor:
            cur_ts, cur_id = _decode_cursor(cursor)
            cur_ts = _legacy_query_datetime(cur_ts)
            after = AuditQueryAfter(created_at=cur_ts.isoformat(), id=str(cur_id))
        reply = await AuditQueryPostgresData(db).query(
            AuditQueryRequest.list_page(
                _query_filters(
                    action=action,
                    target_type=target_type,
                    target_id=target_id,
                    actor_id=actor_id,
                    from_=from_,
                    to=to,
                    business_only=business_only,
                    detail_key=detail_key,
                    detail_value=detail_value,
                ),
                page=page,
                page_size=page_size,
                after=after,
            )
        )
        items = [_query_event_to_out(event) for event in reply.data.events]
        next_cursor = None
        if len(items) == page_size and items:
            last = items[-1]
            next_cursor = _encode_cursor(last.created_at, last.id)
        return AuditLogList(
            items=items,
            total=int(reply.data.total),
            page=page,
            page_size=page_size,
            next_cursor=next_cursor,
        )

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
            (audit_event.created_at < cur_ts)
            | ((audit_event.created_at == cur_ts) & (audit_event.id < cur_id))
        )
    else:
        paged = base.offset((page - 1) * page_size)

    rows = (
        await db.execute(
            paged.order_by(audit_event.created_at.desc(), audit_event.id.desc()).limit(
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
    _: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    """Return authorized monthly audit aggregates from the local calculator."""
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
    return build_monthly_summary(
        month=month,
        month_start=month_start,
        month_end=month_end,
        materialized_through=materialized_through,
        rows=rows,
    )


@router.get("/archives", response_model=list[AuditArchiveOut])
async def list_audit_archives(
    _: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
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
    _: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
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
    current_user: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    _MAX_ROWS = EXPORT_MAX_ROWS
    reply = await AuditQueryPostgresData(db).query(
        AuditQueryRequest.export(
            _query_filters(
                action=action,
                target_type=target_type,
                target_id=target_id,
                actor_id=actor_id,
                from_=from_,
                to=to,
                business_only=business_only,
                detail_key=detail_key,
                detail_value=detail_value,
            )
        )
    )
    total = int(reply.data.total)
    if total > _MAX_ROWS:
        raise HTTPException(
            status_code=413, detail=f"导出条数超过 {_MAX_ROWS}，请缩小筛选范围"
        )
    items = [_query_event_to_out(event) for event in reply.data.events]

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
