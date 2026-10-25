from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from aap_inference_contract.capabilities import CapabilityLimits
from .capabilities import extract_capabilities


@dataclass(frozen=True)
class BackendSetupLocator:
    id: UUID
    url: str
    auth_method: str | None
    auth_token: str | None
    extra_params: dict[str, Any] = field(default_factory=dict)


async def fetch_backend_setup(locator: BackendSetupLocator, *, timeout: float) -> Any:
    """Fetch the existing backend ``/setup`` operation without API dependencies."""
    import httpx

    headers = {"Content-Type": "application/json"}
    if locator.auth_method == "token" and locator.auth_token:
        headers["Authorization"] = f"Bearer {locator.auth_token}"
    headers["Cache-Control"] = "no-cache"
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(f"{locator.url.rstrip('/')}/setup", headers=headers)
        response.raise_for_status()
        return response.json()


def _validate_setup_client_configuration(locator: BackendSetupLocator) -> None:
    # Match MLBackendClient construction before its setup request (URL and concurrency coercion).
    locator.url.rstrip("/")
    if locator.id:
        str(locator.id)
    extra = locator.extra_params or {}
    int(extra.get("max_concurrency", 4))


async def probe_backend_capabilities(
    locator: BackendSetupLocator,
    *,
    timeout: float,
    limits: CapabilityLimits = CapabilityLimits(),
) -> dict | None:
    """Probe and normalize capabilities, failing open like health metadata refresh."""
    try:
        _validate_setup_client_configuration(locator)
        return extract_capabilities(
            await fetch_backend_setup(locator, timeout=timeout), limits=limits
        )
    except Exception:
        return None
