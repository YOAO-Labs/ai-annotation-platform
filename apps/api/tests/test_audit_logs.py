"""审计日志过滤 / 分页 / 自动记录测试。"""

from datetime import datetime, timezone
from uuid import uuid4

from app.db.models.audit_log import AuditLog


class TestAuditLogFilters:
    async def test_list_requires_super_admin(self, httpx_client, db_session, annotator):
        """非管理员无法访问审计日志。"""
        _, token = annotator
        r = await httpx_client.get(
            "/api/v1/audit-logs", headers={"Authorization": f"Bearer {token}"}
        )
        assert r.status_code == 403

    async def test_list_as_super_admin(self, httpx_client, auth_headers):
        """super_admin 可获取审计日志列表。"""
        r = await httpx_client.get("/api/v1/audit-logs", headers=auth_headers)
        assert r.status_code == 200
        data = r.json()
        assert "items" in data
        assert "total" in data
        assert isinstance(data["items"], list)

    async def test_filter_by_target_type(self, httpx_client, auth_headers):
        """按 target_type 过滤。"""
        r = await httpx_client.get(
            "/api/v1/audit-logs?target_type=user", headers=auth_headers
        )
        assert r.status_code == 200
        for item in r.json()["items"]:
            assert item.get("target_type") == "user" or item.get("target_type") is None

    async def test_business_only_filters_before_pagination(
        self, httpx_client, db_session, auth_headers
    ):
        """仅业务事件过滤在分页和计数前生效。"""
        target_type = "audit_business_scope_test"
        target_id = "business-only-scope"
        now = datetime.now(timezone.utc)
        db_session.add_all(
            [
                AuditLog(
                    action="http.post",
                    target_type=target_type,
                    target_id=target_id,
                    request_id="audit-business-scope-test",
                    created_at=now,
                ),
                AuditLog(
                    action="project.create",
                    target_type=target_type,
                    target_id=target_id,
                    request_id="audit-business-scope-test",
                    created_at=now,
                ),
            ]
        )
        await db_session.commit()

        r = await httpx_client.get(
            (
                "/api/v1/audit-logs"
                f"?target_type={target_type}&business_only=true&page_size=50"
            ),
            headers=auth_headers,
        )
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 1
        assert [item["action"] for item in data["items"]] == ["project.create"]

    async def test_pagination(self, httpx_client, auth_headers):
        """分页参数生效。

        列表端点的分页契约为 `page` / `page_size`（`limit` / `offset` 只属于导出端点），
        因此这里必须传 `page_size`；传 `limit` 会被忽略并回落到默认 20 条。
        """
        r = await httpx_client.get(
            "/api/v1/audit-logs?page=1&page_size=5", headers=auth_headers
        )
        assert r.status_code == 200
        data = r.json()
        assert len(data["items"]) <= 5


class TestAuditAutoRecord:
    async def test_login_creates_audit_log(self, httpx_client, db_session, super_admin):
        """登录操作自动产生审计日志。"""
        user, _ = super_admin
        r = await httpx_client.post(
            "/api/v1/auth/login", json={"email": user.email, "password": "Test1234"}
        )
        assert r.status_code == 200

        from sqlalchemy import select

        result = await db_session.execute(
            select(AuditLog)
            .where(
                AuditLog.action == "auth.login",
                AuditLog.target_id == str(user.id),
            )
            .order_by(AuditLog.created_at.desc())
            .limit(1)
        )
        log = result.scalar_one_or_none()
        assert log is not None
        assert log.status_code == 200


class TestAuditLogProjection:
    async def test_export_projects_current_email_without_mutating_history(
        self, httpx_client, db_session, super_admin
    ):
        """Exports project a current email fallback without rewriting audit history."""
        from sqlalchemy import select

        user, token = super_admin
        historical_email = user.email
        current_email = f"renamed-{uuid4()}@test.local"
        target_id = str(uuid4())
        target_type = "audit_projection"
        history_action = "audit.projection.historical"
        legacy_action = "audit.projection.legacy"
        now = datetime.now(timezone.utc)
        db_session.add_all(
            [
                AuditLog(
                    actor_id=user.id,
                    actor_email=historical_email,
                    actor_role="super_admin",
                    action=history_action,
                    target_type=target_type,
                    target_id=target_id,
                    created_at=now,
                ),
                AuditLog(
                    actor_id=user.id,
                    actor_email=None,
                    actor_role="super_admin",
                    action=legacy_action,
                    target_type=target_type,
                    target_id=target_id,
                    created_at=now,
                ),
            ]
        )
        user.email = current_email
        await db_session.flush()
        headers = {"Authorization": f"Bearer {token}"}
        filters = {"target_type": target_type, "target_id": target_id}

        exported = await httpx_client.get(
            "/api/v1/audit-logs/export",
            params={**filters, "format": "json"},
            headers=headers,
        )
        assert exported.status_code == 200, exported.text
        export_body = exported.json()
        export_items = {item["action"]: item for item in export_body["items"]}
        assert export_body["_export_meta"]["rows"] == 2
        assert export_body["_export_meta"]["filter_criteria"] == filters
        assert export_items[history_action]["actor_email"] == historical_email
        assert export_items[legacy_action]["actor_email"] == current_email

        listed = await httpx_client.get(
            "/api/v1/audit-logs", params=filters, headers=headers
        )
        assert listed.status_code == 200, listed.text
        list_body = listed.json()
        assert list_body["total"] == 2
        list_items = {item["action"]: item for item in list_body["items"]}
        assert list_items[history_action]["actor_email"] == historical_email
        assert list_items[legacy_action]["actor_email"] == current_email

        stored_emails = (
            await db_session.execute(
                select(AuditLog.action, AuditLog.actor_email)
                .where(AuditLog.target_id == target_id)
                .order_by(AuditLog.action)
            )
        ).all()
        assert dict(stored_emails) == {
            history_action: historical_email,
            legacy_action: None,
        }

        export_detail = (
            await db_session.execute(
                select(AuditLog.detail_json).where(
                    AuditLog.action == "audit.export",
                    AuditLog.actor_id == user.id,
                    AuditLog.target_type == "audit_logs",
                )
            )
        ).scalar_one()
        assert export_detail["format"] == "json"
        assert export_detail["rows"] == 2
        assert export_detail["actor_email"] == current_email
        assert export_detail["filter_criteria"] == filters
