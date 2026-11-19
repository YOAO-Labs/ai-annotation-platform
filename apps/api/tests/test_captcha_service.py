"""Cloudflare Turnstile CAPTCHA service tests."""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

import httpx
import pytest

from app.services.captcha_service import verify_turnstile_token


@contextmanager
def _patched_httpx(transport: httpx.MockTransport):
    """让 captcha_service 内部的 `httpx.AsyncClient(...)` 用我们的 MockTransport。"""
    real = httpx.AsyncClient

    def make_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    with patch(
        "app.services.captcha_service.httpx.AsyncClient", side_effect=make_client
    ):
        yield


@pytest.mark.asyncio
async def test_disabled_short_circuits_true():
    with patch("app.services.captcha_service.settings.turnstile_enabled", False):
        assert await verify_turnstile_token("anything") is True
        assert await verify_turnstile_token(None) is True
        assert await verify_turnstile_token("") is True


@pytest.mark.asyncio
async def test_enabled_no_secret_returns_false():
    with (
        patch("app.services.captcha_service.settings.turnstile_enabled", True),
        patch("app.services.captcha_service.settings.turnstile_secret_key", None),
    ):
        assert await verify_turnstile_token("token") is False


@pytest.mark.asyncio
async def test_enabled_empty_token_returns_false():
    with (
        patch("app.services.captcha_service.settings.turnstile_enabled", True),
        patch("app.services.captcha_service.settings.turnstile_secret_key", "sk"),
    ):
        assert await verify_turnstile_token("") is False
        assert await verify_turnstile_token(None) is False


@pytest.mark.asyncio
async def test_enabled_success_response():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"success": True, "challenge_ts": "x"})
    )
    with (
        patch("app.services.captcha_service.settings.turnstile_enabled", True),
        patch("app.services.captcha_service.settings.turnstile_secret_key", "sk"),
        _patched_httpx(transport),
    ):
        assert await verify_turnstile_token("good-token", "1.2.3.4") is True


@pytest.mark.asyncio
async def test_enabled_failed_response():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, json={"success": False, "error-codes": ["invalid-input-response"]}
        )
    )
    with (
        patch("app.services.captcha_service.settings.turnstile_enabled", True),
        patch("app.services.captcha_service.settings.turnstile_secret_key", "sk"),
        _patched_httpx(transport),
    ):
        assert await verify_turnstile_token("bad-token") is False


@pytest.mark.asyncio
async def test_enabled_network_error_returns_false():
    def boom(request):
        raise httpx.ConnectError("boom")

    transport = httpx.MockTransport(boom)
    with (
        patch("app.services.captcha_service.settings.turnstile_enabled", True),
        patch("app.services.captcha_service.settings.turnstile_secret_key", "sk"),
        _patched_httpx(transport),
    ):
        assert await verify_turnstile_token("any") is False


@pytest.mark.asyncio
async def test_enabled_non_200_response_returns_false():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(503, text="upstream")
    )
    with (
        patch("app.services.captcha_service.settings.turnstile_enabled", True),
        patch("app.services.captcha_service.settings.turnstile_secret_key", "sk"),
        _patched_httpx(transport),
    ):
        assert await verify_turnstile_token("any") is False
