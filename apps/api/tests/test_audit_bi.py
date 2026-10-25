"""Audit BI materialization, API aggregation, and scheduling tests."""

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text

from app.db.models.audit_log import AuditLog


async def test_materialized_view_only_contains_completed_utc_days(
    db_session, monkeypatch
):
    from contextlib import asynccontextmanager

    from app.workers import cleanup

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

    class RecordingSession:
        def __init__(self):
            self.statements = []
            self.commits = 0
            self.rollbacks = 0

        async def execute(self, statement, parameters=None):
            self.statements.append(str(statement))
            return await db_session.execute(statement, parameters)

        async def commit(self):
            self.commits += 1
            await db_session.commit()

        async def rollback(self):
            self.rollbacks += 1
            await db_session.rollback()

    recorded = RecordingSession()

    @asynccontextmanager
    async def task_session():
        yield recorded

    monkeypatch.setattr(cleanup, "task_session", task_session)
    assert await cleanup._refresh_audit_bi_mv_async() == {"refreshed": True}
    assert recorded.statements == ["SELECT aap_audit_v1.refresh_audit_bi_mv()"]
    assert recorded.commits == 1
    assert recorded.rollbacks == 0

    rows = (
        await db_session.execute(
            text(
                "SELECT action, event_count FROM aap_audit_v1.audit_daily_facts "
                "WHERE action IN (:completed_action, :current_action)"
            ),
            {
                "completed_action": completed_action,
                "current_action": current_action,
            },
        )
    ).all()
    assert [(row.action, row.event_count) for row in rows] == [(completed_action, 1)]


async def test_daily_projection_preserves_materialized_view_native_types(db_session):
    rows = (
        await db_session.execute(
            text(
                """
                SELECT relation.relname AS relation_name,
                       attribute.attname AS column_name,
                       pg_catalog.format_type(attribute.atttypid, attribute.atttypmod)
                           AS column_type
                FROM pg_catalog.pg_class AS relation
                JOIN pg_catalog.pg_namespace AS namespace
                  ON namespace.oid = relation.relnamespace
                JOIN pg_catalog.pg_attribute AS attribute
                  ON attribute.attrelid = relation.oid
                WHERE (
                    (namespace.nspname = 'public'
                     AND relation.relname = 'mv_audit_bi_daily')
                    OR (namespace.nspname = 'aap_audit_v1'
                        AND relation.relname = 'audit_daily_facts')
                )
                  AND attribute.attnum > 0
                  AND NOT attribute.attisdropped
                ORDER BY relation_name, attribute.attnum
                """
            )
        )
    ).all()
    by_relation = {}
    for row in rows:
        by_relation.setdefault(row.relation_name, {})[row.column_name] = row.column_type

    expected = {
        "day": "date",
        "action": "character varying(64)",
        "target_type": "character varying",
        "actor_role": "character varying",
        "status_family": "smallint",
        "event_count": "bigint",
    }
    assert by_relation == {
        "audit_daily_facts": expected,
        "mv_audit_bi_daily": expected,
    }


async def test_monthly_summary_combines_completed_day_and_raw_tail(db_session):
    from app.api.v1.audit_logs import _load_monthly_summary_rows

    now = datetime.now(timezone.utc)
    today = now.date()
    completed_day = today - timedelta(days=1)
    completed_action = "test.audit_bi.completed_day"
    tail_action = "test.audit_bi.raw_tail"
    db_session.add_all(
        [
            AuditLog(
                action=completed_action,
                target_type="project",
                actor_role="reviewer",
                status_code=201,
                created_at=datetime.combine(
                    completed_day, datetime.min.time(), timezone.utc
                )
                + timedelta(hours=12),
            ),
            AuditLog(
                action=tail_action,
                target_type="task",
                actor_role="reviewer",
                status_code=500,
                created_at=now,
            ),
        ]
    )
    await db_session.flush()
    await db_session.execute(text("SELECT aap_audit_v1.refresh_audit_bi_mv()"))

    class RecordingSession:
        def __init__(self, session):
            self.session = session
            self.statements = []

        async def execute(self, statement, params=None):
            self.statements.append(str(statement))
            return await self.session.execute(statement, params)

    recorded = RecordingSession(db_session)
    materialized_through, rows = await _load_monthly_summary_rows(
        recorded,
        month_start=completed_day,
        month_end=today + timedelta(days=1),
        business_only=False,
    )

    assert materialized_through == completed_day
    by_action = {row.action: row for row in rows}
    assert by_action[completed_action].day == completed_day
    assert by_action[completed_action].event_count == 1
    assert by_action[tail_action].day == today
    assert by_action[tail_action].event_count == 1
    assert any(
        "from aap_audit_v1.audit_events" in statement.lower()
        for statement in recorded.statements
    )
    assert any(
        "from aap_audit_v1.audit_daily_facts" in statement.lower()
        for statement in recorded.statements
    )
    assert not any(
        "from audit_logs" in statement.lower() for statement in recorded.statements
    )
    assert not any(
        "from mv_audit_bi_daily" in statement.lower()
        for statement in recorded.statements
    )


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


async def test_monthly_summary_works_without_experimental_peer(
    httpx_client, auth_headers, monkeypatch
):
    monkeypatch.setenv("AUDIT_SUMMARY_URL", "http://127.0.0.1:1")
    response = await httpx_client.get(
        "/api/v1/audit-logs/monthly-summary?month=2099-01", headers=auth_headers
    )
    assert response.status_code == 200
    assert response.json()["totals"] == {
        "event_count": 0,
        "error_count": 0,
        "action_kind_count": 0,
    }
    assert len(response.json()["daily"]) == 31
