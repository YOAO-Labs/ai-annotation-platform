"""Guide asset value constraints shared by workspace and transport adapters."""

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
