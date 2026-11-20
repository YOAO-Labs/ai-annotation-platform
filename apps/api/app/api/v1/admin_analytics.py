"""DuckDB 离线分析面板（ROADMAP §1.6）。

仅 super_admin 可见。固定 panel enum，不接受任意 SQL。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from app.deps import require_roles
from app.db.enums import UserRole

router = APIRouter()

PanelName = Literal[
    "throughput_daily",
    "reject_rate_by_type",
    "duration_dist",
    "activity_heatmap",
]


@router.get("/{panel_name}")
async def get_analytics_panel(
    panel_name: PanelName,
    days: int = Query(30, ge=1, le=365),
    _: Any = Depends(require_roles(UserRole.SUPER_ADMIN.value)),
) -> dict:
    """取一个面板的数据。

    DuckDB 文件由 Celery beat (sync_to_duckdb) 每日更新；如果首次同步尚未跑过
    返回 503，让前端展示「数据初始化中」。
    """
    from app.services import analytics_queries

    try:
        if panel_name == "throughput_daily":
            return {
                "panel": panel_name,
                "data": analytics_queries.user_throughput_daily(days),
            }
        if panel_name == "reject_rate_by_type":
            return {
                "panel": panel_name,
                "data": analytics_queries.reject_rate_by_type(days),
            }
        if panel_name == "duration_dist":
            return {
                "panel": panel_name,
                "data": analytics_queries.annotation_duration_distribution(days),
            }
        if panel_name == "activity_heatmap":
            return {
                "panel": panel_name,
                "data": analytics_queries.activity_heatmap(days),
            }
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=503, detail=f"analytics not ready: {exc}"
        ) from exc
    raise HTTPException(status_code=400, detail="unknown panel")
