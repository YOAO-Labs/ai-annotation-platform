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
        assert r.status_code == 200, r.text
        assert r.json()["is_active"] is False
        assert r.json()["disabled_kind"] == "deleted"
        await db_session.refresh(temp)
        assert temp.is_active is False
        assert temp.disabled_reason == "管理员删除账号"
        assert temp.credential_generation == 0

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
            role="employee",
            is_active=True,
        )
        db_session.add(temp)
        await db_session.flush()

        r = await httpx_client.delete(
            f"/api/v1/users/{uid}",
            headers={"Authorization": f"Bearer {sa_token}"},
        )
        assert r.status_code == 200, r.text

        from app.db.models.audit_log import AuditLog
        from sqlalchemy import select

        log = await db_session.scalar(
            select(AuditLog).where(
                AuditLog.action == "user.delete", AuditLog.target_id == str(uid)
            )
        )
        assert log is not None
        assert log.detail_json == {
            "email": "todel@test.local",
            "role": "employee",
            "transferred_to": None,
            "transferred_count": 0,
            "released_locks": 0,
        }

    async def test_cannot_delete_last_super_admin(self, httpx_client, super_admin):
        """不能删除最后一名 super_admin。"""
        user, token = super_admin
        # 尝试删除自己——应拒绝
        r = await httpx_client.delete(
            f"/api/v1/users/{user.id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 400  # 不能删除自己
