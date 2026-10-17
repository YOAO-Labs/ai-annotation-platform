from datetime import date
from types import SimpleNamespace

import pytest

from app.services.audit_summary import build_monthly_summary


@pytest.mark.parametrize(
    "year,month,days", [(2024, 2, 29), (2026, 2, 28), (2026, 12, 31)]
)
def test_local_summary_fills_utc_calendar_without_remote_service(year, month, days):
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    summary = build_monthly_summary(
        month=start.strftime("%Y-%m"),
        month_start=start,
        month_end=end,
        materialized_through=None,
        rows=[],
    )
    assert len(summary.daily) == days
    assert summary.daily[0].day == start
    assert summary.daily[-1].day == date(year, month, days)
    assert summary.totals.event_count == summary.totals.error_count == 0
    assert summary.top_actions == summary.target_types == summary.actor_roles == []


def test_local_summary_keeps_exact_counts_empty_dimensions_and_tie_order():
    large = 9007199254740993
    rows = [
        SimpleNamespace(
            day=date(2026, 10, 1),
            action=action,
            target_type="",
            actor_role="",
            status_family=status,
            event_count=large,
        )
        for action, status in (("z.action", 2), ("a.action", 5))
    ]
    summary = build_monthly_summary(
        month="2026-10",
        month_start=date(2026, 10, 1),
        month_end=date(2026, 11, 1),
        materialized_through=date(2026, 9, 30),
        rows=rows,
    )
    assert summary.totals.event_count == 2 * large
    assert summary.totals.error_count == large
    assert summary.totals.action_kind_count == 2
    assert [bucket.key for bucket in summary.top_actions] == ["a.action", "z.action"]
    assert summary.target_types[0].key == summary.actor_roles[0].key == ""
    assert summary.target_types[0].event_count == 2 * large
