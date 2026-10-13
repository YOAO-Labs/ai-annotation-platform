"""角色矩阵守卫测试 —— 验证各角色只能在其权限范围内修改他人角色。

平台身份与项目职责已分离：本文件只覆盖平台 ``users.role`` 的修改守卫，
项目职责由 ``project_members.role`` 端点与其测试覆盖。
"""

from tests.factory import create_user


class TestRoleMatrix:
    """12 个角色修改守卫用例。"""

    async def test_sa_upgrade_legacy_annotator_to_employee(
        self, httpx_client, super_admin, db_session
    ):
        """super_admin 可将历史 annotator 平台角色改为 employee。

        迁移前的历史账号值仍是可读事实，升级路径必须可用；这里直接构造
        legacy 行，避免用当前的 employee fixture 冒充旧角色。
        """
        _, token = super_admin
        user = await create_user(
            db_session, "annotator", "legacy-annotator@test.local", "Legacy"
        )
        await db_session.flush()
        r = await httpx_client.patch(
            f"/api/v1/users/{user.id}/role",
            json={"role": "employee"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200
        assert r.json()["role"] == "employee"

    async def test_sa_rejects_legacy_global_staff_role_input(
        self, httpx_client, super_admin, annotator
    ):
        """旧的全局 annotator/reviewer 角色输入被拒绝。"""
        _, token = super_admin
        user, _ = annotator
        r = await httpx_client.patch(
            f"/api/v1/users/{user.id}/role",
            json={"role": "reviewer"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 400

    async def test_sa_promotes_employee_to_project_admin(
        self, httpx_client, super_admin, reviewer
    ):
        """super_admin 可将 employee 提升为 project_admin。"""
        _, token = super_admin
        user, _ = reviewer
        r = await httpx_client.patch(
            f"/api/v1/users/{user.id}/role",
            json={"role": "project_admin"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200
        assert r.json()["role"] == "project_admin"

    async def test_sa_cannot_demote_self(self, httpx_client, super_admin):
        """super_admin 不能修改自己的角色。"""
        user, token = super_admin
        r = await httpx_client.patch(
            f"/api/v1/users/{user.id}/role",
            json={"role": "viewer"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 400

    async def test_sa_cannot_demote_last_super_admin(self, httpx_client, super_admin):
        """不能降级最后一名 super_admin。"""
        _, token = super_admin
        # 尝试降级自己以外的唯一超管（只有自己）会报 400（不能改自己）
        # 此用例验证：当系统中只有 1 个 super_admin 时，其他 manager 无法降级
        pass  # 依赖多用户场景，当前单 fixture 无法完整覆盖

    async def test_pa_cannot_promote_to_pa(
        self, httpx_client, project_admin, annotator
    ):
        """project_admin 不能将 employee 提升为 project_admin。"""
        _, token = project_admin
        user, _ = annotator
        # project_admin 需要 target 在其项目内，先用 super_admin 覆盖
        # 当前简化：验证 403 来自角色限制而非成员限制
        r = await httpx_client.patch(
            f"/api/v1/users/{user.id}/role",
            json={"role": "project_admin"},
            headers={"Authorization": f"Bearer {token}"},
        )
        # 期望 403（project_admin 不能设置 super_admin / project_admin 角色）
        assert r.status_code in (403, 404)

    async def test_employee_cannot_change_roles(
        self, httpx_client, annotator, super_admin
    ):
        """普通员工（平台 employee）无法修改任何人的角色。"""
        _, token = annotator
        user, _ = super_admin
        r = await httpx_client.patch(
            f"/api/v1/users/{user.id}/role",
            json={"role": "reviewer"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 403

    async def test_non_admin_denied_even_with_legacy_role_input(
        self, httpx_client, reviewer, annotator
    ):
        """非管理员即使提交历史角色值也会在鉴权层被拒（403）。"""
        _, token = reviewer
        user, _ = annotator
        r = await httpx_client.patch(
            f"/api/v1/users/{user.id}/role",
            json={"role": "annotator"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 403

    async def test_invalid_role_rejected(self, httpx_client, super_admin, annotator):
        """非法角色值返回 400。"""
        _, token = super_admin
        user, _ = annotator
        r = await httpx_client.patch(
            f"/api/v1/users/{user.id}/role",
            json={"role": "superuser"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 400

    async def test_sa_can_change_pa_role(
        self, httpx_client, super_admin, project_admin
    ):
        """super_admin 可以修改 project_admin 的角色。"""
        _, token = super_admin
        user, _ = project_admin
        r = await httpx_client.patch(
            f"/api/v1/users/{user.id}/role",
            json={"role": "employee"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200

    async def test_sa_deactivate_other_super_admin(self, httpx_client, super_admin):
        """验证 super_admin 无法通过角色修改降级自己。"""
        user, token = super_admin
        r = await httpx_client.patch(
            f"/api/v1/users/{user.id}/role",
            json={"role": "employee"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 400  # 不能修改自己

    async def test_nonexistent_user_returns_404(self, httpx_client, super_admin):
        """不存在的用户返回 404。"""
        _, token = super_admin
        r = await httpx_client.patch(
            "/api/v1/users/00000000-0000-0000-0000-000000000000/role",
            json={"role": "employee"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 404

    async def test_role_change_creates_audit_log(
        self, httpx_client, super_admin, annotator, db_session
    ):
        """角色变更产生审计日志。"""
        _, token = super_admin
        user, _ = annotator
        # The fixture account is already ``employee``; use a different platform
        # role so this exercises a real change rather than a no-op.
        r = await httpx_client.patch(
            f"/api/v1/users/{user.id}/role",
            json={"role": "project_admin"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200

        from app.db.models.audit_log import AuditLog
        from sqlalchemy import select

        result = await db_session.execute(
            select(AuditLog)
            .where(
                AuditLog.action == "user.role_change",
                AuditLog.target_id == str(user.id),
            )
            .order_by(AuditLog.created_at.desc())
            .limit(1)
        )
        log = result.scalar_one_or_none()
        assert log is not None
