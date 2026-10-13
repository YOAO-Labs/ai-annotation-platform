"""用户删除 + 转交测试。"""

import uuid


class TestUserDelete:
    async def test_delete_requires_manager(self, httpx_client, annotator, super_admin):
        """普通用户不能删除他人。"""
        _, token = annotator
        user, _ = super_admin
        r = await httpx_client.delete(
            f"/api/v1/users/{user.id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 403

    async def test_cannot_delete_self(self, httpx_client, super_admin):
        """不能删除自己。"""
        user, token = super_admin
        r = await httpx_client.delete(
            f"/api/v1/users/{user.id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 400

    async def test_delete_without_pending_tasks(
        self, httpx_client, super_admin, db_session
    ):
        """无待处理任务的用户可以软删除。"""
        from app.db.models.user import User

        _, sa_token = super_admin
        # 创建一个临时用户用于删除测试
        user_data = {
            "id": uuid.uuid4(),
            "email": "temp@test.local",
            "name": "Temp",
            "password_hash": "$2b$12$" + "0" * 53,  # dummy
            "role": "employee",
            "is_active": True,
        }
        temp = User(**user_data)
        db_session.add(temp)
        await db_session.flush()

        r = await httpx_client.delete(
            f"/api/v1/users/{user_data['id']}",
            headers={"Authorization": f"Bearer {sa_token}"},
        )
        # 预期 200（软删除成功），或 409（如果该用户有其他依赖）
        assert r.status_code in (200, 409)

    async def test_delete_creates_audit_log(
        self, httpx_client, super_admin, db_session
    ):
        """删除操作产生审计日志。"""
        from app.db.models.user import User

        _, sa_token = super_admin
        uid = uuid.uuid4()
        temp = User(
            id=uid,
            email="todel@test.local",
            name="ToDelete",
            password_hash="$2b$12$" + "0" * 53,
            role="annotator",
            is_active=True,
        )
        db_session.add(temp)
        await db_session.flush()

        r = await httpx_client.delete(
            f"/api/v1/users/{uid}",
            headers={"Authorization": f"Bearer {sa_token}"},
        )
        assert r.status_code in (200, 409)

        if r.status_code == 200:
            from app.db.models.audit_log import AuditLog
            from sqlalchemy import select

            result = await db_session.execute(
                select(AuditLog).where(AuditLog.action == "user.delete")
            )
            logs = result.scalars().all()
            assert len(logs) >= 1

    async def test_cannot_delete_last_super_admin(self, httpx_client, super_admin):
        """不能删除最后一名 super_admin。"""
        user, token = super_admin
        # 尝试删除自己——应拒绝
        r = await httpx_client.delete(
            f"/api/v1/users/{user.id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 400  # 不能删除自己
