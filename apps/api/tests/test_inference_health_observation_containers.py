"""Public health check through the retained Inference Compose owner."""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.db.models.ml_backend_pool import MLBackendPoolMember, MLBackendServicePool
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.db.models.user import User
from tests.conftest import create_registry_with_pool
from tests.factory import create_user
from tests.inference_health_observation_helpers import backend_server
from tests.inference_owner_container_helpers import (
    inference_owner_container,
    inference_owner_live,
)
from tests.test_workspace_configuration_live import _auth, _suppress_request_audit

pytestmark = [pytest.mark.asyncio, inference_owner_live]


async def test_public_health_uses_observer_image_survives_restart_and_fails_without_fallback(
    test_engine, httpx_client, app_module, monkeypatch
):
    from app.deps import get_db

    _suppress_request_audit(monkeypatch)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)

    async def request_db():
        async with maker() as db:
            try:
                yield db
            finally:
                await db.rollback()

    monkeypatch.setitem(app_module.dependency_overrides, get_db, request_db)
    monkeypatch.setattr(settings, "inference_health_observation_token_file", "")
    actor_id = registry_id = pool_id = None
    try:
        async with inference_owner_container(test_engine, monkeypatch) as owner:
            monkeypatch.setattr(
                settings, "inference_health_observation_url", owner.origin
            )
            monkeypatch.setattr(
                settings,
                "inference_health_observation_token",
                owner.tokens["health_observation"],
            )
            with backend_server(host=owner.gateway) as (backend_url, calls):
                async with maker.begin() as seed:
                    actor = await create_user(
                        seed,
                        "super_admin",
                        f"health-image-{uuid.uuid4().hex}@example.test",
                        "Health image operator",
                    )
                    actor_id = actor.id
                    backend, pool = await create_registry_with_pool(
                        seed,
                        name="Image health",
                        url=backend_url,
                        state="disconnected",
                        is_interactive=False,
                        auth_method="token",
                        auth_token="image-health-token",
                        extra_params={},
                    )
                    registry_id = backend.id
                    pool_id = pool.id
                    authorization = _auth(actor)
                route = f"/api/v1/admin/ml-integrations/registry/{registry_id}/health"

                async def health(*, allow_superseded=False):
                    response = await httpx_client.post(route, headers=authorization)
                    assert response.status_code == 200, response.text
                    body = response.json()
                    assert body == {
                        "status": body["status"],
                        "backend_id": str(registry_id),
                        "backend_name": "Image health",
                    }
                    assert body["status"] in {"ok", "error"}
                    if not allow_superseded:
                        assert body["status"] == "ok"
                    return body["status"]

                await health()
                async with maker() as outside:
                    first = await outside.get(MLBackendRegistry, registry_id)
                    first_checked = first.last_checked_at
                    assert first_checked is not None and first.state == "connected"
                    assert first.health_meta["capabilities"]
                await owner.restart()
                monkeypatch.setattr(
                    settings, "inference_health_observation_url", owner.origin
                )
                await health()
                limiter = asyncio.Semaphore(4)

                async def measured():
                    async with limiter:
                        started = time.monotonic()
                        status = await health(allow_superseded=True)
                        return time.monotonic() - started, status

                outcomes = await asyncio.gather(*(measured() for _ in range(16)))
                samples = sorted(seconds for seconds, _ in outcomes)
                owner.evidence["local_sample"] = {
                    "requests": 16,
                    "concurrency": 4,
                    "p50_seconds": samples[7],
                    "p95_seconds": samples[15],
                    "superseded_observations": sum(
                        status == "error" for _, status in outcomes
                    ),
                    "api_database_backend_outside_container_budget": True,
                }
                assert len(calls) == 36
                assert all(
                    authorization == "Bearer image-health-token"
                    for _, authorization in calls
                )
                async with maker() as outside:
                    last = await outside.get(MLBackendRegistry, registry_id)
                    checked_before_outage = last.last_checked_at
                    assert checked_before_outage > first_checked
                owner.stop()
                count = len(calls)
                response = await httpx_client.post(route, headers=authorization)
                assert response.status_code == 503
                assert (
                    response.json()["detail"]["error_code"]
                    == "inference_health_observation_unavailable"
                )
                assert len(calls) == count
                async with maker() as outside:
                    current = await outside.get(MLBackendRegistry, registry_id)
                    assert current.last_checked_at == checked_before_outage
                owner.evidence["health_finalize_and_fail_closed_verified"] = True
    finally:
        async with maker.begin() as cleanup:
            if registry_id is not None:
                await cleanup.execute(
                    delete(MLBackendPoolMember).where(
                        MLBackendPoolMember.registry_id == registry_id
                    )
                )
            if pool_id is not None:
                await cleanup.execute(
                    delete(MLBackendServicePool).where(
                        MLBackendServicePool.id == pool_id
                    )
                )
            if registry_id is not None:
                await cleanup.execute(
                    delete(MLBackendRegistry).where(MLBackendRegistry.id == registry_id)
                )
            if actor_id is not None:
                await cleanup.execute(delete(User).where(User.id == actor_id))
