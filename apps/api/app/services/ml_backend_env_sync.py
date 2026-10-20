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

from aap_inference import extract_capabilities
import httpx
from sqlalchemy import select

from app.config import settings
from app.db.base import async_session
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.services.capability_instances import _observe_urls, _probe_setup
from app.services.inference_limits import capability_limits_from_settings

logger = logging.getLogger(__name__)


async def sync_env_backends() -> None:
    """在 FastAPI lifespan 启动时调用。失败不阻断启动 (仅告警)。"""
    urls = _observe_urls()
    timeout = httpx.Timeout(float(settings.ml_health_timeout))
    async with httpx.AsyncClient(timeout=timeout) as client:
        setups = await asyncio.gather(
            *[_probe_setup(client, u) for u in urls], return_exceptions=True
        )

    now = datetime.now(UTC)
    async with async_session() as db:
        for url, setup in zip(urls, setups):
            if isinstance(setup, BaseException):
                setup = None
            caps = (
                extract_capabilities(setup, limits=capability_limits_from_settings())
                if setup
                else None
            )
            existing = (
                await db.execute(
                    select(MLBackendRegistry).where(MLBackendRegistry.url == url)
                )
            ).scalar_one_or_none()

            if existing is not None and existing.source != "env":
                # manual 注册项优先, env 不覆盖
                continue

            health_meta = {"capabilities": caps} if caps else None
            state = "connected" if caps else "error"
            is_interactive = bool(caps["is_interactive"]) if caps else False
            name = (setup or {}).get("name") or url.rstrip("/").split("/")[-1]

            if existing is None:
                from app.services.ml_backend import MLBackendService

                # v0.23.3 ADR-0050 · 经 service.create_registry 自动建 singleton pool。
                await MLBackendService(db).create_registry(
                    name=name,
                    url=url,
                    source="env",
                    is_interactive=is_interactive,
                )
                # 探测态 (connected/error) 与 health_meta 写回刚建的行。
                fresh = (
                    await db.execute(
                        select(MLBackendRegistry).where(MLBackendRegistry.url == url)
                    )
                ).scalar_one()
                fresh.state = state
                fresh.health_meta = health_meta
                fresh.last_checked_at = now
            else:
                existing.name = name
                existing.state = state
                existing.is_interactive = is_interactive
                # A failed probe must invalidate the previous endpoint snapshot.
                existing.health_meta = health_meta
                existing.last_checked_at = now

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
