"""Borrow the API transaction for startup environment Registry reconciliation."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
import logging

import httpx

from aap_inference.registry_ingress import sync_environment
from app.config import settings
from app.db.base import async_session
from app.services.capability_instances import _observe_urls, _probe_setup
from app.services.inference_limits import capability_limits_from_settings
from app.services.inference_registry_ingress import sync_selected_environment
from app.services.inference_registry_ingress_data import InferenceRegistryIngressData

logger = logging.getLogger(__name__)


def _http_client(timeout_seconds: float):
    return httpx.AsyncClient(timeout=httpx.Timeout(float(timeout_seconds)))


@asynccontextmanager
async def _borrowed_registry_data():
    async with async_session() as db:
        yield InferenceRegistryIngressData(db)
        await db.commit()


async def _sync_borrowed(urls: list[str]) -> int:
    """Run the default owner in one API transaction after the HTTP gather."""
    return await sync_environment(
        urls,
        timeout_seconds=settings.ml_health_timeout,
        data_factory=_borrowed_registry_data,
        capability_limits_factory=capability_limits_from_settings,
        client_factory=_http_client,
        setup_probe=_probe_setup,
        clock=lambda: datetime.now(UTC),
    )


async def sync_env_backends() -> None:
    """Reconcile configured env URLs at API startup without blocking app import."""
    urls = _observe_urls()
    observed_count = await sync_selected_environment(urls, borrowed=_sync_borrowed)
    logger.info("env ML backend sync done: %d observed url(s)", observed_count)
