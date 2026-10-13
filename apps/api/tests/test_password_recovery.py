"""账户恢复：邮件投递、统一响应、单次消费和会话失效。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ratelimit import limiter
from app.core.security import (
    create_access_token,
    hash_password,
    verify_password,
)
from app.core.token_blacklist import get_user_generation, increment_user_generation
from app.config import settings
from app.db.models.password_reset_token import PasswordResetToken
from app.db.models.user import User
from app.services.email import SmtpConfigError
from app.services.password_reset import PasswordResetService

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture(autouse=True)
def _background_database(db_session, monkeypatch):
    # Background delivery opens its own session in production. Keep this test
    # within the fixture's rollback boundary while exercising the same task.
    @asynccontextmanager
    async def session():
        yield db_session

    monkeypatch.setattr("app.api.v1.auth.async_session", session)


async def test_forgot_password_sends_reset_email_and_keeps_response_uniform(
    httpx_client,
    annotator,
    db_session: AsyncSession,
    monkeypatch,
):
    user, _ = annotator
    captured: dict[str, object] = {}

    async def fake_send(
        db: AsyncSession,
        to_address: str,
        reset_url: str,
        *,
        expires_in_hours: int,
    ) -> None:
        captured.update(
            to=to_address,
            url=reset_url,
            expires_in_hours=expires_in_hours,
        )

    monkeypatch.setattr("app.api.v1.auth.send_password_reset_email", fake_send)
    info_messages: list[str] = []
    monkeypatch.setattr(
        "app.api.v1.auth.logger.info",
        lambda message, *args: info_messages.append(message % args),
    )

    response = await httpx_client.post(
        "/api/v1/auth/forgot-password",
        json={"email": user.email, "captcha_token": None},
    )

    assert response.status_code == 202
    assert "如果该邮箱已注册" in response.json()["message"]
    assert captured["to"] == user.email
    assert captured["expires_in_hours"] == PasswordResetService.TOKEN_EXPIRY_HOURS

    token = str(captured["url"]).split("token=", 1)[1]
    row = await db_session.scalar(
        select(PasswordResetToken).where(PasswordResetToken.token == token)
    )
    assert row is not None
    assert row.used_at is None
    assert token not in " ".join(info_messages)

    missing = await httpx_client.post(
        "/api/v1/auth/forgot-password",
        json={"email": "missing@example.com", "captcha_token": None},
    )
    assert missing.status_code == response.status_code
    assert missing.json() == response.json()


async def test_forgot_password_smtp_failure_keeps_uniform_response_and_invalidates_token(
    httpx_client,
    annotator,
    db_session: AsyncSession,
    monkeypatch,
):
    user, _ = annotator
    captured: dict[str, str] = {}

    async def fail_send(
        db: AsyncSession,
        to_address: str,
        reset_url: str,
        *,
        expires_in_hours: int,
    ) -> None:
        captured["url"] = reset_url
        raise SmtpConfigError("SMTP 未配置")

    monkeypatch.setattr("app.api.v1.auth.send_password_reset_email", fail_send)
    warning_messages: list[str] = []
    monkeypatch.setattr(
        "app.api.v1.auth.logger.warning",
        lambda message, *args: warning_messages.append(message % args),
    )

    response = await httpx_client.post(
        "/api/v1/auth/forgot-password",
        json={"email": user.email, "captcha_token": None},
    )

    assert response.status_code == 202
    token = captured["url"].split("token=", 1)[1]
    row = await db_session.scalar(
        select(PasswordResetToken).where(PasswordResetToken.token == token)
    )
    assert row is not None
    assert row.used_at is not None
    assert "SMTP 未配置" in " ".join(warning_messages)
    assert token not in " ".join(warning_messages)


async def test_reset_password_is_single_use_and_invalidates_old_sessions(
    httpx_client,
    annotator,
    db_session: AsyncSession,
):
    user, _ = annotator
    service = PasswordResetService(db_session)
    reset_token = await service.create_token(user.email)
    assert reset_token
    await db_session.flush()

    old_session = create_access_token(subject=str(user.id), role=user.role, gen=0)
    response = await httpx_client.post(
        "/api/v1/auth/reset-password",
        json={"token": reset_token, "new_password": "ResetStrong1"},
    )

    assert response.status_code == 200, response.text
    assert verify_password("ResetStrong1", user.password_hash)

    old_me = await httpx_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {old_session}"},
    )
    assert old_me.status_code == 401

    reused = await httpx_client.post(
        "/api/v1/auth/reset-password",
        json={"token": reset_token, "new_password": "ResetStrong2"},
    )
    assert reused.status_code == 400
    assert "无效或已过期" in reused.text

    login = await httpx_client.post(
        "/api/v1/auth/login",
        json={"email": user.email, "password": "ResetStrong1"},
    )
    assert login.status_code == 200, login.text


async def test_reset_password_rejects_expired_token(
    httpx_client,
    annotator,
    db_session: AsyncSession,
):
    user, _ = annotator
    row = PasswordResetToken(
        user_id=user.id,
        token="expired-token",
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    db_session.add(row)
    await db_session.flush()

    response = await httpx_client.post(
        "/api/v1/auth/reset-password",
        json={"token": row.token, "new_password": "ResetStrong1"},
    )

    assert response.status_code == 400
    assert "无效或已过期" in response.text


async def test_reset_password_rejects_token_for_deactivated_user(
    httpx_client,
    annotator,
    db_session: AsyncSession,
):
    user, _ = annotator
    service = PasswordResetService(db_session)
    reset_token = await service.create_token(user.email)
    assert reset_token
    user.is_active = False
    await db_session.flush()

    response = await httpx_client.post(
        "/api/v1/auth/reset-password",
        json={"token": reset_token, "new_password": "ResetStrong1"},
    )

    assert response.status_code == 400
    row = await db_session.scalar(
        select(PasswordResetToken).where(PasswordResetToken.token == reset_token)
    )
    assert row is not None
    assert row.used_at is not None


async def test_concurrent_consumers_can_use_a_reset_token_only_once(test_engine):
    """两个真实数据库事务并行消费同一 token 时，行锁只允许一个成功。"""
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    user_id = uuid4()
    token_value = f"concurrent-{uuid4().hex}"

    async with maker() as setup:
        setup.add(
            User(
                id=user_id,
                email=f"concurrent-{uuid4().hex}@local",
                name="Concurrent Reset",
                password_hash=hash_password("Test1234"),
                role="employee",
                is_active=True,
            )
        )
        await setup.flush()
        setup.add(
            PasswordResetToken(
                user_id=user_id,
                token=token_value,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
        )
        await setup.commit()

    first_locked = asyncio.Event()

    async def consume(hold_lock: bool) -> bool:
        async with maker() as session:
            async with session.begin():
                user = await PasswordResetService(session).consume_token(token_value)
                if hold_lock:
                    first_locked.set()
                    await asyncio.sleep(0.2)
                return user is not None

    try:
        first = asyncio.create_task(consume(True))
        await asyncio.wait_for(first_locked.wait(), timeout=2)
        second = asyncio.create_task(consume(False))
        first_result, second_result = await asyncio.gather(first, second)
        assert sorted((first_result, second_result)) == [False, True]
    finally:
        async with maker() as cleanup:
            await cleanup.execute(
                delete(PasswordResetToken).where(
                    PasswordResetToken.token == token_value
                )
            )
            await cleanup.execute(delete(User).where(User.id == user_id))
            await cleanup.commit()


async def test_reset_user_lock_serializes_old_password_login(test_engine):
    """重置持有用户锁时，并发登录不能用旧密码拿到新 generation。"""
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    user_id = uuid4()
    token_value = f"login-race-{uuid4().hex}"
    email = f"login-race-{uuid4().hex}@local"

    async with maker() as setup:
        setup.add(
            User(
                id=user_id,
                email=email,
                name="Login Race",
                password_hash=hash_password("OldStrong1"),
                role="employee",
                is_active=True,
            )
        )
        await setup.flush()
        setup.add(
            PasswordResetToken(
                user_id=user_id,
                token=token_value,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
        )
        await setup.commit()

    reset_locked = asyncio.Event()

    async def reset_password_in_transaction() -> None:
        async with maker() as session:
            async with session.begin():
                user = await PasswordResetService(session).consume_token(token_value)
                assert user is not None
                user.password_hash = hash_password("NewStrong1")
                await increment_user_generation(str(user.id))
                reset_locked.set()
                await asyncio.sleep(0.2)

    async def login_with_old_password() -> tuple[bool, int]:
        async with maker() as session:
            async with session.begin():
                user = await session.scalar(
                    select(User).where(User.email == email).with_for_update()
                )
                assert user is not None
                return (
                    verify_password("OldStrong1", user.password_hash),
                    await get_user_generation(str(user.id)),
                )

    try:
        reset_task = asyncio.create_task(reset_password_in_transaction())
        await asyncio.wait_for(reset_locked.wait(), timeout=2)
        login_task = asyncio.create_task(login_with_old_password())
        await reset_task
        old_password_valid, observed_generation = await login_task
        assert old_password_valid is False
        assert observed_generation >= 1
    finally:
        async with maker() as cleanup:
            await cleanup.execute(
                delete(PasswordResetToken).where(
                    PasswordResetToken.token == token_value
                )
            )
            await cleanup.execute(delete(User).where(User.id == user_id))
            await cleanup.commit()
        import redis.asyncio as aioredis

        redis = aioredis.from_url(settings.redis_url)
        try:
            await redis.delete(f"token_gen:{user_id}")
        finally:
            await redis.aclose()
