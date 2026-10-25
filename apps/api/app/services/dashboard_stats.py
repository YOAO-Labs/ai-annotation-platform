"""Dashboard 统计纯函数(从 api/v1/dashboard.py 拆出,行为零变化)。

只承载"取数 + 计算"逻辑(分位、时间窗、驳回原因/类别分布、首过率),不依赖
FastAPI 请求/鉴权上下文 —— 因此可被 admin / annotator 等多个 dashboard router 共享。
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.services.labeling_core_read_relations import annotation_row, task_row


def _percentile_rank(values: list[float], target: float) -> int:
    """简易团队分位（0-100）：value 在排序后处于的百分位。"""
    if not values:
        return 50
    below = sum(1 for v in values if v < target)
    return int(round(below / len(values) * 100))


def _period_window(period: str) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "today":
        return today, now
    if period == "1m":
        return today - timedelta(days=30), now
    if period == "4w":
        return today - timedelta(days=28), now
    # default: 7d / week
    return today - timedelta(days=6), now


async def _reject_reason_breakdown(db, uid, start, project_id=None) -> list[dict]:
    """v0.12.4 · 本人被驳回任务按 reject_reason_type 分布(A1)。

    v0.12.6 (A3)：project_id 给定时按该项目切分。
    """
    proj = [task_row.project_id == project_id] if project_id else []
    rows = (
        await db.execute(
            select(task_row.reject_reason_type, func.count().label("n"))
            .where(
                task_row.assignee_id == uid,
                task_row.reject_reason_type.isnot(None),
                task_row.submitted_at >= start,
                *proj,
            )
            .group_by(task_row.reject_reason_type)
            .order_by(func.count().desc())
        )
    ).all()
    total = sum(int(r.n) for r in rows)
    return [
        {
            "reason_type": r.reject_reason_type,
            "count": int(r.n),
            "pct": round(int(r.n) / total * 100, 1) if total else 0.0,
        }
        for r in rows
    ]


async def _class_distribution(
    db, uid, start, limit: int = 10, project_id=None
) -> list[dict]:
    """v0.12.4 · 本人标注按 class_name 的 top-N 占比(A1 类别覆盖)。

    v0.12.6 (A3)：project_id 给定时按该项目切分。
    """
    proj = [annotation_row.project_id == project_id] if project_id else []
    rows = (
        await db.execute(
            select(annotation_row.class_name, func.count().label("n"))
            .where(
                annotation_row.user_id == uid,
                annotation_row.is_active.is_(True),
                annotation_row.created_at >= start,
                *proj,
            )
            .group_by(annotation_row.class_name)
            .order_by(func.count().desc())
            .limit(limit)
        )
    ).all()
    total = (
        await db.execute(
            select(func.count())
            .select_from(annotation_row)
            .where(
                annotation_row.user_id == uid,
                annotation_row.is_active.is_(True),
                annotation_row.created_at >= start,
                *proj,
            )
        )
    ).scalar() or 0
    return [
        {
            "class_name": r.class_name,
            "count": int(r.n),
            "pct": round(int(r.n) / total * 100, 1) if total else 0.0,
        }
        for r in rows
    ]


async def _first_pass_yield(db, uid, start, project_id=None) -> float | None:
    """v0.12.4 · 首过率 = 一次通过(无 reopen)/ 提交总数(A1)。无样本→None。

    v0.12.6 (A3)：project_id 给定时按该项目切分。
    """
    proj = [task_row.project_id == project_id] if project_id else []
    row = (
        await db.execute(
            select(
                func.count().label("submitted_n"),
                func.count().filter(task_row.reopened_count == 0).label("clean_n"),
            ).where(
                task_row.assignee_id == uid,
                task_row.submitted_at.isnot(None),
                task_row.submitted_at >= start,
                *proj,
            )
        )
    ).first()
    sn = int(row.submitted_n or 0) if row else 0
    cn = int(row.clean_n or 0) if row else 0
    return round(cn / sn, 3) if sn else None
