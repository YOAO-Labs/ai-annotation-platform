"""Public degraded health does not expose internal exception details."""

import httpx
import pytest

from app.api import health


@pytest.fixture
async def health_client(app_module):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_module), base_url="http://test"
    ) as client:
        yield client


@pytest.mark.asyncio
@pytest.mark.parametrize("component", ["db", "redis", "minio", "celery"])
async def test_public_component_health_redacts_diagnostics(
    monkeypatch, health_client, component
):
    raw = {
        "status": "error",
        "latency_ms": None,
        "detail": "postgresql://user:secret@internal/db",
    }
    if component in {"db", "redis"}:

        async def fake():
            return raw
    else:

        def fake():
            return raw

    monkeypatch.setattr(health, f"_check_{component}", fake)
    response = await health_client.get(f"/health/{component}")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "error" and "secret" not in body["detail"]
    assert raw["detail"] == "postgresql://user:secret@internal/db"


@pytest.mark.asyncio
async def test_overall_public_health_preserves_check_status_and_redacts(
    monkeypatch, health_client
):
    async def db():
        return {"status": "error", "detail": "secret connection failure"}

    async def redis():
        return {"status": "ok", "latency_ms": 1.2}

    monkeypatch.setattr(health, "_check_db", db)
    monkeypatch.setattr(health, "_check_redis", redis)
    monkeypatch.setattr(health, "_check_minio", lambda: {"status": "ok"})
    monkeypatch.setattr(
        health,
        "_check_celery",
        lambda: {
            "status": "error",
            "detail": "secret broker failure",
            "queues": [],
            "workers": [],
        },
    )
    response = await health_client.get("/health")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"]["redis"]["latency_ms"] == 1.2
    assert "secret" not in response.text
