"""Portable guide-image content policy shared with the API adapter."""

from __future__ import annotations

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
