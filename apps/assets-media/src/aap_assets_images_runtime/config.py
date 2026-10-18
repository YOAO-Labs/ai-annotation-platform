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


_BUCKET_ADAPTER = TypeAdapter(str)
_BUCKET_PATTERN = re.compile(
    r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$"
)


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
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    connect_timeout: float = 10.0
    read_timeout: float = 120.0
    max_attempts: int = 2


@dataclass(frozen=True, slots=True)
class ImageWorkerConfig:
    database_url: str = field(repr=False)
    storage: ImageStorageConfig
    poll_seconds: float = 5.0
    srgb_profile: Path = Path("/usr/share/color/icc/sRGB.icc")


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
    if legacy_value is None:
        # Preserve the deployed annotation bucket only when its existing setting is
        # explicitly present in the process environment.
        legacy_value = values.get("MINIO_BUCKET")
    if legacy_value is None:
        legacy_value = values.get("MINIO_BUCKET_ANNOTATIONS")
    if legacy_value is None or not legacy_value.strip():
        raise ConfigurationError(f"Missing required setting {legacy_name}")
    region = values.get("ASSETS_MEDIA_S3_REGION", "us-east-1").strip()
    if not region:
        raise ConfigurationError("ASSETS_MEDIA_S3_REGION must not be empty")
    return ImageStorageConfig(
        endpoint=endpoint,
        public_endpoint=_origin(values, "ASSETS_MEDIA_S3_PUBLIC_ENDPOINT", endpoint),
        region=region,
        datasets_bucket=_bucket(values, "ASSETS_MEDIA_DATASETS_BUCKET"),
        legacy_bucket=_bucket_value(legacy_name, legacy_value.strip()),
        cache_bucket=_bucket(values, "ASSETS_MEDIA_CACHE_BUCKET"),
        access_key=_secret(values, "ASSETS_MEDIA_S3_ACCESS_KEY"),
        secret_key=_secret(values, "ASSETS_MEDIA_S3_SECRET_KEY"),
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
    poll_seconds = _positive_float(
        values, "ASSETS_IMAGES_POLL_SECONDS", "5", maximum=3600
    )
    profile_value = values.get(
        "ASSETS_IMAGES_SRGB_PROFILE", "/usr/share/color/icc/sRGB.icc"
    ).strip()
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
    )
