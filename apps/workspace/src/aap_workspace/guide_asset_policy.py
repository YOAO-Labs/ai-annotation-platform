"""Portable policy for guide-image asset keys and project metadata."""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any
from uuid import UUID, uuid4

from .configuration_policy import ConfigurationPolicyError

ALLOWED_GUIDE_ASSET_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/gif",
        "image/svg+xml",
    }
)
MAX_GUIDE_ASSET_SIZE_BYTES = 5 * 1024 * 1024

_GUIDE_KEY_RE = re.compile(r"^projects/[0-9a-f-]{36}/guide/[0-9a-f-]{36}-[^/]+$")


def sanitize_guide_asset_filename(name: str) -> str:
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch.isprintable() and ch not in "\r\n\t")
    return name[:200] or "image"


def assert_guide_asset_key(key: str, project_id: UUID) -> None:
    if not _GUIDE_KEY_RE.match(key) or not key.startswith(
        f"projects/{project_id}/guide/"
    ):
        raise ConfigurationPolicyError(404, "Guide asset not found")


def assert_guide_asset_upload_target(
    project_id: UUID, key: str, content_type: str
) -> None:
    assert_guide_asset_key(key, project_id)
    if content_type not in ALLOWED_GUIDE_ASSET_TYPES:
        raise ConfigurationPolicyError(400, "Unsupported content type")


def new_guide_asset_key(
    project_id: UUID,
    *,
    filename: str,
    content_type: str,
    size: int,
    asset_id: UUID | None = None,
) -> str:
    if content_type not in ALLOWED_GUIDE_ASSET_TYPES:
        raise ConfigurationPolicyError(400, f"Unsupported content type: {content_type}")
    if size > MAX_GUIDE_ASSET_SIZE_BYTES:
        raise ConfigurationPolicyError(400, "File too large")

    safe_name = sanitize_guide_asset_filename(filename)
    selected_id = uuid4() if asset_id is None else asset_id
    return f"projects/{project_id}/guide/{selected_id}-{safe_name}"


def record_guide_asset_upload(
    project_id: UUID,
    guide_assets: list[Any] | None,
    *,
    key: str,
    original_name: str,
    content_type: str,
    size: int,
    uploaded_at: datetime | None = None,
) -> tuple[dict[str, str | int], list[Any]]:
    assert_guide_asset_upload_target(project_id, key, content_type)

    if size <= 0 or size > MAX_GUIDE_ASSET_SIZE_BYTES:
        raise ConfigurationPolicyError(400, "Invalid upload size")

    entry = {
        "key": key,
        "original_name": sanitize_guide_asset_filename(original_name),
        "content_type": content_type,
        "size": size,
        "uploaded_at": (
            datetime.now(timezone.utc) if uploaded_at is None else uploaded_at
        ).isoformat(),
    }
    assets = list(guide_assets or [])
    assets.append(entry)
    return entry, assets


def remove_guide_asset(
    project_id: UUID, guide_assets: list[Any] | None, key: str
) -> tuple[dict[str, str], list[Any]]:
    assert_guide_asset_key(key, project_id)
    existing = list(guide_assets or [])
    remaining = [entry for entry in existing if entry.get("key") != key]
    if len(remaining) == len(existing):
        raise ConfigurationPolicyError(404, "Asset not found")
    return {"deleted": key}, remaining


def assert_registered_guide_asset_key(
    project_id: UUID, guide_assets: list[Any] | None, key: str
) -> None:
    assert_guide_asset_key(key, project_id)
    keys = {entry.get("key") for entry in (guide_assets or [])}
    if key not in keys:
        raise ConfigurationPolicyError(404, "Asset not found")
