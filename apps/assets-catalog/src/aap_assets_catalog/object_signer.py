"""Read-only S3 URL signing for existing dataset and derived media objects."""

from __future__ import annotations

import time
from typing import Protocol

import boto3
from botocore.config import Config

from .config import CatalogConfig

_PRESIGN_ALIGN_WINDOW_SECONDS = 600
_MEDIA_CACHE_PREFIXES = (
    "thumbnails/",
    "videos/",
    "playback/",
    "image-pyramids/",
)


class PresignClient(Protocol):
    def generate_presigned_url(
        self,
        client_method: str,
        *,
        Params: dict[str, str],
        ExpiresIn: int,
    ) -> str: ...


def _aligned_expires_in(expires_in: int) -> int:
    now = int(time.time())
    deadline = ((now // _PRESIGN_ALIGN_WINDOW_SECONDS) + 1) * (
        _PRESIGN_ALIGN_WINDOW_SECONDS
    ) + expires_in
    return deadline - now


class CatalogObjectSigner:
    """Sign only GET operations for fixed buckets and keys read from Assets."""

    def __init__(
        self,
        config: CatalogConfig,
        *,
        client: PresignClient | None = None,
    ) -> None:
        self.datasets_bucket = config.datasets_bucket
        self.media_cache_bucket = config.media_cache_bucket
        # SigV4 binds Host into the canonical request. Sign against the external
        # origin directly instead of rewriting a URL signed for the internal host.
        self._client = client or boto3.client(
            "s3",
            endpoint_url=config.s3_public_endpoint,
            aws_access_key_id=config.s3_access_key,
            aws_secret_access_key=config.s3_secret_key,
            region_name=config.s3_region,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
            ),
        )

    def sign_source(self, key: str) -> str:
        return self._sign(self.datasets_bucket, key)

    def sign_thumbnail(self, key: str) -> str:
        bucket = (
            self.media_cache_bucket
            if key.startswith(_MEDIA_CACHE_PREFIXES)
            else self.datasets_bucket
        )
        return self._sign(bucket, key)

    def _sign(self, bucket: str, key: str) -> str:
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=_aligned_expires_in(3600),
        )
