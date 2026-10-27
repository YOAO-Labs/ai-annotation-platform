"""Narrow S3 operations required by Assets source controls."""

from __future__ import annotations

import asyncio
from io import BytesIO
import logging
from typing import Any
from urllib.parse import urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from .config import SourceConfig
from aap_assets_source.contract import SourceFailure
from aap_assets_source.connectors.crypto import (
    ConnectorCryptoError,
    ConnectorCryptoNotConfigured,
    decrypt_secret,
)
from aap_assets_source.managed_original_contract import (
    ManagedOriginalMaterial,
)
from .ingest_storage_errors import ManagedStorageError
from .managed_storage import ManagedStorage, ManagedWrite

logger = logging.getLogger("aap_assets_source_runtime.storage")

_MISSING_CODES = {"404", "NoSuchKey", "NotFound"}
_PRECONDITION_CODES = {"412", "PreconditionFailed"}


class SourceStorage:
    """Use fixed buckets and keys supplied by validated native capabilities."""

    def __init__(
        self,
        config: SourceConfig,
        *,
        internal_client: Any | None = None,
        public_signer: Any | None = None,
    ) -> None:
        self.import_bucket = config.import_bucket
        self.datasets_bucket = config.datasets_bucket
        self._image_header_bytes = config.image_header_bytes
        self._upload_url_expires_seconds = config.upload_url_expires_seconds
        client_config = Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            connect_timeout=config.s3_connect_timeout_seconds,
            read_timeout=config.s3_read_timeout_seconds,
            retries={"max_attempts": 2, "mode": "standard"},
            proxies={},
        )
        self._client = internal_client or boto3.client(
            "s3",
            endpoint_url=config.s3_endpoint,
            aws_access_key_id=config.s3_access_key,
            aws_secret_access_key=config.s3_secret_key,
            region_name=config.s3_region,
            config=client_config,
        )
        # A presigned URL binds the Host header in SigV4. Sign directly against
        # the public origin instead of rewriting an internally signed URL.
        self._public_signer = public_signer or boto3.client(
            "s3",
            endpoint_url=config.s3_public_endpoint,
            aws_access_key_id=config.s3_access_key,
            aws_secret_access_key=config.s3_secret_key,
            region_name=config.s3_region,
            config=client_config,
        )
        self._config = config
        self.managed = ManagedStorage(
            self._client,
            timeout_seconds=config.s3_read_timeout_seconds,
            client_for_target=self._connection_client,
        )

    def _connection_client(self, target: ManagedOriginalMaterial) -> Any:
        """Build a single-attempt client for exactly one connection-backed write."""
        destination = target.destination
        if not isinstance(destination, dict):
            raise ManagedStorageError("storage_write_failed")
        config = destination.get("config")
        secret_enc = destination.get("secret_enc")
        if not isinstance(config, dict) or not isinstance(secret_enc, str):
            raise ManagedStorageError("storage_write_failed")
        try:
            secret = decrypt_secret(secret_enc, self._config.connector_encryption_key)
            access_key = secret.get("access_key")
            secret_key = secret.get("secret_key")
            if (
                not isinstance(access_key, str)
                or not access_key
                or not isinstance(secret_key, str)
                or not secret_key
            ):
                raise ManagedStorageError("storage_write_failed")
        except (ConnectorCryptoError, ConnectorCryptoNotConfigured):
            raise ManagedStorageError("storage_write_failed") from None
        client = boto3.client(
            "s3",
            endpoint_url=target.endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=str(config.get("region") or self._config.s3_region),
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                connect_timeout=self._config.s3_connect_timeout_seconds,
                read_timeout=self._config.s3_read_timeout_seconds,
                retries={"total_max_attempts": 1, "mode": "standard"},
                proxies={},
            ),
        )
        return client

    async def publish_managed_upload(
        self,
        target: ManagedOriginalMaterial,
        *,
        staging_key: str,
        staging_etag: str | None,
        staging_version_id: str | None,
        max_bytes: int,
        image: bool,
        content_type: str,
        on_prepared=None,
        on_writing=None,
        on_written=None,
    ) -> ManagedWrite:
        """Copy one staging object into the owner-reserved immutable key."""
        try:
            return await self.managed.publish_object(
                target,
                source_client=self._client,
                source_bucket=self.import_bucket,
                source_key=staging_key,
                source_etag=staging_etag,
                source_version_id=staging_version_id,
                content_type=content_type,
                max_bytes=max_bytes,
                image=image,
                on_prepared=on_prepared,
                on_writing=on_writing,
                on_written=on_written,
            )
        except ManagedStorageError as exc:
            raise SourceFailure(exc.code) from None

    @property
    def client(self) -> Any:
        """Expose the explicitly configured client to the shared ingest helper."""
        return self._client

    async def generate_upload_url(self, key: str, content_type: str) -> str:
        return await asyncio.to_thread(self._generate_upload_url, key, content_type)

    def _generate_upload_url(self, key: str, content_type: str) -> str:
        try:
            url = self._public_signer.generate_presigned_url(
                "put_object",
                Params={
                    "Bucket": self.import_bucket,
                    "Key": key,
                    "ContentType": content_type,
                },
                ExpiresIn=self._upload_url_expires_seconds,
            )
        except (BotoCoreError, ClientError) as exc:
            logger.warning("Upload URL signing failed (%s)", type(exc).__name__)
            raise SourceFailure("storage_unavailable") from None
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or any(ord(char) < 32 or ord(char) == 127 for char in url)
        ):
            raise SourceFailure("invalid_response")
        return url

    async def verify_staging_upload(self, key: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._verify, self.import_bucket, key)

    async def verify_legacy_source(self, key: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._verify, self.datasets_bucket, key)

    def _verify(self, bucket: str, key: str) -> dict[str, Any] | None:
        try:
            return self._client.head_object(Bucket=bucket, Key=key)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code in _MISSING_CODES and status == 404:
                return None
            logger.warning("Object head failed (%s)", type(exc).__name__)
            raise SourceFailure("storage_unavailable") from None
        except BotoCoreError as exc:
            logger.warning("Object head failed (%s)", type(exc).__name__)
            raise SourceFailure("storage_unavailable") from None

    async def read_staging_image_dimensions(
        self, key: str, *, if_match: str
    ) -> tuple[int, int] | None:
        return await asyncio.to_thread(
            self._read_image_dimensions, self.import_bucket, key, if_match
        )

    async def read_legacy_image_dimensions(
        self, key: str, *, if_match: str
    ) -> tuple[int, int] | None:
        return await asyncio.to_thread(
            self._read_image_dimensions, self.datasets_bucket, key, if_match
        )

    def _read_image_dimensions(
        self, bucket: str, key: str, if_match: str
    ) -> tuple[int, int] | None:
        try:
            from PIL import Image
        except ImportError:
            logger.warning("Pillow is unavailable; image dimensions were skipped")
            return None

        try:
            response = self._client.get_object(
                Bucket=bucket,
                Key=key,
                Range=f"bytes=0-{self._image_header_bytes - 1}",
                IfMatch=if_match,
            )
            body = response["Body"]
            try:
                data = body.read(self._image_header_bytes)
            finally:
                body.close()
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in _PRECONDITION_CODES or code in _MISSING_CODES:
                raise SourceFailure("source_changed") from None
            logger.warning("Image header read failed (%s)", type(exc).__name__)
            raise SourceFailure("storage_unavailable") from None
        except BotoCoreError as exc:
            logger.warning("Image header read failed (%s)", type(exc).__name__)
            raise SourceFailure("storage_unavailable") from None

        try:
            with Image.open(BytesIO(data)) as image:
                width, height = int(image.width), int(image.height)
                try:
                    orientation = int(image.getexif().get(274, 1))
                except Exception:  # noqa: BLE001 - malformed EXIF means orientation 1
                    orientation = 1
                return (
                    (height, width) if orientation in {5, 6, 7, 8} else (width, height)
                )
        except Exception as exc:  # noqa: BLE001 - unsupported/corrupt images remain dimensionless
            logger.info("Image header could not be decoded (%s)", type(exc).__name__)
            return None

    async def publish_managed_adoption(
        self,
        target: ManagedOriginalMaterial,
        *,
        max_bytes: int,
        image: bool,
    ) -> ManagedWrite:
        """Fully read an existing object and pin its exact saved version."""
        try:
            return await self.managed.adopt(target, max_bytes=max_bytes, image=image)
        except ManagedStorageError as exc:
            raise SourceFailure(exc.code) from None

    async def verify_managed_original(
        self,
        target: ManagedOriginalMaterial,
        *,
        sha256: str,
        size_bytes: int,
        image: bool = False,
    ):
        """Read back a verified creation without acquiring new bytes."""
        try:
            return await self.managed.verify(
                target, sha256=sha256, size_bytes=size_bytes, image=image
            )
        except ManagedStorageError as exc:
            raise SourceFailure(exc.code) from None
