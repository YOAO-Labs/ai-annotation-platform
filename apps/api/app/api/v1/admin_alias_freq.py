"""v0.9.7 · 项目 alias 频率聚合.

统计本项目 predictions.result JSONB 中各 detected label 出现次数, 让
AIPreAnnotate chips 按真实预标频率排序, 高频常用类别浮上来.

实现:
- 直接在 PG 用 ``jsonb_path_query`` 展开 result 数组, 取 ``$.value.labels[0]``
  fallback ``$.value.class``, GROUP BY count.
- ``Prediction.project_id`` FK 已索引, 无需 JOIN tasks.
- 当前预标量级 ~10k/项目, 直查 < 100ms; 暂不加缓存, 监控 P95 触发再加.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import UserRole
from app.db.models.project import Project
from app.deps import get_db, require_project_owner, require_roles
from app.services.identity_contracts import IdentityActorFacts

router = APIRouter()

# label 维度截断, 防止异常 prompt (DINO 偶发空字符串等) 把响应撑爆
_LIMIT = 200


class AliasFrequencyResponse(BaseModel):
    project_id: uuid.UUID
    total_predictions: int
    frequency: dict[str, int]
    last_computed_at: datetime


@router.get(
    "/admin/projects/{project_id}/alias-frequency",
    response_model=AliasFrequencyResponse,
)
async def get_alias_frequency(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _admin: IdentityActorFacts = Depends(
        require_roles(UserRole.PROJECT_ADMIN, UserRole.SUPER_ADMIN)
    ),
    _project: Project = Depends(require_project_owner),
) -> AliasFrequencyResponse:
    """统计本项目 predictions 中各 detected label 出现次数, 按 count desc 排."""
    # total predictions
    total_q = await db.execute(
        text(
            """
            SELECT COUNT(*) FROM aap_labeling_v1.prediction_rows WHERE project_id = :pid
            """
        ),
        {"pid": str(project_id)},
    )
    total: int = int(total_q.scalar_one() or 0)

    if total == 0:
        return AliasFrequencyResponse(
            project_id=project_id,
            total_predictions=0,
            frequency={},
            last_computed_at=datetime.now(timezone.utc),
        )

    # 展开 JSONB 数组, 按 LabelStudio 标准 value.{type}[0] 取 label
    # (rectanglelabels → value.rectanglelabels[0]; polygonlabels → value.polygonlabels[0]),
    # 兼容老格式 value.labels[0] 或 value.class.
    # NOTE: result 通常是 list[AnnotationResult]; 偶发不是数组 (mock data) 时
    # jsonb_array_elements 会报错, 用 jsonb_typeof 守卫.
    rows = await db.execute(
        text(
            """
            WITH expanded AS (
              SELECT
                COALESCE(
                  (elem->'value' -> (elem->>'type') ->> 0),
                  (elem->'value'->'labels'->>0),
                  (elem->'value'->>'class')
                ) AS label
              FROM aap_labeling_v1.prediction_rows p
              CROSS JOIN LATERAL jsonb_array_elements(
                CASE WHEN jsonb_typeof(p.result) = 'array'
                     THEN p.result ELSE '[]'::jsonb END
              ) AS elem
              WHERE p.project_id = :pid
            )
            SELECT label, COUNT(*) AS cnt
            FROM expanded
            WHERE label IS NOT NULL AND label <> ''
            GROUP BY label
            ORDER BY cnt DESC
            LIMIT :lim
            """
        ),
        {"pid": str(project_id), "lim": _LIMIT},
    )

    frequency: dict[str, int] = {label: int(cnt) for label, cnt in rows.all()}

    return AliasFrequencyResponse(
        project_id=project_id,
        total_predictions=total,
        frequency=frequency,
        last_computed_at=datetime.now(timezone.utc),
    )
