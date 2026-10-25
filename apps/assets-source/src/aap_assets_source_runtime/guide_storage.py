"""Fixed-bucket S3 operations for project guide objects."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from urllib.parse import urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from aap_assets_source.guide_object_contract import GuideObjectFailure

from .guide_config import GuideStorageConfig

logger = logging.getLogger("aap_assets_source_runtime.guide_storage")
_MISSING_CODES = {"404", "NoSuchKey", "NotFound"}
_PRESIGN_ALIGN_WINDOW = 600


def _aligned_expires_in(expires_in: int) -> int:
    now = int(time.time())
    deadline = ((now // _PRESIGN_ALIGN_WINDOW) + 1) * _PRESIGN_ALIGN_WINDOW + expires_in
    return deadline - now


def _client_config(config: GuideStorageConfig, *, signature_version: str) -> Config:
    return Config(
        signature_version=signature_version,
        s3={"addressing_style": "path"},
        connect_timeout=config.connect_timeout_seconds,
        read_timeout=config.read_timeout_seconds,
        retries={"mode": "standard", "total_max_attempts": 1},
        proxies={},
    )


class GuideStorage:
    """Perform the narrow object operations needed by project guide assets.

    All storage calls run in worker threads so synchronous boto3 I/O does not
    block the async request loop. Cancelling the awaiting coroutine cannot stop
    a boto3 call that is already running in its thread.
    """

    def __init__(
        self,
        config: GuideStorageConfig,
        internal_client: Any | None = None,
        signer: Any | None = None,
    ) -> None:
        self.config = config
        self._owns_internal_client = internal_client is None
        self._owns_signer = signer is None
        self._closed = False
        self._client_config_sigv4 = _client_config(config, signature_version="s3v4")
        self._client_config_sigv2 = _client_config(config, signature_version="s3")

        self._client = internal_client
        if self._client is None:
            self._client = boto3.client(
                "s3",
                endpoint_url=config.endpoint,
                aws_access_key_id=config.access_key,
                aws_secret_access_key=config.secret_key,
                region_name=config.region,
                config=self._client_config_sigv4,
            )

        self._signer = signer
        if self._signer is None:
            try:
                self._signer = boto3.client(
                    "s3",
                    endpoint_url=config.endpoint,
                    aws_access_key_id=config.access_key,
                    aws_secret_access_key=config.secret_key,
                    region_name=config.region,
                    config=self._client_config_sigv2,
                )
            except BaseException:
                self._close_owned()
                raise

    async def generate_upload_url(self, key: str, *, content_type: str) -> str:
        return await asyncio.to_thread(self._generate_upload_url, key, content_type)

    def _generate_upload_url(self, key: str, content_type: str) -> str:
        try:
            url = self._signer.generate_presigned_url(
                "put_object",
                Params={
                    "Bucket": self.config.bucket,
                    "Key": key,
                    "ContentType": content_type,
                },
                ExpiresIn=900,
            )
        except (BotoCoreError, ClientError) as exc:
            logger.warning("Guide upload signing failed (%s)", type(exc).__name__)
            raise GuideObjectFailure(code="storage_error") from None
        return self._public_url(url)

    async def verify_upload(self, key: str) -> int | None:
        return await asyncio.to_thread(self._verify_upload, key)

    def _verify_upload(self, key: str) -> int | None:
        try:
            result = self._client.head_object(Bucket=self.config.bucket, Key=key)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in _MISSING_CODES:
                return None
            logger.warning("Guide object verification failed (%s)", type(exc).__name__)
            raise GuideObjectFailure(code="storage_error") from None
        except BotoCoreError as exc:
            logger.warning("Guide object verification failed (%s)", type(exc).__name__)
            raise GuideObjectFailure(code="storage_error") from None
        return int(result.get("ContentLength") or 0)

    async def delete_object(self, key: str) -> None:
        await asyncio.to_thread(self._delete_object, key)

    def _delete_object(self, key: str) -> None:
        try:
            self._client.delete_object(Bucket=self.config.bucket, Key=key)
        except (BotoCoreError, ClientError) as exc:
            logger.warning("Guide object deletion failed (%s)", type(exc).__name__)
            raise GuideObjectFailure(code="storage_error") from None

    async def generate_download_url(self, key: str) -> str:
        return await asyncio.to_thread(self._generate_download_url, key)

    def _generate_download_url(self, key: str) -> str:
        try:
            url = self._signer.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.config.bucket, "Key": key},
                ExpiresIn=_aligned_expires_in(3600),
            )
        except (BotoCoreError, ClientError) as exc:
            logger.warning("Guide download signing failed (%s)", type(exc).__name__)
            raise GuideObjectFailure(code="storage_error") from None
        return self._public_url(url)

    def _public_url(self, url: str) -> str:
        try:
            parsed = urlsplit(url)
            internal = urlsplit(self.config.endpoint)
        except ValueError:
            raise GuideObjectFailure(code="storage_error") from None
        if (
            parsed.scheme.lower() != internal.scheme.lower()
            or parsed.netloc.lower() != internal.netloc.lower()
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or any(ord(char) < 32 or ord(char) == 127 for char in url)
        ):
            raise GuideObjectFailure(code="storage_error")

        origin = f"{parsed.scheme}://{parsed.netloc}"
        public_base = self.config.public_url.rstrip("/")
        return public_base + url[len(origin) :]

    async def close(self) -> None:
        await asyncio.to_thread(self._close_owned)

    def _close_owned(self) -> None:
        if self._closed:
            return
        self._closed = True
        failures = []
        for client, owned in (
            (self._client, self._owns_internal_client),
            (self._signer, self._owns_signer),
        ):
            if owned and client is not None:
                try:
                    client.close()
                except Exception as exc:  # noqa: BLE001 - close both owned clients
                    failures.append(exc)
        if failures:
            raise failures[0]
