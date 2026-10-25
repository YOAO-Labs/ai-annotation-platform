"""Open-registration email verification behavior."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aap_identity.recovery_crypto import token_hash
from app.config import settings
from app.db.models.email_verification_token import EmailVerificationToken
from app.db.models.user import User
from tests.identity_recovery_helpers import credential_mail_capture  # noqa: F401


@pytest.fixture
def enable_open_registration():
    with patch("app.config.settings.allow_open_registration", True):
        yield


@pytest.fixture
def require_verification():
    with patch("app.config.settings.require_email_verification", True):
        yield


@pytest.fixture
def no_verification():
    with patch("app.config.settings.require_email_verification", False):
        yield


@pytest.fixture
def reset_rate_limiter():
    from app.core.ratelimit import limiter

    limiter.reset()
    yield
    limiter.reset()


# ---- 环境派生默认值 ----


def test_email_verification_required_derives_from_environment():
    """None → production 开、dev/staging 关；显式值覆盖派生。"""
    with patch("app.config.settings.require_email_verification", None):
        with patch("app.config.settings.environment", "production"):
            assert settings.email_verification_required is True
        with patch("app.config.settings.environment", "development"):
            assert settings.email_verification_required is False
        with patch("app.config.settings.environment", "staging"):
            assert settings.email_verification_required is False
    # 显式 True 覆盖（即便 dev 环境）
    with patch("app.config.settings.require_email_verification", True):
        with patch("app.config.settings.environment", "development"):
            assert settings.email_verification_required is True


# ---- 关闭（dev 默认）：行为同历史 ----


@pytest.mark.asyncio
async def test_register_without_verification_auto_login(
    httpx_client: AsyncClient, enable_open_registration, no_verification
):
    resp = await httpx_client.post(
        "/api/v1/auth/register-open",
        json={"email": "noverify@test.com", "name": "N", "password": "Abcd1234"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["access_token"]
    assert data["email_verification_required"] is False
    assert data["user"]["email_verified_at"] is not None


# ---- 打开（生产默认）：注册不自动登录 + 登录被挡 ----


@pytest.mark.asyncio
async def test_register_with_verification_no_token(
    httpx_client: AsyncClient,
    db_session: AsyncSession,
    enable_open_registration,
    require_verification,
    credential_mail_capture,  # noqa: F811
    caplog,
):
    caplog.set_level("INFO")
    resp = await httpx_client.post(
        "/api/v1/auth/register-open",
        json={"email": "needverify@test.com", "name": "V", "password": "Abcd1234"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["access_token"] is None
    assert data["email_verification_required"] is True
    assert data["user"]["email_verified_at"] is None
    tokens = credential_mail_capture.tokens()
    assert len(tokens) == 1
    row = await db_session.scalar(
        select(EmailVerificationToken).where(
            EmailVerificationToken.token_hash == token_hash(tokens[0])
        )
    )
    assert row is not None
    assert row.issued_email == data["user"]["email"]
    assert row.used_at is None
    assert tokens[0] not in caplog.text


@pytest.mark.asyncio
async def test_login_blocked_when_unverified(
    httpx_client: AsyncClient,
    enable_open_registration,
    require_verification,
    reset_rate_limiter,
    credential_mail_capture,  # noqa: F811
):
    await httpx_client.post(
        "/api/v1/auth/register-open",
        json={"email": "blocked@test.com", "name": "B", "password": "Abcd1234"},
    )
    resp = await httpx_client.post(
        "/api/v1/auth/login",
        json={"email": "blocked@test.com", "password": "Abcd1234"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "email_not_verified"


# ---- verify-email 消费 token → 可登录 ----


@pytest.mark.asyncio
async def test_verify_then_login(
    httpx_client: AsyncClient,
    db_session: AsyncSession,
    enable_open_registration,
    require_verification,
    reset_rate_limiter,
    credential_mail_capture,  # noqa: F811
):
    email = "verifyme@test.com"
    await httpx_client.post(
        "/api/v1/auth/register-open",
        json={"email": email, "name": "M", "password": "Abcd1234"},
    )
    user = (
        await db_session.execute(select(User).where(User.email == email))
    ).scalar_one()
    assert user.email == email
    tokens = credential_mail_capture.tokens()
    assert len(tokens) == 1
    token_value = tokens[0]
    token = (
        await db_session.execute(
            select(EmailVerificationToken).where(
                EmailVerificationToken.token_hash == token_hash(token_value)
            )
        )
    ).scalar_one()

    resp = await httpx_client.post(
        "/api/v1/auth/verify-email", json={"token": token_value}
    )
    assert resp.status_code == 200
    await db_session.refresh(token)
    assert token.used_at is not None

    login = await httpx_client.post(
        "/api/v1/auth/login", json={"email": email, "password": "Abcd1234"}
    )
    assert login.status_code == 200
    assert login.json()["access_token"]


@pytest.mark.asyncio
async def test_verify_invalid_token_400(httpx_client: AsyncClient):
    resp = await httpx_client.post(
        "/api/v1/auth/verify-email", json={"token": "deadbeef"}
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_verify_token_reuse_rejected(
    httpx_client: AsyncClient,
    db_session: AsyncSession,
    enable_open_registration,
    require_verification,
    credential_mail_capture,  # noqa: F811
):
    email = "reuse@test.com"
    await httpx_client.post(
        "/api/v1/auth/register-open",
        json={"email": email, "name": "R", "password": "Abcd1234"},
    )
    user = (
        await db_session.execute(select(User).where(User.email == email))
    ).scalar_one()
    assert user.email == email
    tokens = credential_mail_capture.tokens()
    assert len(tokens) == 1
    token_value = tokens[0]
    token = (
        await db_session.execute(
            select(EmailVerificationToken).where(
                EmailVerificationToken.token_hash == token_hash(token_value)
            )
        )
    ).scalar_one()

    first = await httpx_client.post(
        "/api/v1/auth/verify-email", json={"token": token_value}
    )
    assert first.status_code == 200
    second = await httpx_client.post(
        "/api/v1/auth/verify-email", json={"token": token_value}
    )
    assert second.status_code == 400
    await db_session.refresh(token)
    assert token.used_at is not None


# ---- resend 防枚举：恒 202 ----


@pytest.mark.asyncio
async def test_resend_verification_always_202(
    httpx_client: AsyncClient,
    db_session: AsyncSession,
    reset_rate_limiter,
    credential_mail_capture,  # noqa: F811
):
    user = User(
        email="resend@test.com",
        name="Pending verification",
        password_hash="test-only",
        role="viewer",
        status="offline",
        is_active=True,
        email_verified_at=None,
    )
    db_session.add(user)
    await db_session.flush()

    known = await httpx_client.post(
        "/api/v1/auth/send-verification-email",
        json={"email": user.email},
    )
    assert known.status_code == 202
    tokens = credential_mail_capture.tokens()
    assert len(tokens) == 1
    row = await db_session.scalar(
        select(EmailVerificationToken).where(
            EmailVerificationToken.token_hash == token_hash(tokens[0])
        )
    )
    assert row is not None
    assert row.issued_email == user.email
    assert row.used_at is None

    missing = await httpx_client.post(
        "/api/v1/auth/send-verification-email",
        json={"email": "nobody@test.com"},
    )
    assert missing.status_code == known.status_code == 202
    assert missing.json() == known.json()
