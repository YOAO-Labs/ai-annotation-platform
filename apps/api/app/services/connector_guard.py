"""Database adapter for the portable connector host guard."""

from __future__ import annotations

from typing import Literal

from aap_assets_source.connectors.host_guard import (
    ConnectorAllowlistInvalid,
    ConnectorHostDenied,
    assert_host_allowed,
    extract_host,
    normalize_allowlist,
    resolve_allowed_ips,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.system_setting import SystemSetting
from app.services.system_settings_service import SystemSettingsService


async def get_allowlist(db: AsyncSession) -> list[str]:
    """Read and canonicalize the current DB/environment allowlist."""
    raw = await SystemSettingsService.get(db, "connector_host_allowlist")
    if not isinstance(raw, list):
        return []
    return normalize_allowlist([str(entry) for entry in raw])


async def get_allowlist_state(
    db: AsyncSession,
) -> tuple[list[str], Literal["database", "environment"]]:
    """Return the active allowlist and whether DB explicitly overrides env."""
    entries = await get_allowlist(db)
    row = await db.scalar(
        select(SystemSetting).where(SystemSetting.key == "connector_host_allowlist")
    )
    source: Literal["database", "environment"] = (
        "database" if row is not None and row.value_json is not None else "environment"
    )
    return entries, source


async def assert_connection_target_allowed(
    db: AsyncSession, host_or_endpoint: str
) -> None:
    """Check a connector target against the current effective allowlist."""
    assert_host_allowed(host_or_endpoint, await get_allowlist(db))


__all__ = [
    "ConnectorAllowlistInvalid",
    "ConnectorHostDenied",
    "assert_connection_target_allowed",
    "assert_host_allowed",
    "extract_host",
    "get_allowlist",
    "get_allowlist_state",
    "normalize_allowlist",
    "resolve_allowed_ips",
]
