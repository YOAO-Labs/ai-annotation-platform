"""v0.7.8 · 登录注册改进 + 安全加固 + 治理合规 测试套件。"""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.factory import build_tool_bindings

pytestmark = pytest.mark.asyncio


# ── 邀请频率限流 ────────────────────────────────────────────────────────


async def test_invitation_rate_limit_exceeded(
    httpx_client: httpx.AsyncClient, super_admin, db_session: AsyncSession
):
    """超过每日邀请上限后返回 429。"""
    from app.config import settings

    user, token = super_admin
    headers = {"Authorization": f"Bearer {token}"}

    # 临时将上限设为 2 以便测试
    original = settings.max_invitations_per_day
    settings.max_invitations_per_day = 2

    try:
        for i in range(2):
            r = await httpx_client.post(
                "/api/v1/users/invite",
                json={"email": f"invite{i}@limit.test", "role": "employee"},
                headers=headers,
            )
            assert r.status_code == 201, f"Invite {i} failed: {r.text}"

        r = await httpx_client.post(
            "/api/v1/users/invite",
            json={"email": "over_limit@limit.test", "role": "employee"},
            headers=headers,
        )
        assert r.status_code == 429
    finally:
        settings.max_invitations_per_day = original


# ── 会话管理 ─────────────────────────────────────────────────────────────


async def test_logout_blacklists_token(
    httpx_client: httpx.AsyncClient, super_admin, db_session: AsyncSession
):
    """POST /auth/logout 后旧 token 不可用。"""
    _, token = super_admin
    headers = {"Authorization": f"Bearer {token}"}

    r = await httpx_client.get("/api/v1/auth/me", headers=headers)
    assert r.status_code == 200

    r = await httpx_client.post("/api/v1/auth/logout", headers=headers)
    assert r.status_code == 204

    r = await httpx_client.get("/api/v1/auth/me", headers=headers)
    assert r.status_code == 401


async def test_logout_all_invalidates_sessions(
    httpx_client: httpx.AsyncClient, db_session: AsyncSession
):
    """POST /auth/logout-all 后旧 token 失效，返回新 token 可用。"""
    from app.core.security import create_access_token
    from app.db.models.user import User

    user = User(
        id=uuid.uuid4(),
        email="session_test@local",
        name="SessionTest",
        password_hash="$2b$12$dummy",
        role="annotator",
        is_active=True,
    )
    db_session.add(user)
    await db_session.flush()

    token_a = create_access_token(subject=str(user.id), role=user.role, gen=0)
    token_b = create_access_token(subject=str(user.id), role=user.role, gen=0)

    headers_a = {"Authorization": f"Bearer {token_a}"}

    r = await httpx_client.post("/api/v1/auth/logout-all", headers=headers_a)
    assert r.status_code == 200
    new_token = r.json()["access_token"]

    # 旧 token B 应该失效
    r = await httpx_client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {token_b}"}
    )
    assert r.status_code == 401

    # 新 token 有效
    r = await httpx_client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {new_token}"}
    )
    assert r.status_code == 200


# ── 导出审计 ─────────────────────────────────────────────────────────────


async def test_project_export_creates_audit_log(
    httpx_client: httpx.AsyncClient, super_admin, db_session: AsyncSession
):
    """项目导出应在 audit_logs 中生成 project.export 记录。"""
    from app.db.models.project import Project
    from app.db.models.audit_log import AuditLog

    user, token = super_admin
    headers = {"Authorization": f"Bearer {token}"}

    project = Project(
        id=uuid.uuid4(),
        display_id="P-EXP1",
        name="Export Test",
        type_key="video-track",
        type_label="测试",
        data_type="video",
        owner_id=user.id,
        status="in_progress",
        tool_bindings=build_tool_bindings(["a"]),
    )
    db_session.add(project)
    await db_session.flush()

    # v0.10.27 导出异步化: POST 创建 async_job 并返回 202; 审计日志仍同步落库。
    # v0.10.43 · format → targets 多目标。
    r = await httpx_client.post(
        f"/api/v1/projects/{project.id}/export"
        "?targets=mots&video_overlap_policy=z_order&mots_frame_base=1",
        headers=headers,
    )
    assert r.status_code == 202

    result = await db_session.execute(
        select(AuditLog).where(
            AuditLog.action == "project.export",
            AuditLog.target_id == str(project.id),
        )
    )
    audit = result.scalar_one_or_none()
    assert audit is not None
    assert audit.detail_json["targets"] == ["mots"]
    assert audit.detail_json["filter_criteria"]["video_overlap_policy"] == "z_order"
    assert audit.detail_json["filter_criteria"]["mots_frame_base"] == 1


# ── 审计日志不可变 ───────────────────────────────────────────────────────


async def test_audit_log_delete_denied(
    httpx_client: httpx.AsyncClient, super_admin, db_session: AsyncSession
):
    """直接 DELETE audit_logs 行应被 trigger 拦截。"""
    from app.db.models.audit_log import AuditLog

    entry = AuditLog(
        actor_email="test@deny.local",
        actor_role="super_admin",
        action="test.delete_deny",
        status_code=200,
    )
    db_session.add(entry)
    await db_session.flush()

    with pytest.raises(Exception, match="immutable"):
        await db_session.execute(text(f"DELETE FROM audit_logs WHERE id = {entry.id}"))
        await db_session.flush()


async def test_audit_log_update_denied(
    httpx_client: httpx.AsyncClient, super_admin, db_session: AsyncSession
):
    """直接 UPDATE audit_logs 行应被 trigger 拦截。"""
    from app.db.models.audit_log import AuditLog

    entry = AuditLog(
        actor_email="test@update.local",
        actor_role="super_admin",
        action="test.update_deny",
        status_code=200,
    )
    db_session.add(entry)
    await db_session.flush()

    with pytest.raises(Exception, match="immutable"):
        await db_session.execute(
            text(f"UPDATE audit_logs SET actor_email = 'hacked' WHERE id = {entry.id}")
        )
        await db_session.flush()


async def test_gdpr_redaction_still_works(
    httpx_client: httpx.AsyncClient, super_admin, db_session: AsyncSession
):
    """GDPR 脱敏路径通过 SET LOCAL 豁免后 UPDATE 应成功。"""
    from app.db.models.audit_log import AuditLog

    entry = AuditLog(
        actor_email="gdpr@redact.local",
        actor_role="annotator",
        action="test.gdpr",
        status_code=200,
    )
    db_session.add(entry)
    await db_session.flush()

    await db_session.execute(text("SET LOCAL \"app.allow_audit_update\" = 'true'"))
    await db_session.execute(
        text(f"UPDATE audit_logs SET actor_email = NULL WHERE id = {entry.id}")
    )
    await db_session.flush()

    await db_session.refresh(entry)
    assert entry.actor_email is None


# ── 最后登录追踪 ─────────────────────────────────────────────────────────


async def test_last_login_updated_on_login(
    httpx_client: httpx.AsyncClient, db_session: AsyncSession
):
    """登录成功后 last_login_at 应非空。"""
    from app.core.security import hash_password
    from app.db.models.user import User

    user = User(
        id=uuid.uuid4(),
        email="login_track@local",
        name="LoginTrack",
        password_hash=hash_password("Test1234"),
        role="annotator",
        is_active=True,
    )
    db_session.add(user)
    await db_session.flush()

    assert user.last_login_at is None

    r = await httpx_client.post(
        "/api/v1/auth/login",
        json={"email": "login_track@local", "password": "Test1234"},
    )
    assert r.status_code == 200

    await db_session.refresh(user)
    assert user.last_login_at is not None


# ── 失败登录详情增强 ─────────────────────────────────────────────────────


async def test_failed_login_user_agent_logged(
    httpx_client: httpx.AsyncClient, db_session: AsyncSession
):
    """失败登录的审计日志 detail 应包含 user_agent 字段。"""
    from app.db.models.audit_log import AuditLog

    r = await httpx_client.post(
        "/api/v1/auth/login",
        json={"email": "nonexist@fail.local", "password": "wrong"},
        headers={"User-Agent": "TestBot/1.0"},
    )
    assert r.status_code == 401

    result = await db_session.execute(
        select(AuditLog).where(
            AuditLog.action == "auth.login",
            AuditLog.target_id == "nonexist@fail.local",
        )
    )
    audit = result.scalar_one_or_none()
    assert audit is not None
    assert audit.detail_json["user_agent"] == "TestBot/1.0"
