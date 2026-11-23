"""Public Workspace configuration through the retained Inference Compose owner."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from tests.conftest import create_registry_with_pool
from tests.factory import create_user
from tests.inference_owner_container_helpers import (
    assert_ingress_capability_isolation,
    inference_owner_container,
    inference_owner_live,
)
from tests.test_inference_backend_probe_bindings import setup_backend
from tests.test_workspace_configuration_live import _auth, _cleanup_world
from tests.workspace_configuration_helpers import public_workspace_client

pytestmark = [pytest.mark.asyncio, inference_owner_live]


async def test_public_configuration_probes_through_restricted_inference_container(
    test_engine, app_module, monkeypatch
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_id = registry_id = None
    try:
        async with inference_owner_container(test_engine, monkeypatch) as owner:
            await assert_ingress_capability_isolation(owner)
            monkeypatch.setattr(settings, "inference_backend_probe_url", owner.origin)
            monkeypatch.setattr(
                settings, "inference_backend_probe_token", owner.tokens["backend_probe"]
            )
            monkeypatch.setattr(settings, "inference_backend_probe_token_file", "")
            with setup_backend(owner.gateway) as (backend_url, calls):
                async with maker.begin() as seed:
                    actor = await create_user(
                        seed,
                        "project_admin",
                        f"probe-container-{uuid.uuid4().hex}@example.test",
                        "Probe container actor",
                    )
                    actor_id = actor.id
                    registry, _pool = await create_registry_with_pool(
                        seed,
                        name="Container probe",
                        url=backend_url + "/container",
                        state="disconnected",
                        is_interactive=False,
                        auth_method="token",
                        auth_token="snapshot-secret",
                        extra_params={},
                    )
                    registry_id = registry.id
                request = {
                    "name": "Container probe project",
                    "type_label": "Image",
                    "ml_backend_source_id": str(registry_id),
                }
                async with public_workspace_client(
                    test_engine, app_module, monkeypatch
                ) as client:
                    response = await client.post(
                        "/api/v1/projects", json=request, headers=_auth(actor)
                    )
                    assert response.status_code == 200, response.text
                    assert calls == [
                        ("/container/setup", "Bearer snapshot-secret", "no-cache")
                    ]
                    owner.stop()
                    failed = await client.post(
                        "/api/v1/projects",
                        json={**request, "name": "Provider unavailable"},
                        headers=_auth(actor),
                    )
                    assert failed.status_code == 503, failed.text
                    assert len(calls) == 1
            async with maker() as observer:
                assert (
                    await observer.scalar(
                        text("SELECT count(*) FROM public.projects WHERE owner_id=:id"),
                        {"id": actor_id},
                    )
                    == 1
                )
                assert (
                    await observer.scalar(
                        text(
                            "SELECT count(*) FROM public.workspace_configuration_operation_receipts WHERE actor_id=:id"
                        ),
                        {"id": actor_id},
                    )
                    == 1
                )
            owner.evidence["workspace_fail_closed_verified"] = True
    finally:
        if actor_id is not None:
            await _cleanup_world(maker, actor_id, {})
        if registry_id is not None:
            async with maker.begin() as cleanup:
                await cleanup.execute(
                    text(
                        "DELETE FROM public.ml_backend_service_pools WHERE legacy_instance_id=:id"
                    ),
                    {"id": registry_id},
                )
                await cleanup.execute(
                    text("DELETE FROM public.ml_backend_registry WHERE id=:id"),
                    {"id": registry_id},
                )
