"""Explicit configuration for the independent Assets image worker."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import os
from pathlib import Path
import re
from typing import Mapping
from urllib.parse import urlsplit

from pydantic import TypeAdapter, ValidationError
from yoao_assets_media_runtime.additional_original import (
    AdditionalOriginalConfig,
    load_additional_original_config,
)


_BUCKET_ADAPTER = TypeAdapter(str)
_BUCKET_PATTERN = re.compile(
    r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$"
)
_NATIVE_TOKEN = re.compile(r"^[\x21-\x7e]{32,}$")


class ConfigurationError(ValueError):
    """Required image worker configuration is absent or unsafe."""


@dataclass(frozen=True, slots=True)
class ImageStorageConfig:
    endpoint: str
    public_endpoint: str
    region: str
    datasets_bucket: str
    legacy_bucket: str
    cache_bucket: str
    inputs_bucket: str
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    additional_original: AdditionalOriginalConfig | None = field(
        default=None, repr=False
    )
    connect_timeout: float = 10.0
    read_timeout: float = 120.0
    max_attempts: int = 2


@dataclass(frozen=True, slots=True)
class NativeStorageSignerConfig:
    """Explicit Source signer endpoint and its operation-scoped token."""

    url: str
    token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ImageWorkerConfig:
    database_url: str = field(repr=False)
    storage: ImageStorageConfig
    poll_seconds: float = 5.0
    srgb_profile: Path = Path("/usr/share/color/icc/sRGB.icc")
    native_storage: NativeStorageSignerConfig | None = None


def _secret(environment: Mapping[str, str], name: str) -> str:
    direct = environment.get(name)
    file_path = environment.get(f"{name}_FILE")
    if direct is not None and file_path is not None:
        raise ConfigurationError(f"Set only one of {name} and {name}_FILE")
    if direct is not None:
        value = direct
    elif file_path is not None:
        if not file_path.strip():
            raise ConfigurationError(f"Configured secret file {name}_FILE is empty")
        try:
            value = Path(file_path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigurationError(
                f"Cannot read configured secret {name}_FILE"
            ) from exc
    else:
        raise ConfigurationError(f"Missing required setting {name}")
    if not value or not value.strip():
        raise ConfigurationError(f"Configured secret {name} is empty")
    return value


def _database_url(environment: Mapping[str, str]) -> str:
    value = _secret(environment, "ASSETS_IMAGES_DATABASE_URL")
    try:
        parsed = urlsplit(value)
        parsed.port
        valid = (
            parsed.scheme in {"postgres", "postgresql"}
            and bool(parsed.hostname)
            and parsed.username is not None
        )
    except ValueError:
        valid = False
    if not valid:
        raise ConfigurationError("ASSETS_IMAGES_DATABASE_URL must be a PostgreSQL URL")
    return value


def _origin(
    environment: Mapping[str, str], name: str, default: str | None = None
) -> str:
    value = environment.get(name, default or "").strip()
    try:
        parsed = urlsplit(value)
        parsed.port
        valid = (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and parsed.path in {"", "/"}
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise ConfigurationError(f"{name} must be an HTTP(S) origin")
    return value.rstrip("/")


def _bucket_value(name: str, value: str) -> str:
    try:
        _BUCKET_ADAPTER.validate_python(value, strict=True)
    except ValidationError as exc:
        raise ConfigurationError(f"{name} is invalid") from exc
    if (
        not 3 <= len(value) <= 63
        or not _BUCKET_PATTERN.fullmatch(value)
        or re.fullmatch(r"[0-9]+(?:\.[0-9]+){3}", value)
    ):
        raise ConfigurationError(f"{name} is invalid")
    return value


def _bucket(environment: Mapping[str, str], name: str) -> str:
    return _bucket_value(name, environment.get(name, "").strip())


def load_native_storage_config(
    environment: Mapping[str, str],
    url_name: str,
    token_name: str,
) -> NativeStorageSignerConfig | None:
    """Load one explicit Source signer binding; refuse a partial one.

    Native callers never fall back to another signer or to configured cache
    credentials, so an unconfigured lane stays unconfigured for every consumer.
    Deployments that render every optional signer setting as an empty string are
    unconfigured; only a populated partial binding is an error.
    """
    url_value = (environment.get(url_name) or "").strip()
    direct = environment.get(token_name)
    token_file = environment.get(f"{token_name}_FILE")
    direct_set = bool(direct and direct.strip())
    file_set = bool(token_file and token_file.strip())
    if direct_set and file_set:
        raise ConfigurationError(f"Set only one of {token_name} and {token_name}_FILE")
    if not url_value and not direct_set and not file_set:
        return None
    if not url_value:
        raise ConfigurationError(f"Missing required setting {url_name}")
    if not direct_set and not file_set:
        raise ConfigurationError(f"Missing required setting {token_name}")
    secret_environment = dict(environment)
    if not direct_set:
        secret_environment.pop(token_name, None)
    if not file_set:
        secret_environment.pop(f"{token_name}_FILE", None)
    token = _secret(secret_environment, token_name)
    if _NATIVE_TOKEN.fullmatch(token) is None:
        raise ConfigurationError(
            f"{token_name} must be 32+ printable ASCII non-whitespace characters"
        )
    return NativeStorageSignerConfig(
        url=_origin(environment, url_name),
        token=token,
    )


def _positive_float(
    environment: Mapping[str, str], name: str, default: str, *, maximum: float
) -> float:
    try:
        value = float(environment.get(name, default))
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a positive finite number") from exc
    if not math.isfinite(value) or not 0.1 <= value <= maximum:
        raise ConfigurationError(f"{name} must be between 0.1 and {maximum:g}")
    return value


def _positive_int(
    environment: Mapping[str, str], name: str, default: str, *, maximum: int
) -> int:
    try:
        value = int(environment.get(name, default))
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if not 1 <= value <= maximum:
        raise ConfigurationError(f"{name} must be between 1 and {maximum}")
    return value


def load_storage_config(
    environment: Mapping[str, str] | None = None,
) -> ImageStorageConfig:
    """Load fixed bucket names and explicit credentials; never guess legacy storage."""
    values = os.environ if environment is None else environment
    endpoint = _origin(values, "ASSETS_MEDIA_S3_ENDPOINT")
    legacy_name = "ASSETS_IMAGES_LEGACY_BUCKET"
    legacy_value = values.get(legacy_name)
    if legacy_value is None or not legacy_value.strip():
        legacy_value = values.get("MINIO_BUCKET")
    if legacy_value is None or not legacy_value.strip():
        legacy_value = values.get("MINIO_BUCKET_ANNOTATIONS")
    if legacy_value is None or not legacy_value.strip():
        raise ConfigurationError(f"Missing required setting {legacy_name}")
    region = values.get("ASSETS_MEDIA_S3_REGION", "us-east-1").strip()
    if not region:
        raise ConfigurationError("ASSETS_MEDIA_S3_REGION must not be empty")
    datasets_bucket = _bucket(values, "ASSETS_MEDIA_DATASETS_BUCKET")
    legacy_bucket = _bucket_value(legacy_name, legacy_value.strip())
    cache_bucket = _bucket(values, "ASSETS_MEDIA_CACHE_BUCKET")
    inputs_bucket = _bucket(values, "ASSETS_MEDIA_INPUTS_BUCKET")
    if inputs_bucket in {datasets_bucket, legacy_bucket, cache_bucket}:
        raise ConfigurationError(
            "ASSETS_MEDIA_INPUTS_BUCKET must be separate from source and cache buckets"
        )
    return ImageStorageConfig(
        endpoint=endpoint,
        public_endpoint=_origin(values, "ASSETS_MEDIA_S3_PUBLIC_ENDPOINT", endpoint),
        region=region,
        datasets_bucket=datasets_bucket,
        legacy_bucket=legacy_bucket,
        cache_bucket=cache_bucket,
        inputs_bucket=inputs_bucket,
        access_key=_secret(values, "ASSETS_MEDIA_S3_ACCESS_KEY"),
        secret_key=_secret(values, "ASSETS_MEDIA_S3_SECRET_KEY"),
        additional_original=load_additional_original_config(values),
        connect_timeout=_positive_float(
            values, "ASSETS_MEDIA_S3_CONNECT_TIMEOUT_SECONDS", "10", maximum=3600
        ),
        read_timeout=_positive_float(
            values, "ASSETS_MEDIA_S3_READ_TIMEOUT_SECONDS", "120", maximum=3600
        ),
        max_attempts=_positive_int(
            values, "ASSETS_MEDIA_S3_MAX_ATTEMPTS", "2", maximum=10
        ),
    )


def load_config(environment: Mapping[str, str] | None = None) -> ImageWorkerConfig:
    """Load the dedicated image login and fixed storage configuration."""
    values = os.environ if environment is None else environment
    if values.get("ASSETS_IMAGE_EXECUTOR", "split") != "split":
        raise ConfigurationError(
            "the standalone image executor requires ASSETS_IMAGE_EXECUTOR=split"
        )
    poll_seconds = _positive_float(
        values, "ASSETS_IMAGES_POLL_SECONDS", "5", maximum=3600
    )
    profile_value = values.get("ASSETS_IMAGES_SRGB_PROFILE", "").strip()
    if not profile_value:
        profile_value = values.get(
            "IMAGE_PYRAMID_SRGB_PROFILE", "/usr/share/color/icc/sRGB.icc"
        ).strip()
    if not profile_value:
        profile_value = "/usr/share/color/icc/sRGB.icc"
    if not profile_value:
        raise ConfigurationError("ASSETS_IMAGES_SRGB_PROFILE must not be empty")
    profile = Path(profile_value)
    if not profile.is_absolute():
        raise ConfigurationError("ASSETS_IMAGES_SRGB_PROFILE must be an absolute path")
    return ImageWorkerConfig(
        database_url=_database_url(values),
        storage=load_storage_config(values),
        poll_seconds=poll_seconds,
        srgb_profile=profile,
        native_storage=load_native_storage_config(
            values,
            "ASSETS_IMAGES_NATIVE_STORAGE_URL",
            "ASSETS_IMAGES_NATIVE_STORAGE_TOKEN",
        ),
    )
