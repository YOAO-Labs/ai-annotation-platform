"""Submit startup Registry reconciliation to its independent Inference owner."""

from __future__ import annotations

import logging

from app.config import settings
from app.services.inference_registry_ingress import sync_selected_environment

logger = logging.getLogger(__name__)


def _observe_urls() -> list[str]:
    """Normalize the configured startup URLs while preserving their order."""
    urls = list(settings.ml_backend_observe_urls or [])
    if not urls and settings.ml_backend_default_url:
        urls = [settings.ml_backend_default_url]
    seen: set[str] = set()
    out: list[str] = []
    for url in urls:
        key = url.rstrip("/")
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


async def sync_env_backends() -> None:
    urls = _observe_urls()
    observed_count = await sync_selected_environment(urls)
    logger.info("env ML backend sync done: %d observed url(s)", observed_count)
