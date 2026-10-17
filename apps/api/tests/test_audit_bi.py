"""Audit BI materialization, API aggregation, and scheduling tests."""

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text

from app.db.models.audit_log import AuditLog


async def test_materialized_view_only_contains_completed_utc_days(db_session):
    now = datetime.now(timezone.utc)
    completed_action = "test.audit_bi.completed_day"
    current_action = "test.audit_bi.current_day"
    db_session.add_all(
        [
            AuditLog(action=completed_action, created_at=now - timedelta(days=1)),
            AuditLog(action=current_action, created_at=now),
        ]
    )
    await db_session.flush()
    await db_session.execute(text("REFRESH MATERIALIZED VIEW mv_audit_bi_daily"))

    rows = (
        await db_session.execute(
            text(
                "SELECT action, event_count FROM mv_audit_bi_daily "
                "WHERE action IN (:completed_action, :current_action)"
            ),
            {
                "completed_action": completed_action,
                "current_action": current_action,
            },
        )
    ).all()
    assert [(row.action, row.event_count) for row in rows] == [(completed_action, 1)]


async def test_monthly_summary_merges_dimensions_and_business_scope(
    httpx_client, db_session, auth_headers
):
    from app.services.audit_partition_service import AuditPartitionService

    await AuditPartitionService.ensure_future_partitions(db_session, months_ahead=2)
    today = datetime.now(timezone.utc).date()
    month_index = today.year * 12 + today.month - 1 + 2
    year, zero_month = divmod(month_index, 12)
    month_start = date(year, zero_month + 1, 1)
    month_end = (
        date(year + 1, 1, 1)
        if month_start.month == 12
        else date(year, month_start.month + 1, 1)
    )
    month = month_start.strftime("%Y-%m")
    db_session.add_all(
        [
            AuditLog(
                action="project.create",
                target_type="project",
                actor_role="super_admin",
                status_code=201,
                created_at=datetime.combine(
                    month_start + timedelta(days=1),
                    datetime.min.time(),
                    timezone.utc,
                )
                + timedelta(hours=10),
            ),
            AuditLog(
                action="task.reject",
                target_type="task",
                actor_role="reviewer",
                status_code=422,
                created_at=datetime.combine(
                    month_start + timedelta(days=1),
                    datetime.min.time(),
                    timezone.utc,
                )
                + timedelta(hours=11),
            ),
            AuditLog(
                action="system.event",
                status_code=None,
                created_at=datetime.combine(
                    month_start + timedelta(days=2),
                    datetime.min.time(),
                    timezone.utc,
                )
                + timedelta(hours=8),
            ),
            AuditLog(
                action="http.post",
                status_code=500,
                created_at=datetime.combine(
                    month_start + timedelta(days=2),
                    datetime.min.time(),
                    timezone.utc,
                )
                + timedelta(hours=9),
            ),
        ]
    )
    await db_session.commit()

    response = await httpx_client.get(
        f"/api/v1/audit-logs/monthly-summary?month={month}", headers=auth_headers
    )
    assert response.status_code == 200
    data = response.json()
    assert data["timezone"] == "UTC"
    assert data["totals"] == {
        "event_count": 3,
        "error_count": 1,
        "action_kind_count": 3,
    }
    assert len(data["daily"]) == (month_end - month_start).days
    assert data["daily"][1]["event_count"] == 2
    assert data["daily"][1]["error_count"] == 1
    assert data["daily"][2]["event_count"] == 1
    assert [item["key"] for item in data["top_actions"]] == [
        "project.create",
        "system.event",
        "task.reject",
    ]
    assert data["target_types"] == [
        {"key": "", "event_count": 1},
        {"key": "project", "event_count": 1},
        {"key": "task", "event_count": 1},
    ]

    all_response = await httpx_client.get(
        f"/api/v1/audit-logs/monthly-summary?month={month}&business_only=false",
        headers=auth_headers,
    )
    assert all_response.status_code == 200
    assert all_response.json()["totals"] == {
        "event_count": 4,
        "error_count": 2,
        "action_kind_count": 4,
    }


async def test_monthly_summary_rejects_invalid_or_archived_month(
    httpx_client, auth_headers
):
    invalid = await httpx_client.get(
        "/api/v1/audit-logs/monthly-summary?month=2026-13", headers=auth_headers
    )
    assert invalid.status_code == 422

    archived = await httpx_client.get(
        "/api/v1/audit-logs/monthly-summary?month=2000-01", headers=auth_headers
    )
    assert archived.status_code == 422
    assert "归档" in archived.json()["detail"]


async def test_monthly_summary_is_super_admin_only(httpx_client, project_admin):
    _, token = project_admin
    response = await httpx_client.get(
        "/api/v1/audit-logs/monthly-summary?month=2099-01",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


def test_audit_bi_refresh_is_scheduled_on_maintenance_queue():
    from app.workers.celery_app import celery_app

    task = "app.workers.cleanup.refresh_audit_bi_mv"
    assert celery_app.conf.task_routes[task] == {"queue": "maintenance"}
    assert celery_app.conf.beat_schedule["refresh-audit-bi-mv"]["task"] == task


async def test_monthly_summary_reports_peer_outage_without_empty_data(
    httpx_client, auth_headers, monkeypatch
):
    from app.config import settings

    monkeypatch.setattr(settings, "audit_summary_url", "http://127.0.0.1:1")
    response = await httpx_client.get(
        "/api/v1/audit-logs/monthly-summary?month=2099-01", headers=auth_headers
    )
    assert response.status_code == 503
    assert response.json()["detail"] == {"reason": "audit_summary_unavailable"}
