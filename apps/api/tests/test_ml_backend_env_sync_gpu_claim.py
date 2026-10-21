"""Env backend reconciliation must never guess or overwrite GPU claims."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.ml_backend_registry import MLBackendRegistry
from app.services import ml_backend_env_sync


async def test_env_sync_uses_safe_defaults_and_preserves_existing_claims(
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    existing_env = MLBackendRegistry(
        id=uuid.uuid4(),
        name="existing-env",
        url="http://existing-env:8000",
        source="env",
        state="connected",
        gpu_resource_id="node-a/GPU-env",
        vram_budget_mb=8192,
        eviction_priority=3,
    )
    existing_manual = MLBackendRegistry(
        id=uuid.uuid4(),
        name="existing-manual",
        url="http://existing-manual:8000",
        source="manual",
        state="connected",
        gpu_resource_id="node-a/GPU-manual",
        vram_budget_mb=12288,
        eviction_priority=9,
    )
    stale_env = MLBackendRegistry(
        id=uuid.uuid4(),
        name="stale-env",
        url="http://stale-env:8000",
        source="env",
        state="connected",
        gpu_resource_id="node-b/GPU-stale",
        vram_budget_mb=4096,
        eviction_priority=-1,
    )
    failed_env = MLBackendRegistry(
        id=uuid.uuid4(),
        name="failed-env",
        url="http://failed-env:8000",
        source="env",
        state="connected",
        health_meta={"compute": {"configured_device": "cpu"}},
        gpu_resource_id="node-b/GPU-failed",
        vram_budget_mb=6144,
        eviction_priority=2,
    )
    db_session.add_all((existing_env, existing_manual, stale_env, failed_env))
    await db_session.flush()

    urls = [
        "http://new-env:8000",
        existing_env.url,
        existing_manual.url,
        failed_env.url,
    ]

    async def _probe_setup(_client, url: str):
        if url == failed_env.url:
            return None
        return {
            "name": f"observed-{url}",
            "is_interactive": True,
            "supported_prompts": ["bbox"],
        }

    @asynccontextmanager
    async def _session():
        yield db_session

    monkeypatch.setattr(ml_backend_env_sync, "_observe_urls", lambda: urls)
    monkeypatch.setattr(ml_backend_env_sync, "_probe_setup", _probe_setup)
    monkeypatch.setattr(ml_backend_env_sync, "async_session", _session)

    await ml_backend_env_sync.sync_env_backends()

    rows = {
        row.url: row
        for row in (await db_session.execute(select(MLBackendRegistry))).scalars()
    }
    created = rows["http://new-env:8000"]
    assert created.gpu_resource_id is None
    assert created.vram_budget_mb is None
    assert created.eviction_priority == 0

    refreshed_env = rows[existing_env.url]
    assert refreshed_env.gpu_resource_id == "node-a/GPU-env"
    assert refreshed_env.vram_budget_mb == 8192
    assert refreshed_env.eviction_priority == 3

    untouched_manual = rows[existing_manual.url]
    assert untouched_manual.name == "existing-manual"
    assert untouched_manual.gpu_resource_id == "node-a/GPU-manual"
    assert untouched_manual.vram_budget_mb == 12288
    assert untouched_manual.eviction_priority == 9

    refreshed_stale = rows[stale_env.url]
    assert refreshed_stale.state == "disconnected"
    assert refreshed_stale.gpu_resource_id == "node-b/GPU-stale"
    assert refreshed_stale.vram_budget_mb == 4096
    assert refreshed_stale.eviction_priority == -1

    refreshed_failed = rows[failed_env.url]
    assert refreshed_failed.state == "error"
    assert refreshed_failed.health_meta is None
    assert refreshed_failed.gpu_resource_id == "node-b/GPU-failed"
    assert refreshed_failed.vram_budget_mb == 6144
    assert refreshed_failed.eviction_priority == 2
