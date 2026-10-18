from __future__ import annotations

import pytest

from app.services.storage import StorageService


@pytest.mark.parametrize(
    "key",
    [
        "media-assets/5ad04098-1970-441f-9e8e-46585c6bc134/g1/a60c4f5b-cc21-4d10-90ed-b26f063199e8/poster.webp",
        "media-assets/5ad04098-1970-441f-9e8e-46585c6bc134/g2/a60c4f5b-cc21-4d10-90ed-b26f063199e8/playback.mp4",
        "media-assets/5ad04098-1970-441f-9e8e-46585c6bc134/g2/a60c4f5b-cc21-4d10-90ed-b26f063199e8/frame.webp",
        "media-assets/5ad04098-1970-441f-9e8e-46585c6bc134/g2/a60c4f5b-cc21-4d10-90ed-b26f063199e8/chunk.mp4",
    ],
)
def test_assets_media_keys_route_to_media_cache_bucket(key: str) -> None:
    storage = object.__new__(StorageService)
    storage.datasets_bucket = "datasets"
    storage.media_cache_bucket = "media-cache"

    assert storage.bucket_for_cache_key(key) == "media-cache"
    assert storage.bucket_for_cache_key("uploads/source.mp4") == "datasets"
