"""Read-only S3 URL signing for existing dataset and derived media objects."""

from __future__ import annotations

import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Protocol

from .config import CatalogConfig

_PRESIGN_ALIGN_WINDOW_SECONDS = 600
_MEDIA_CACHE_PREFIXES = (
    "thumbnails/",
    "videos/",
    "playback/",
    "image-pyramids/",
)
MAX_CALIBRATION_SOURCE_BYTES = 1_048_576


class PresignClient(Protocol):
    def generate_presigned_url(
        self,
        client_method: str,
        *,
        Params: dict[str, str],
        ExpiresIn: int,
    ) -> str: ...


class SourceReadClient(Protocol):
    def head_object(self, **kwargs) -> dict: ...

    def get_object(self, **kwargs) -> dict: ...


def create_source_read_client(
    *, endpoint_url: str, region_name: str, access_key: str, secret_key: str
) -> SourceReadClient:
    """Create the bounded, fixed-origin client used for source HEAD/Get reads."""
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region_name,
        config=Config(
            signature_version="s3v4",
            connect_timeout=2,
            read_timeout=10,
            retries={"max_attempts": 1, "mode": "standard"},
            s3={"addressing_style": "path"},
        ),
    )


@dataclass(frozen=True, slots=True)
class SourceObjectFacts:
    etag: str
    size: int


class SourceReadError(RuntimeError):
    """A fixed-dataset source object cannot be read under its captured identity."""


class SourceObjectChanged(SourceReadError):
    """The object no longer matches the captured ETag and size."""


class SourceObjectMissing(SourceReadError):
    """The fixed-dataset source object no longer exists."""


class SourceObjectTooLarge(SourceReadError):
    """A calibration source exceeds the small JSON read budget."""


class SourceObjectUnavailable(SourceReadError):
    """The fixed-dataset source store is unavailable or returned bad facts."""


def _aligned_expires_in(expires_in: int) -> int:
    now = int(time.time())
    deadline = ((now // _PRESIGN_ALIGN_WINDOW_SECONDS) + 1) * (
        _PRESIGN_ALIGN_WINDOW_SECONDS
    ) + expires_in
    return deadline - now


class SpatialCalibrationSourceReader:
    """Conditional, bounded reads from one fixed datasets bucket."""

    def __init__(self, client: SourceReadClient, datasets_bucket: str) -> None:
        self._client = client
        self.datasets_bucket = datasets_bucket

    def head_source(
        self,
        key: str,
        *,
        expected_etag: str | None = None,
        expected_size: int | None = None,
    ) -> SourceObjectFacts:
        from botocore.exceptions import ClientError

        self._validate_source_key(key)
        params = {"Bucket": self.datasets_bucket, "Key": key}
        if expected_etag is not None:
            params["IfMatch"] = _if_match_value(expected_etag)
        try:
            response = self._client.head_object(**params)
        except ClientError as exc:
            raise _source_error(exc) from None
        except Exception as exc:
            raise SourceObjectUnavailable("Assets source is unavailable") from exc

        facts = _source_facts(response)
        if expected_etag is not None and facts.etag != _normalize_etag(expected_etag):
            raise SourceObjectChanged("Assets source changed")
        if expected_size is not None and facts.size != expected_size:
            raise SourceObjectChanged("Assets source changed")
        return facts

    def get_calibration_source(
        self,
        key: str,
        *,
        expected_etag: str | None = None,
        expected_size: int | None = None,
    ) -> bytes:
        return self.read_calibration_source(
            key,
            expected_etag=expected_etag,
            expected_size=expected_size,
        )[1]

    def read_calibration_source(
        self,
        key: str,
        *,
        expected_etag: str | None = None,
        expected_size: int | None = None,
    ) -> tuple[SourceObjectFacts, bytes]:
        """Read a calibration JSON object with a HEAD/Get If-Match fence."""
        from botocore.exceptions import ClientError

        self._validate_source_key(key)
        if expected_size is not None and expected_size < 0:
            raise ValueError("expected_size must be non-negative")

        facts = self.head_source(
            key,
            expected_etag=expected_etag,
            expected_size=expected_size,
        )
        if facts.size > MAX_CALIBRATION_SOURCE_BYTES:
            raise SourceObjectTooLarge("Calibration source exceeds its byte limit")

        try:
            response = self._client.get_object(
                Bucket=self.datasets_bucket,
                Key=key,
                IfMatch=_if_match_value(facts.etag),
            )
        except ClientError as exc:
            raise _source_error(exc) from None
        except Exception as exc:
            raise SourceObjectUnavailable("Assets source is unavailable") from exc

        body = response.get("Body")
        if body is None or not hasattr(body, "read"):
            raise SourceObjectUnavailable("Assets source returned no body")
        payload = bytearray()
        try:
            get_facts = _source_facts(response)
            if get_facts != facts:
                raise SourceObjectChanged("Assets source changed")
            while len(payload) <= MAX_CALIBRATION_SOURCE_BYTES:
                chunk = body.read(
                    min(64 * 1024, MAX_CALIBRATION_SOURCE_BYTES + 1 - len(payload))
                )
                if not chunk:
                    break
                payload.extend(chunk)
        except SourceReadError:
            raise
        except Exception as exc:
            raise SourceObjectUnavailable("Assets source read failed") from exc
        finally:
            close = getattr(body, "close", None)
            if close is not None:
                with suppress(Exception):
                    close()

        if len(payload) > MAX_CALIBRATION_SOURCE_BYTES:
            raise SourceObjectTooLarge("Calibration source exceeds its byte limit")
        if len(payload) != facts.size:
            raise SourceObjectChanged("Assets source changed")
        return facts, bytes(payload)

    @staticmethod
    def _validate_source_key(key: str) -> None:
        if not isinstance(key, str) or not key or "\x00" in key:
            raise ValueError("A valid Assets source key is required")


class CatalogObjectSigner:
    """Sign only GET operations for fixed buckets and keys read from Assets."""

    def __init__(
        self,
        config: CatalogConfig,
        *,
        client: PresignClient | None = None,
        source_client: SourceReadClient | None = None,
    ) -> None:
        self.datasets_bucket = config.datasets_bucket
        self.media_cache_bucket = config.media_cache_bucket
        self._config = config
        self._source_client = source_client
        self._spatial_source_reader: SpatialCalibrationSourceReader | None = None
        # SigV4 binds Host into the canonical request. Sign against the external
        # origin directly instead of rewriting a URL signed for the internal host.
        if client is None:
            import boto3
            from botocore.config import Config

            self._client = boto3.client(
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
        else:
            self._client = client

    def sign_source(self, key: str) -> str:
        return self._sign(self.datasets_bucket, key)

    def sign_thumbnail(self, key: str) -> str:
        bucket = (
            self.media_cache_bucket
            if key.startswith(_MEDIA_CACHE_PREFIXES)
            else self.datasets_bucket
        )
        return self._sign(bucket, key)

    def head_source(
        self,
        key: str,
        *,
        expected_etag: str | None = None,
        expected_size: int | None = None,
    ) -> SourceObjectFacts:
        return self.spatial_source_reader.head_source(
            key,
            expected_etag=expected_etag,
            expected_size=expected_size,
        )

    def get_calibration_source(
        self,
        key: str,
        *,
        expected_etag: str | None = None,
        expected_size: int | None = None,
    ) -> bytes:
        return self.spatial_source_reader.get_calibration_source(
            key,
            expected_etag=expected_etag,
            expected_size=expected_size,
        )

    def read_calibration_source(
        self,
        key: str,
        *,
        expected_etag: str | None = None,
        expected_size: int | None = None,
    ) -> tuple[SourceObjectFacts, bytes]:
        return self.spatial_source_reader.read_calibration_source(
            key,
            expected_etag=expected_etag,
            expected_size=expected_size,
        )

    @property
    def spatial_source_reader(self) -> SpatialCalibrationSourceReader:
        if self._spatial_source_reader is None:
            self._spatial_source_reader = SpatialCalibrationSourceReader(
                self._read_client(), self.datasets_bucket
            )
        return self._spatial_source_reader

    def _read_client(self) -> SourceReadClient:
        if self._source_client is None:
            # Presigning has no network timeout, while reads must not hold an
            # accepted import open indefinitely. Always use a separate client
            # with the fixed datasets origin and bounded I/O/retry budgets.
            self._source_client = create_source_read_client(
                endpoint_url=self._config.s3_endpoint,
                access_key=self._config.s3_access_key,
                secret_key=self._config.s3_secret_key,
                region_name=self._config.s3_region,
            )
        return self._source_client

    def _sign(self, bucket: str, key: str) -> str:
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=_aligned_expires_in(3600),
        )


def _normalize_etag(value: str) -> str:
    return value.strip().strip('"')


def _if_match_value(value: str) -> str:
    normalized = _normalize_etag(value)
    if not normalized:
        raise ValueError("A source ETag is required")
    return f'"{normalized}"'


def _source_facts(response: object) -> SourceObjectFacts:
    if not isinstance(response, dict):
        raise SourceObjectUnavailable("Assets source returned invalid facts")
    etag = response.get("ETag")
    size = response.get("ContentLength")
    if not isinstance(etag, str) or not _normalize_etag(etag):
        raise SourceObjectUnavailable("Assets source returned invalid facts")
    if type(size) is not int or size < 0:
        raise SourceObjectUnavailable("Assets source returned invalid facts")
    return SourceObjectFacts(etag=_normalize_etag(etag), size=size)


def _source_error(exc: Exception) -> SourceReadError:
    response = getattr(exc, "response", {})
    error = response.get("Error", {}) if isinstance(response, dict) else {}
    code = error.get("Code") if isinstance(error, dict) else None
    status_code = (
        response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if isinstance(response, dict)
        else None
    )
    if code in {"PreconditionFailed", "412"} or status_code == 412:
        return SourceObjectChanged("Assets source changed")
    if code in {"NoSuchKey", "NotFound", "404"} or status_code == 404:
        return SourceObjectMissing("Assets source is missing")
    return SourceObjectUnavailable("Assets source is unavailable")
