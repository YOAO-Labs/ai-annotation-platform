from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user, get_db
from app.db.models.user import User
from app.schemas.project_performance import (
    PerformanceEventsResponse,
    PerformanceMemberDetailResponse,
    PerformanceMembersResponse,
)
from app.services.project_performance import (
    export_members_csv,
    list_members_performance,
    member_performance_detail,
    member_performance_events,
)


router = APIRouter()


def _common_query(
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    timezone_name: str | None = Query(None, alias="timezone"),
    work_type: Literal["annotation", "review"] = Query("annotation"),
    account_status: Literal["all", "active", "inactive"] = Query("all"),
    include_historical: bool = Query(False),
    q: str | None = Query(None, max_length=200),
    sort: str | None = Query("+name"),
    cursor: str | None = Query(None),
    limit: int = Query(50, ge=1, le=100),
) -> dict:
    return {
        "from_": from_,
        "to": to,
        "timezone_name": timezone_name,
        "work_type": work_type,
        "account_status": account_status,
        "include_historical": include_historical,
        "query": q,
        "sort": sort,
        "cursor": cursor,
        "limit": limit,
    }


@router.get(
    "/projects/{project_id}/performance/members",
    response_model=PerformanceMembersResponse,
)
async def get_members_performance(
    project_id: UUID,
    params: dict = Depends(_common_query),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PerformanceMembersResponse:
    return await list_members_performance(db, project_id, current_user, **params)


@router.get(
    "/projects/{project_id}/performance/members/{member_id}/events",
    response_model=PerformanceEventsResponse,
)
async def get_member_performance_events(
    project_id: UUID,
    member_id: UUID,
    params: dict = Depends(_common_query),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PerformanceEventsResponse:
    return await member_performance_events(
        db,
        project_id,
        member_id,
        current_user,
        **{key: value for key, value in params.items() if key != "sort"},
    )


@router.get(
    "/projects/{project_id}/performance/members/{member_id}",
    response_model=PerformanceMemberDetailResponse,
)
async def get_member_performance_detail(
    project_id: UUID,
    member_id: UUID,
    params: dict = Depends(_common_query),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PerformanceMemberDetailResponse:
    return await member_performance_detail(
        db,
        project_id,
        member_id,
        current_user,
        evidence_cursor=params["cursor"],
        **{
            key: value for key, value in params.items() if key not in {"cursor", "sort"}
        },
    )


@router.get("/projects/{project_id}/performance/export")
async def export_project_members_performance(
    project_id: UUID,
    params: dict = Depends(_common_query),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Response:
    payload = await export_members_csv(db, project_id, current_user, **params)
    return Response(
        content=payload,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="project_members_performance.csv"'
        },
    )
