"""Open registration tests."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import AsyncClient


@pytest.fixture
def enable_open_registration():
    with patch("app.config.settings.allow_open_registration", True):
        yield


@pytest.fixture
def disable_open_registration():
    with patch("app.config.settings.allow_open_registration", False):
        yield


@pytest.mark.asyncio
async def test_registration_status_returns_config(
    httpx_client: AsyncClient, enable_open_registration
):
    resp = await httpx_client.get("/api/v1/auth/registration-status")
    assert resp.status_code == 200
    assert resp.json()["open_registration_enabled"] is True


@pytest.mark.asyncio
async def test_registration_status_disabled(
    httpx_client: AsyncClient, disable_open_registration
):
    resp = await httpx_client.get("/api/v1/auth/registration-status")
    assert resp.status_code == 200
    assert resp.json()["open_registration_enabled"] is False


@pytest.mark.asyncio
async def test_open_register_disabled_returns_403(
    httpx_client: AsyncClient, disable_open_registration
):
    resp = await httpx_client.post(
        "/api/v1/auth/register-open",
        json={"email": "new@test.com", "name": "New", "password": "Abcd1234"},
    )
    assert resp.status_code == 403
    assert "未启用" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_open_register_success(
    httpx_client: AsyncClient, enable_open_registration
):
    resp = await httpx_client.post(
        "/api/v1/auth/register-open",
        json={
            "email": "newuser@example.com",
            "name": "NewUser",
            "password": "Abcd1234",
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["access_token"]
    assert data["user"]["role"] == "viewer"
    assert data["user"]["email"] == "newuser@example.com"


@pytest.mark.asyncio
async def test_open_register_duplicate_email_409(
    httpx_client: AsyncClient, enable_open_registration, super_admin
):
    admin_user, _ = super_admin
    resp = await httpx_client.post(
        "/api/v1/auth/register-open",
        json={"email": admin_user.email, "name": "Dup", "password": "Abcd1234"},
    )
    assert resp.status_code == 409
    assert "已被注册" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_open_register_weak_password_422(
    httpx_client: AsyncClient, enable_open_registration
):
    resp = await httpx_client.post(
        "/api/v1/auth/register-open",
        json={"email": "weak@test.com", "name": "Weak", "password": "short"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_open_register_invalid_email_422(
    httpx_client: AsyncClient, enable_open_registration
):
    resp = await httpx_client.post(
        "/api/v1/auth/register-open",
        json={"email": "notanemail", "name": "Bad", "password": "Abcd1234"},
    )
    assert resp.status_code == 422


@pytest.fixture
def reset_rate_limiter():
    """clear slowapi 内存计数器，避免本文件前置测试用满 3/min 触 429。"""
    from app.core.ratelimit import limiter

    limiter.reset()
    yield
    limiter.reset()


@pytest.mark.asyncio
async def test_open_register_captcha_required_when_enabled(
    httpx_client: AsyncClient, enable_open_registration, reset_rate_limiter
):
    """turnstile_enabled=True 且未带 token 时 400 captcha_failed。"""
    with (
        patch("app.services.captcha_service.settings.turnstile_enabled", True),
        patch("app.services.captcha_service.settings.turnstile_secret_key", "sk"),
    ):
        resp = await httpx_client.post(
            "/api/v1/auth/register-open",
            json={
                "email": "captcha-miss@test.com",
                "name": "X",
                "password": "Abcd1234",
            },
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "captcha_failed"


@pytest.mark.asyncio
async def test_open_register_captcha_token_valid_passes(
    httpx_client: AsyncClient, enable_open_registration, reset_rate_limiter
):
    """turnstile_enabled=True 且 token 通过校验时正常注册。"""
    with (
        patch("app.services.captcha_service.settings.turnstile_enabled", True),
        patch("app.services.captcha_service.settings.turnstile_secret_key", "sk"),
        patch(
            "app.services.captcha_service.verify_turnstile_token",
            return_value=True,
        ),
    ):
        with patch("app.api.v1.auth.verify_turnstile_token", return_value=True):
            resp = await httpx_client.post(
                "/api/v1/auth/register-open",
                json={
                    "email": "captcha-ok@test.com",
                    "name": "Y",
                    "password": "Abcd1234",
                    "captcha_token": "valid-cf-token",
                },
            )
            assert resp.status_code == 201
