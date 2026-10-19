"""v0.19.0 ADR-0044 · 启动时把 env 配置的 ML backend upsert 成全局注册项。

settings.ml_backend_observe_urls 里的每个 URL 启动时:
- 探测 /setup 拿 name / 能力快照 / is_interactive;
- 按 url upsert 成 source='env' 的 ml_backend_registry 行 (探测成功置 connected, 失败
  置 error/disconnected 但保留行);
- 已存在的 source='manual' 行不覆盖 (运维手动注册优先)。

reconcile: env 里已删除的 URL 对应的 source='env' 行置 state='disconnected' (不删行,
保留历史 prediction 溯源)。manual 行不受影响。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import httpx
from aap_protocol_v2.backend_auth import managed_provider

from sqlalchemy import select

from app.config import settings

from app.db.base import async_session
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.services.capability_instances import _observe_urls, _probe_setup
from app.services.ml_capabilities import extract_capabilities
from app.services.ml_client import MLBackendClient

logger = logging.getLogger(__name__)


async def _probe_backend(client: MLBackendClient) -> dict | None:
    try:
        if client.auth_method == "none":
            async with httpx.AsyncClient(timeout=settings.ml_health_timeout) as public:
                return await _probe_setup(public, client.base_url)
        if managed_provider(client.auth_method) is not None:
            # Public health alone cannot attest an authenticated backend.
            healthy, _ = await client.health_meta()
            if not healthy:
                return None
        return await client.setup()
    except Exception:
        logger.warning("env backend discovery failed for a configured service")
        return None


async def sync_env_backends() -> None:
    """在 FastAPI lifespan 启动时调用。失败不阻断启动 (仅告警)。"""
    urls = _observe_urls()
    now = datetime.now(UTC)
    async with async_session() as db:
        probes = []
        for url in urls:
            existing = (
                await db.execute(
                    select(MLBackendRegistry).where(MLBackendRegistry.url == url)
                )
            ).scalar_one_or_none()

            if existing is not None and existing.source != "env":
                # manual 注册项优先, env 不覆盖
                continue

            if existing is None:
                from app.services.ml_backend import MLBackendService

                existing = await MLBackendService(db).create_registry(
                    name=url,
                    url=url,
                    source="env",
                )
                await db.commit()
            probes.append(
                (
                    existing.id,
                    (existing.url, existing.auth_revision),
                    MLBackendClient(existing),
                )
            )
        # End the discovery transaction before waiting for any network calls.
        # Only probes run concurrently; the AsyncSession stays in this task.
        await db.commit()
        setups = await asyncio.gather(
            *(_probe_backend(client) for _, _, client in probes)
        )
        for (registry_id, requested_identity, _), setup in zip(probes, setups):
            url = requested_identity[0]
            caps = extract_capabilities(setup) if setup else None
            health_meta = {"capabilities": caps} if caps else None
            state = "connected" if caps else "error"
            is_interactive = bool(caps["is_interactive"]) if caps else False
            name = (setup or {}).get("name") or url.rstrip("/").split("/")[-1]
            current = await db.scalar(
                select(MLBackendRegistry)
                .where(MLBackendRegistry.id == registry_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if (
                current is None
                or current.source != "env"
                or (current.url, current.auth_revision) != requested_identity
            ):
                await db.commit()
                continue
            current.name = name
            current.state = state
            current.is_interactive = is_interactive
            current.health_meta = health_meta
            current.last_checked_at = now
            # Release this row before probing the next service over the network.
            await db.commit()

        # reconcile: env 里已不存在的 source='env' 行置 disconnected (保留行)
        observed = set(urls)
        stale = (
            await db.execute(
                select(MLBackendRegistry).where(MLBackendRegistry.source == "env")
            )
        ).scalars()
        for row in stale:
            if row.url not in observed:
                row.state = "disconnected"

        await db.commit()
    logger.info("env ML backend sync done: %d observed url(s)", len(urls))
