"""登录 progressive CAPTCHA。

通过 monkeypatch login_failed_counter 的三个函数避开 redis 依赖。
"""

from __future__ import annotations

import pytest


class _Counter:
    def __init__(self) -> None:
        self.values: dict[str, int] = {}

    async def get_count(self, ip: str) -> int:
        return self.values.get(ip, 0)

    async def increment(self, ip: str) -> int:
        self.values[ip] = self.values.get(ip, 0) + 1
        return self.values[ip]

    async def reset(self, ip: str) -> None:
        self.values.pop(ip, None)


@pytest.fixture
def fake_counter(monkeypatch):
    counter = _Counter()
    monkeypatch.setattr(
        "app.api.v1.auth.login_failed_counter.get_count", counter.get_count
    )
    monkeypatch.setattr(
        "app.api.v1.auth.login_failed_counter.increment", counter.increment
    )
    monkeypatch.setattr("app.api.v1.auth.login_failed_counter.reset", counter.reset)
    return counter


@pytest.mark.asyncio
async def test_failed_login_returns_count_header(httpx_client, fake_counter):
    res = await httpx_client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@test.local", "password": "wrong"},
    )
    assert res.status_code == 401
    assert res.headers.get("x-login-failed-count") == "1"
    assert fake_counter.values


@pytest.mark.asyncio
async def test_captcha_required_after_threshold(
    httpx_client, fake_counter, monkeypatch
):
    """达到阈值后无 captcha_token 应返 400 captcha_required（turnstile 启用时）。"""

    async def fake_verify(token, ip):  # noqa: ARG001
        return bool(token)

    monkeypatch.setattr("app.api.v1.auth.verify_turnstile_token", fake_verify)

    counter = fake_counter
    counter.values["127.0.0.1"] = 5

    res = await httpx_client.post(
        "/api/v1/auth/login",
        json={"email": "x@test.local", "password": "wrong"},
    )
    assert res.status_code == 400
    assert res.json()["detail"] == "captcha_required"
    assert res.headers.get("x-login-failed-count") == "5"


@pytest.mark.asyncio
async def test_successful_login_resets_counter(httpx_client, fake_counter, super_admin):
    user, _ = super_admin
    fake_counter.values["127.0.0.1"] = 3

    res = await httpx_client.post(
        "/api/v1/auth/login",
        json={"email": user.email, "password": "Test1234"},
    )
    assert res.status_code == 200
    assert "127.0.0.1" not in fake_counter.values


@pytest.mark.asyncio
async def test_captcha_disabled_short_circuit_passes(
    httpx_client, fake_counter, super_admin, monkeypatch
):
    """turnstile_enabled=False 时 verify_turnstile_token 直接返 True，
    达阈值后即使 captcha_token=None 也能继续走原密码校验。"""
    user, _ = super_admin
    fake_counter.values["127.0.0.1"] = 5

    res = await httpx_client.post(
        "/api/v1/auth/login",
        json={"email": user.email, "password": "wrong"},
    )
    assert res.status_code == 401
    assert fake_counter.values["127.0.0.1"] == 6
