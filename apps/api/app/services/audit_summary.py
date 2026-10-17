"""Local audit summary calculation owned by the Python application."""

from datetime import date, timedelta

from app.schemas.audit import (
    AuditMonthlySummary,
    AuditSummaryBucket,
    AuditSummaryDailyPoint,
    AuditSummaryTotals,
)


def _rank_buckets(values: dict[str, int], *, limit: int | None = None):
    ranked = [
        AuditSummaryBucket(key=key, event_count=count)
        for key, count in sorted(values.items(), key=lambda item: (-item[1], item[0]))
    ]
    return ranked[:limit] if limit is not None else ranked


def build_monthly_summary(
    *,
    month: str,
    month_start: date,
    month_end: date,
    materialized_through: date | None,
    rows: list,
) -> AuditMonthlySummary:
    daily = {
        month_start + timedelta(days=offset): {"event_count": 0, "error_count": 0}
        for offset in range((month_end - month_start).days)
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
