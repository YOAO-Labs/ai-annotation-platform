"""Bind Mask byte operations to their owner without moving Labeling authority."""

from contextlib import contextmanager
from urllib.parse import urlsplit

import httpx

from aap_assets_mask_content.client import MaskContentClient
from aap_assets_mask_content.contract import MaskContentFailure
from aap_assets_mask_content.storage import S3MaskContentStore

from app.config import settings


@contextmanager
def mask_content_owner(storage, *, cleanup: bool = False):
    remote = bool(settings.assets_mask_content_url or settings.assets_mask_cleanup_url)
    origin = (
        settings.assets_mask_cleanup_url
        if cleanup
        else settings.assets_mask_content_url
    )
    token = (
        settings.assets_mask_cleanup_token
        if cleanup
        else settings.assets_mask_content_token
    )
    if not remote:
        yield S3MaskContentStore(storage.client, storage.bucket)
        return
    try:
        parts = urlsplit(origin)
    except (TypeError, ValueError):
        raise MaskContentFailure("storage_unavailable") from None
    if (
        not token
        or parts.scheme not in {"http", "https"}
        or not parts.netloc
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise MaskContentFailure("storage_unavailable")
    # The selected owner is authoritative even when unavailable or misconfigured.
    with httpx.Client(
        timeout=settings.assets_mask_content_timeout_seconds,
        trust_env=False,
    ) as transport:
        with MaskContentClient(
            origin,
            token,
            client=transport,
            timeout_seconds=settings.assets_mask_content_timeout_seconds,
        ) as owner:
            yield owner
