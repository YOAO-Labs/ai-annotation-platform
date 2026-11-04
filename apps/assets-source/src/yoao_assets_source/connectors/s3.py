from __future__ import annotations

from datetime import datetime
from typing import Any, BinaryIO, Iterable, Iterator
from urllib.parse import urlsplit

from .base import (
    SourceAdapter,
    SourceObject,
    SourceNotFoundError,
    SourcePathError,
    matches_include_globs,
    normalize_relpath,
)


def _under_prefix(prefix: str, key: str) -> bool:
    return not prefix or key == prefix or key.startswith(f"{prefix}/")


def _join_under_base(base_prefix: str, path: str | None) -> str:
    base = normalize_relpath(base_prefix)
    requested = normalize_relpath(path)
    if not requested:
        return base
    if base and _under_prefix(base, requested):
        return requested
    return f"{base}/{requested}" if base else requested


def validate_s3_source_path(config: dict, path: str | None) -> None:
    _join_under_base(str((config or {}).get("base_prefix") or ""), path)


def validate_s3_endpoint(config: dict) -> str:
    """Return a normalized endpoint URL without userinfo or unsupported schemes."""
    if not isinstance(config, dict) or not isinstance(config.get("endpoint"), str):
        raise ValueError("S3 endpoint must be a non-empty string")
    scheme = "https" if config.get("use_ssl") else "http"
    endpoint = config["endpoint"].strip()
    if "://" not in endpoint:
        endpoint = f"{scheme}://{endpoint}"
    try:
        parsed_endpoint = urlsplit(endpoint)
        parsed_endpoint.port
        valid_endpoint = (
            parsed_endpoint.scheme in {"http", "https"}
            and bool(parsed_endpoint.hostname)
            and parsed_endpoint.username is None
            and parsed_endpoint.password is None
            and not any(ord(char) < 32 or ord(char) == 127 for char in endpoint)
        )
    except ValueError:
        valid_endpoint = False
    if not valid_endpoint:
        raise ValueError("S3 endpoint must be an HTTP(S) URL without userinfo")
    return endpoint


class S3CompatibleSource(SourceAdapter):
    def __init__(
        self,
        config: dict,
        secret: dict,
        *,
        client: Any | None = None,
        require_conditional_reads: bool = False,
    ) -> None:
        if not isinstance(config, dict) or not isinstance(secret, dict):
            raise ValueError("invalid S3 connector configuration")
        if any(
            not isinstance(secret.get(key), str) or not secret.get(key)
            for key in ("access_key", "secret_key")
        ):
            raise ValueError("S3 access_key and secret_key are required")
        endpoint = validate_s3_endpoint(config)

        bucket = config.get("bucket")
        if not isinstance(bucket, str) or not bucket:
            raise ValueError("S3 bucket must be a non-empty string")
        self.bucket = bucket
        self.base_prefix = normalize_relpath(str(config.get("base_prefix") or ""))
        self._require_conditional_reads = require_conditional_reads
        if client is None:
            import boto3
            from botocore.config import Config as BotoConfig

            client = boto3.client(
                "s3",
                endpoint_url=endpoint,
                aws_access_key_id=secret.get("access_key"),
                aws_secret_access_key=secret.get("secret_key"),
                region_name=config.get("region"),
                config=BotoConfig(
                    connect_timeout=10,
                    read_timeout=60,
                    retries={"max_attempts": 2},
                    proxies={},
                ),
            )
        self.client = client

    def _rel_from_key(self, key: str) -> str:
        key = normalize_relpath(key)
        if self.base_prefix and key.startswith(f"{self.base_prefix}/"):
            return key[len(self.base_prefix) + 1 :]
        if self.base_prefix and key == self.base_prefix:
            return ""
        return key

    def _key_for_relpath(self, relpath: str) -> str:
        rel = normalize_relpath(relpath)
        if not rel:
            raise SourcePathError("relpath 不能为空")
        return f"{self.base_prefix}/{rel}" if self.base_prefix else rel

    def list(
        self,
        path: str,
        recursive: bool,
        include_globs: Iterable[str] | None = None,
    ) -> Iterator[SourceObject]:
        prefix = _join_under_base(self.base_prefix, path)
        continuation_token: str | None = None
        while True:
            kwargs: dict = {"Bucket": self.bucket, "Prefix": prefix}
            if continuation_token:
                kwargs["ContinuationToken"] = continuation_token
            try:
                resp = self.client.list_objects_v2(**kwargs)
            except Exception as exc:
                if _is_not_found(exc):
                    raise SourceNotFoundError(
                        "S3 source prefix no longer exists"
                    ) from exc
                raise
            for obj in resp.get("Contents", []):
                key = str(obj.get("Key") or "")
                if not key or key.endswith("/"):
                    continue
                normalized_key = normalize_relpath(key)
                if prefix and not _under_prefix(prefix, normalized_key):
                    continue
                if not recursive:
                    below_source = (
                        normalized_key[len(prefix) :].lstrip("/")
                        if prefix
                        else normalized_key
                    )
                    if "/" in below_source:
                        continue

                relpath = self._rel_from_key(normalized_key)
                if not relpath or not matches_include_globs(relpath, include_globs):
                    continue
                mtime = obj.get("LastModified")
                yield SourceObject(
                    relpath=relpath,
                    size=int(obj.get("Size") or 0),
                    mtime=mtime if isinstance(mtime, datetime) else None,
                    etag=(str(obj.get("ETag") or "").strip('"') or None),
                )
            if not resp.get("IsTruncated"):
                break
            next_token = resp.get("NextContinuationToken")
            if not isinstance(next_token, str) or not next_token:
                raise ValueError("S3 listing omitted its continuation token")
            if next_token == continuation_token:
                raise ValueError("S3 listing repeated its continuation token")
            continuation_token = next_token

    def open(self, relpath: str, *, etag: str | None = None) -> BinaryIO:
        if not etag and self._require_conditional_reads:
            from .base import SourceChangedError

            raise SourceChangedError("S3 source object has no listing ETag")
        key = self._key_for_relpath(relpath)
        kwargs = {"Bucket": self.bucket, "Key": key}
        if etag:
            value = etag.strip('"')
            kwargs["IfMatch"] = f'"{value}"'
        try:
            resp = self.client.get_object(**kwargs)
        except Exception as exc:
            if _is_precondition_failed(exc):
                from .base import SourceChangedError

                raise SourceChangedError("S3 source changed after enumeration") from exc
            if _is_not_found(exc):
                raise SourceNotFoundError("S3 source object no longer exists") from exc
            raise
        return resp["Body"]

    def close(self) -> None:
        return None


def _error_code(exc: Exception) -> str | None:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return None
    error = response.get("Error")
    if not isinstance(error, dict):
        return None
    return str(error.get("Code") or "") or None


def _is_not_found(exc: Exception) -> bool:
    return _error_code(exc) in {"404", "NoSuchKey", "NoSuchBucket", "NotFound"}


def _is_precondition_failed(exc: Exception) -> bool:
    return _error_code(exc) in {"412", "PreconditionFailed"}
