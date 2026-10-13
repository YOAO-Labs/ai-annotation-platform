"""v0.10.58 · super_admin system health aggregate endpoint."""

from __future__ import annotations

import pytest


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_admin_system_health_super_admin_only(
    httpx_client, annotator, super_admin, monkeypatch
):
    from app.api.v1 import admin_system_health

    async def fake_db():
        return {"status": "ok", "latency_ms": 1.2}

    async def fake_redis():
        return {"status": "ok", "latency_ms": 2.3}

    monkeypatch.setattr(admin_system_health.health, "_check_db", fake_db)
    monkeypatch.setattr(admin_system_health.health, "_check_redis", fake_redis)
    monkeypatch.setattr(
        admin_system_health.health,
        "_check_minio",
        lambda: {"status": "ok", "latency_ms": 3.4},
    )
    monkeypatch.setattr(
        admin_system_health.health,
        "_check_celery",
        lambda: {
            "status": "ok",
            "latency_ms": 4.5,
            "active_count": 1,
            "workers": [
                {
                    "name": "celery@test",
                    "last_heartbeat_seconds_ago": 10,
                    "pool_max": 4,
                }
            ],
            "queues": [{"name": "ml", "length": 3}],
        },
    )

    _, annotator_token = annotator
    forbidden = await httpx_client.get(
        "/api/v1/admin/system-health", headers=_bearer(annotator_token)
    )
    assert forbidden.status_code == 403

    _, admin_token = super_admin
    resp = await httpx_client.get(
        "/api/v1/admin/system-health", headers=_bearer(admin_token)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert [item["name"] for item in body["components"]] == [
        "db",
        "redis",
        "minio",
        "celery",
    ]
    assert body["celery"]["workers"][0]["status"] == "ok"
    assert body["celery"]["queues"][0] == {
        "name": "ml",
        "length": 3,
        "status": "ok",
    }


@pytest.mark.asyncio
async def test_admin_system_health_marks_stale_worker_degraded(
    httpx_client, super_admin, monkeypatch
):
    from app.api.v1 import admin_system_health

    async def fake_ok():
        return {"status": "ok", "latency_ms": 1}

    monkeypatch.setattr(admin_system_health.health, "_check_db", fake_ok)
    monkeypatch.setattr(admin_system_health.health, "_check_redis", fake_ok)
    monkeypatch.setattr(
        admin_system_health.health,
        "_check_minio",
        lambda: {"status": "ok", "latency_ms": 1},
    )
    monkeypatch.setattr(
        admin_system_health.health,
        "_check_celery",
        lambda: {
            "status": "ok",
            "latency_ms": 1,
            "active_count": 1,
            "workers": [
                {
                    "name": "celery@test",
                    "last_heartbeat_seconds_ago": 180,
                    "pool_max": 4,
                }
            ],
            "queues": [{"name": "ml", "length": 10}],
        },
    )

    _, admin_token = super_admin
    resp = await httpx_client.get(
        "/api/v1/admin/system-health", headers=_bearer(admin_token)
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["components"][3]["status"] == "degraded"
    assert body["celery"]["workers"][0]["status"] == "degraded"
